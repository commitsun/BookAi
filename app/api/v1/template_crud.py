"""
POST  /api/v1/whatsapp/templates              — create a template (+ register in Meta)
PATCH /api/v1/whatsapp/templates/{code}        — update translations (+ recreate in Meta if text changes)
GET   /api/v1/whatsapp/templates/{code}/status — check Meta approval status
POST  /api/v1/whatsapp/templates/check-status  — poll all pending templates
"""

import logging

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


# ── Schemas ──────────────────────────────────────────────────────────

class TranslationInput(BaseModel):
    whatsapp_name: str = Field(..., description="Meta platform template name")
    language: str = Field(default="es", description="BCP-47 language tag")
    components: list[dict] = Field(
        default_factory=list,
        description="Meta-format component array for sending (parameters)",
    )
    active: bool = True
    property_ids: list[int] = Field(
        default_factory=list,
        description="Properties this translation is available for",
    )
    body_text: str = Field(
        ...,
        description="Body text with placeholders: 'Hola {{1}}, su reserva en {{2}}'",
    )
    header_text: str | None = Field(
        default=None, description="Header text (optional)",
    )
    footer_text: str | None = Field(
        default=None, description="Footer text (optional)",
    )
    button_texts: list[dict] | None = Field(
        default=None,
        description='Buttons: [{"type": "URL", "text": "Ver", "url": "https://..."}]',
    )


class CreateTemplateRequest(BaseModel):
    code: str = Field(..., description="Internal template code (unique per instance)")
    category: str = Field(
        default="UTILITY",
        description="UTILITY | MARKETING | AUTHENTICATION",
    )
    translations: list[TranslationInput] = Field(
        ..., min_length=1,
        description="At least one language translation",
    )


class TranslationPatchInput(BaseModel):
    whatsapp_name: str = Field(..., description="Meta platform template name")
    language: str = Field(default="es", description="BCP-47 language tag")
    components: list[dict] | None = None
    active: bool | None = None
    property_ids: list[int] | None = None
    body_text: str | None = None
    header_text: str | None = None
    footer_text: str | None = None
    button_texts: list[dict] | None = None


class UpdateTemplateRequest(BaseModel):
    translations: list[TranslationPatchInput] = Field(
        ..., min_length=1,
        description="Translations to upsert (by language)",
    )


class TranslationOut(BaseModel):
    id: int
    whatsapp_name: str
    language: str
    components: list[dict]
    active: bool
    property_ids: list[int]
    meta_template_id: str | None
    meta_status: str
    body_text: str | None
    header_text: str | None
    footer_text: str | None


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
                whatsapp_name=t.whatsapp_name,
                language=t.language,
                components=t.components,
                active=t.active,
                property_ids=[tp.property_id for tp in t.translation_properties],
                meta_template_id=t.meta_template_id,
                meta_status=t.meta_status,
                body_text=t.body_text,
                header_text=t.header_text,
                footer_text=t.footer_text,
            )
            for t in template.translations
        ],
    )


async def _get_waba_credentials(
    db: AsyncSession, instance_id: int,
) -> tuple[str, str] | None:
    """Find WABA credentials from the first WhatsApp channel endpoint of this instance."""
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


# ── POST — create ────────────────────────────────────────────────────


@router.post(
    "",
    response_model=TemplateOut,
    status_code=201,
    summary="Create a WhatsApp template and register in Meta",
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
        translations=[t.model_dump() for t in body.translations],
    )
    await db.commit()

    # Reload with relationships for Meta registration
    template = await template_repo.find_by_code_and_instance(
        db, body.code, instance.id,
    )

    # Register in Meta
    waba = await _get_waba_credentials(db, instance.id)
    if waba:
        waba_id, access_token = waba
        http = request.app.state.wa_client._http
        for trans in template.translations:
            if not trans.body_text:
                continue
            try:
                meta_id, status = await create_template_in_meta(
                    http, waba_id, access_token,
                    name=trans.whatsapp_name,
                    language=trans.language,
                    category=body.category,
                    header_text=trans.header_text,
                    body_text=trans.body_text,
                    footer_text=trans.footer_text,
                    button_texts=trans.button_texts,
                )
                trans.meta_template_id = meta_id
                trans.meta_status = status
            except MetaTemplateError as exc:
                log.error("Meta create failed for '%s' (%s): %s",
                          trans.whatsapp_name, trans.language, exc)
                trans.meta_status = "error"
        await db.commit()
    else:
        log.warning("No WABA credentials found for instance %d — template saved locally only",
                     instance.id)

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

    translations_data = [t.model_dump(exclude_unset=True) for t in body.translations]
    await template_repo.upsert_translations(db, template, translations_data)
    await db.commit()

    # Recreate in Meta if body_text changed
    waba = await _get_waba_credentials(db, instance.id)
    if waba:
        waba_id, access_token = waba
        http = request.app.state.wa_client._http
        # Reload template with fresh data
        template = await template_repo.find_by_code_and_instance(db, code, instance.id)
        for t_data in translations_data:
            if "body_text" not in t_data:
                continue
            lang = t_data.get("language", "es")
            trans = next((t for t in template.translations if t.language == lang), None)
            if not trans or not trans.body_text:
                continue
            # Delete old and recreate
            if trans.meta_template_id:
                await delete_template_in_meta(
                    http, waba_id, access_token, trans.whatsapp_name,
                )
            try:
                meta_id, status = await create_template_in_meta(
                    http, waba_id, access_token,
                    name=trans.whatsapp_name,
                    language=trans.language,
                    category=template.category,
                    header_text=trans.header_text,
                    body_text=trans.body_text,
                    footer_text=trans.footer_text,
                    button_texts=trans.button_texts,
                )
                trans.meta_template_id = meta_id
                trans.meta_status = status
            except MetaTemplateError as exc:
                log.error("Meta recreate failed for '%s' (%s): %s",
                          trans.whatsapp_name, trans.language, exc)
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
        status_info = {
            "language": trans.language,
            "meta_status": trans.meta_status,
            "meta_template_id": trans.meta_template_id,
        }
        # Refresh from Meta if we have credentials and a pending template
        if waba and trans.meta_template_id and trans.meta_status == "pending":
            waba_id, access_token = waba
            http = request.app.state.wa_client._http
            fresh_status = await check_template_status(
                http, waba_id, access_token,
                trans.whatsapp_name, trans.language,
            )
            if fresh_status and fresh_status != trans.meta_status:
                trans.meta_status = fresh_status
                status_info["meta_status"] = fresh_status
                await db.commit()
        results.append(status_info)

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
