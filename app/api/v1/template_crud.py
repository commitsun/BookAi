"""
POST  /api/v1/whatsapp/templates              — create template (named placeholders → Meta)
PATCH /api/v1/whatsapp/templates/{code}        — update translations
GET   /api/v1/whatsapp/templates/{code}/status — check Meta approval status
POST  /api/v1/whatsapp/templates/check-status  — poll all pending templates
"""

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance
from app.models.channel import ChannelEndpoint
from app.models.instance import Instance, Property
from app.repositories import template_repo
from app.services.meta_template_service import (
    MetaTemplateError,
    check_template_status,
    create_template_in_meta,
    delete_template_in_meta,
)

log = logging.getLogger("template_crud")

router = APIRouter(prefix="/whatsapp/templates", tags=["templates"])


# ── Placeholder mapping ─────────────────────────────────────────────

def _named_to_meta(text: str, parameters: list[str]) -> str:
    """Replace {{ name }} with {{1}}, {{2}}... based on parameter order."""
    result = text
    for i, name in enumerate(parameters, 1):
        result = re.sub(
            r"\{\{\s*" + re.escape(name) + r"\s*\}\}",
            "{{" + str(i) + "}}",
            result,
        )
    return result


def _build_send_components(
    parameters: list[str],
    body_text: str | None,
    header_text: str | None,
    button_texts: list[dict] | None,
) -> list[dict]:
    """Build Meta-format components for message sending (parameter values)."""
    components = []

    # Body parameters
    if body_text and parameters:
        body_params = []
        for name in parameters:
            if re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", body_text):
                body_params.append({"type": "text", "text": "{{" + name + "}}"})
        if body_params:
            components.append({"type": "body", "parameters": body_params})

    # Header parameters (if header has placeholders)
    if header_text and parameters:
        header_params = []
        for name in parameters:
            if re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", header_text):
                header_params.append({"type": "text", "text": "{{" + name + "}}"})
        if header_params:
            components.append({"type": "header", "parameters": header_params})

    # Button parameters
    if button_texts:
        for idx, btn in enumerate(button_texts):
            if btn.get("url") and "{{" in btn.get("url", ""):
                components.append({
                    "type": "button",
                    "sub_type": "url",
                    "index": str(idx),
                    "parameters": [{"type": "text", "text": btn["url"]}],
                })

    return components


# ── Schemas ──────────────────────────────────────────────────────────

class TranslationInput(BaseModel):
    language: str = Field(default="es", description="BCP-47 language tag")
    body_text: str = Field(
        ..., description='Body with named placeholders: "Hola {{ guest_name }}"',
    )
    header_text: str | None = Field(default=None, description="Header text")
    footer_text: str | None = Field(default=None, description="Footer text")
    button_texts: list[dict] | None = Field(
        default=None,
        description='[{"type":"URL","text":"Ver","url":"https://..."}]',
    )
    parameters: list[str] = Field(
        default_factory=list,
        description='Ordered param names: ["guest_name","hotel_name"]',
    )
    active: bool = True
    property_ids: list[int] = Field(
        default_factory=list,
        description="Properties this translation is available for",
    )


class CreateTemplateRequest(BaseModel):
    code: str = Field(..., description="Internal template code")
    category: str = Field(default="UTILITY")
    translations: list[TranslationInput] = Field(..., min_length=1)


class TranslationPatchInput(BaseModel):
    language: str = Field(default="es")
    body_text: str | None = None
    header_text: str | None = None
    footer_text: str | None = None
    button_texts: list[dict] | None = None
    parameters: list[str] | None = None
    active: bool | None = None
    property_ids: list[int] | None = None


class UpdateTemplateRequest(BaseModel):
    translations: list[TranslationPatchInput] = Field(..., min_length=1)


class TranslationOut(BaseModel):
    id: int
    language: str
    body_text: str | None
    header_text: str | None
    footer_text: str | None
    parameters: list[str] | None
    active: bool
    property_ids: list[int]
    meta_template_id: str | None
    meta_status: str


class TemplateOut(BaseModel):
    id: int
    code: str
    category: str
    instance_id: int
    translations: list[TranslationOut]


# ── Helpers ──────────────────────────────────────────────────────────

def _to_out(template) -> TemplateOut:
    return TemplateOut(
        id=template.id,
        code=template.code,
        category=template.category,
        instance_id=template.instance_id,
        translations=[
            TranslationOut(
                id=t.id,
                language=t.language,
                body_text=t.body_text,
                header_text=t.header_text,
                footer_text=t.footer_text,
                parameters=t.parameters,
                active=t.active,
                property_ids=[tp.property_id for tp in t.translation_properties],
                meta_template_id=t.meta_template_id,
                meta_status=t.meta_status,
            )
            for t in template.translations
        ],
    )


async def _get_waba_credentials(
    db: AsyncSession, instance_id: int,
) -> tuple[str, str] | None:
    result = await db.execute(
        select(ChannelEndpoint)
        .join(Property, Property.channel_endpoint_id == ChannelEndpoint.id)
        .where(
            Property.instance_id == instance_id,
            ChannelEndpoint.channel == "whatsapp",
            ChannelEndpoint.account_id.isnot(None),
            ChannelEndpoint.mock_mode.is_(False),
        )
        .limit(1)
    )
    ep = result.scalar_one_or_none()
    if ep and ep.account_id and ep.access_token:
        return ep.account_id, ep.access_token
    return None


async def _register_in_meta(
    template, http, waba_id: str, access_token: str, db: AsyncSession,
) -> None:
    """Register each translation of a template in Meta Cloud API."""
    for trans in template.translations:
        if not trans.body_text:
            continue
        params = trans.parameters or []
        meta_body = _named_to_meta(trans.body_text, params)
        meta_header = _named_to_meta(trans.header_text, params) if trans.header_text else None
        meta_footer = trans.footer_text  # footers don't have placeholders in Meta

        try:
            meta_id, status = await create_template_in_meta(
                http, waba_id, access_token,
                name=template.code,
                language=trans.language,
                category=template.category,
                header_text=meta_header,
                body_text=meta_body,
                footer_text=meta_footer,
                button_texts=trans.button_texts,
            )
            trans.meta_template_id = meta_id
            trans.meta_status = status
        except MetaTemplateError as exc:
            log.error("Meta create failed for '%s' (%s): %s",
                      template.code, trans.language, exc)
            trans.meta_status = "error"

        # Build and store send-components automatically
        trans.components = _build_send_components(
            params, trans.body_text, trans.header_text, trans.button_texts,
        )
        trans.whatsapp_name = template.code

    await db.commit()


# ── POST — create ────────────────────────────────────────────────────

@router.post(
    "",
    response_model=TemplateOut,
    status_code=201,
    summary="Create a WhatsApp template with named placeholders",
)
async def create_template(
    body: CreateTemplateRequest,
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    existing = await template_repo.find_by_code_and_instance(
        db, body.code, instance.id,
    )
    if existing:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Template '{body.code}' already exists for this instance",
        )

    await template_repo.create_template(
        db,
        instance_id=instance.id,
        code=body.code,
        category=body.category,
        translations=[{
            **t.model_dump(),
            "whatsapp_name": body.code,
            "components": _build_send_components(
                t.parameters, t.body_text, t.header_text, t.button_texts,
            ),
        } for t in body.translations],
    )
    await db.commit()

    template = await template_repo.find_by_code_and_instance(db, body.code, instance.id)

    # Register in Meta
    waba = await _get_waba_credentials(db, instance.id)
    if waba:
        waba_id, access_token = waba
        http = request.app.state.wa_client._http
        await _register_in_meta(template, http, waba_id, access_token, db)
    else:
        log.warning("No WABA credentials — template saved locally only")

    template = await template_repo.find_by_code_and_instance(db, body.code, instance.id)
    return _to_out(template)


# ── PATCH — update ───────────────────────────────────────────────────

@router.patch(
    "/{code}",
    response_model=TemplateOut,
    summary="Update a WhatsApp template's translations",
)
async def update_template(
    code: str,
    body: UpdateTemplateRequest,
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> TemplateOut:
    template = await template_repo.find_by_code_and_instance(db, code, instance.id)
    if template is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Template '{code}' not found for this instance",
        )

    translations_data = []
    body_text_changed_langs = []
    for t in body.translations:
        data = t.model_dump(exclude_unset=True)
        data["whatsapp_name"] = code
        # Rebuild components if parameters or body changed
        if "parameters" in data or "body_text" in data:
            params = data.get("parameters") or t.parameters or []
            bt = data.get("body_text") or t.body_text
            ht = data.get("header_text") or t.header_text
            btns = data.get("button_texts") or t.button_texts
            data["components"] = _build_send_components(params, bt, ht, btns)
        if "body_text" in data:
            body_text_changed_langs.append(data.get("language", "es"))
        translations_data.append(data)

    await template_repo.upsert_translations(db, template, translations_data)
    await db.commit()

    # Recreate in Meta if body_text changed
    if body_text_changed_langs:
        waba = await _get_waba_credentials(db, instance.id)
        if waba:
            waba_id, access_token = waba
            http = request.app.state.wa_client._http
            template = await template_repo.find_by_code_and_instance(db, code, instance.id)
            for trans in template.translations:
                if trans.language not in body_text_changed_langs:
                    continue
                if not trans.body_text:
                    continue
                # Delete old and recreate
                if trans.meta_template_id:
                    await delete_template_in_meta(
                        http, waba_id, access_token, template.code,
                    )
                params = trans.parameters or []
                meta_body = _named_to_meta(trans.body_text, params)
                meta_header = (
                    _named_to_meta(trans.header_text, params)
                    if trans.header_text else None
                )
                try:
                    meta_id, status = await create_template_in_meta(
                        http, waba_id, access_token,
                        name=template.code,
                        language=trans.language,
                        category=template.category,
                        header_text=meta_header,
                        body_text=meta_body,
                        footer_text=trans.footer_text,
                        button_texts=trans.button_texts,
                    )
                    trans.meta_template_id = meta_id
                    trans.meta_status = status
                except MetaTemplateError as exc:
                    log.error("Meta recreate failed: %s", exc)
                    trans.meta_status = "error"
            await db.commit()

    template = await template_repo.find_by_code_and_instance(db, code, instance.id)
    return _to_out(template)


# ── GET — status ─────────────────────────────────────────────────────

@router.get(
    "/{code}/status",
    summary="Check Meta approval status for a template",
)
async def get_template_status(
    code: str,
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    template = await template_repo.find_by_code_and_instance(db, code, instance.id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{code}' not found")

    waba = await _get_waba_credentials(db, instance.id)
    results = []
    for trans in template.translations:
        info = {
            "language": trans.language,
            "meta_status": trans.meta_status,
            "meta_template_id": trans.meta_template_id,
        }
        if waba and trans.meta_template_id and trans.meta_status == "pending":
            waba_id, access_token = waba
            http = request.app.state.wa_client._http
            fresh = await check_template_status(
                http, waba_id, access_token, template.code, trans.language,
            )
            if fresh and fresh != trans.meta_status:
                trans.meta_status = fresh
                info["meta_status"] = fresh
                await db.commit()
        results.append(info)

    return {"code": code, "translations": results}


# ── POST — bulk check status ─────────────────────────────────────────

@router.post(
    "/check-status",
    summary="Poll Meta for all pending templates of this instance",
)
async def check_all_pending(
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models.template import WhatsAppTemplate, WhatsAppTemplateTranslation

    result = await db.execute(
        select(WhatsAppTemplateTranslation)
        .join(WhatsAppTemplate)
        .where(
            WhatsAppTemplate.instance_id == instance.id,
            WhatsAppTemplateTranslation.meta_status == "pending",
        )
    )
    pending = list(result.scalars().all())
    if not pending:
        return {"status": "ok", "checked": 0, "updated": 0}

    waba = await _get_waba_credentials(db, instance.id)
    if not waba:
        return {"status": "error", "detail": "No WABA credentials"}

    waba_id, access_token = waba
    http = request.app.state.wa_client._http
    updated = 0
    for trans in pending:
        fresh = await check_template_status(
            http, waba_id, access_token,
            trans.whatsapp_name, trans.language,
        )
        if fresh and fresh != "pending":
            trans.meta_status = fresh
            updated += 1

    if updated:
        await db.commit()

    return {"status": "ok", "checked": len(pending), "updated": updated}
