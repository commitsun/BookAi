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

from app.api.dependencies import get_db, get_instance, get_sdk_registry
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.models.channel import ChannelEndpoint
from app.models.instance import Instance, Property
from app.repositories import template_repo
from app.services.meta_template_service import (
    MetaTemplateError,
    check_template_exists,
    check_template_status,
    create_template_in_meta,
    delete_template_in_meta,
)

log = logging.getLogger("template_crud")

router = APIRouter(prefix="/whatsapp/templates", tags=["templates"])


# ── Placeholder mapping ─────────────────────────────────────────────

def _named_to_meta(text: str, parameters: list[str]) -> str:
    """Replace {{ name }} with {{1}}, {{2}}... only for params present in text.

    Numbering is sequential based on order of appearance, skipping params
    not found in the text (e.g. button-only params).
    """
    result = text
    idx = 1
    for name in parameters:
        pattern = r"\{\{\s*" + re.escape(name) + r"\s*\}\}"
        if re.search(pattern, result):
            result = re.sub(pattern, "{{" + str(idx) + "}}", result)
            idx += 1
    return result


def _filter_body_example(
    body_text: str | None, parameters: list[str], body_example: list[str] | None,
) -> list[str] | None:
    """Filter body_example to only include values for params present in body_text.

    Odoo sends examples for ALL params (body + buttons). Meta expects examples
    only for params that appear in the body component.
    """
    if not body_example or not body_text or not parameters:
        return body_example
    filtered = []
    for i, name in enumerate(parameters):
        if i >= len(body_example):
            break
        if re.search(r"\{\{\s*" + re.escape(name) + r"\s*\}\}", body_text):
            filtered.append(body_example[i])
    return filtered or None


def _buttons_named_to_meta(
    button_texts: list[dict] | None, parameters: list[str],
) -> list[dict] | None:
    """Convert named placeholders in button URLs to positional {{N}}.

    Meta numbers button placeholders independently starting at {{1}} per button,
    not sharing the body's numbering.
    """
    if not button_texts:
        return button_texts
    result = []
    for btn in button_texts:
        btn = dict(btn)
        if btn.get("url"):
            # Each button has its own independent numbering starting at {{1}}
            placeholders = re.findall(r"\{\{\s*\w+\s*\}\}", btn["url"])
            url = btn["url"]
            for i, ph in enumerate(placeholders, 1):
                url = url.replace(ph, "{{" + str(i) + "}}", 1)
            btn["url"] = url
        result.append(btn)
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

    # Button parameters — Meta expects only the dynamic part (the placeholder
    # value), not the full URL. Extract named param from the URL template.
    if button_texts:
        for idx, btn in enumerate(button_texts):
            btn_url = btn.get("url", "")
            if btn_url and "{{" in btn_url:
                # Extract placeholder names from the URL
                btn_params = re.findall(r"\{\{\s*(\w+)\s*\}\}", btn_url)
                for bp in btn_params:
                    components.append({
                        "type": "button",
                        "sub_type": "url",
                        "index": str(idx),
                        "parameters": [{"type": "text", "text": "{{" + bp + "}}"}],
                    })

    return components


# ── Schemas ──────────────────────────────────────────────────────────

class TranslationInput(BaseModel):
    language: str = Field(default="es", description="BCP-47 language tag")
    waba_id: str | None = Field(default=None, description="WABA account ID for multi-WABA support")
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
    body_example: list[str] | None = Field(
        default=None,
        description='Example values for body params: ["John Smith", "F2600001"]',
    )
    header_example: list[str] | None = Field(
        default=None,
        description='Example values for header params (rare)',
    )
    active: bool = True
    property_ids: list[int] = Field(
        default_factory=list,
        description="Properties this translation is available for",
    )
    meta_template_id: str | None = Field(
        default=None,
        description="If set, link to existing Meta template instead of creating new",
    )


class CreateTemplateRequest(BaseModel):
    code: str = Field(..., description="Internal template code")
    category: str = Field(default="UTILITY")
    translations: list[TranslationInput] = Field(..., min_length=1)


class TranslationPatchInput(BaseModel):
    language: str = Field(default="es")
    waba_id: str | None = None
    body_text: str | None = None
    header_text: str | None = None
    footer_text: str | None = None
    button_texts: list[dict] | None = None
    parameters: list[str] | None = None
    body_example: list[str] | None = None
    header_example: list[str] | None = None
    active: bool | None = None
    property_ids: list[int] | None = None
    meta_template_id: str | None = None


class UpdateTemplateRequest(BaseModel):
    translations: list[TranslationPatchInput] = Field(..., min_length=1)


class WabaEntryOut(BaseModel):
    waba_id: str
    meta_template_id: str | None
    meta_status: str


class TranslationOut(BaseModel):
    id: int
    language: str
    body_text: str | None
    header_text: str | None
    footer_text: str | None
    parameters: list[str] | None
    active: bool
    property_ids: list[int]
    waba_entries: list[WabaEntryOut] = []


class TemplateOut(BaseModel):
    id: int
    code: str
    category: str
    instance_id: int
    translations: list[TranslationOut]


# ── Helpers ──────────────────────────────────────────────────────────

async def _to_out(template, db=None) -> TemplateOut:
    translations = []
    for t in template.translations:
        waba_entries = []
        if db:
            entries = await template_repo.find_waba_entries(db, t.id)
            waba_entries = [
                WabaEntryOut(waba_id=e.waba_id, meta_template_id=e.meta_template_id, meta_status=e.meta_status)
                for e in entries
            ]
        translations.append(TranslationOut(
            id=t.id,
            language=t.language,
            body_text=t.body_text,
            header_text=t.header_text,
            footer_text=t.footer_text,
            parameters=t.parameters,
            active=t.active,
            property_ids=[tp.property_id for tp in t.translation_properties],
            waba_entries=waba_entries,
        ))
    return TemplateOut(
        id=template.id,
        code=template.code,
        category=template.category,
        instance_id=template.instance_id,
        translations=translations,
    )


async def _resolve_property_ids(
    db: AsyncSession, instance_id: int, odoo_property_ids: list[int],
) -> list[int]:
    """Translate Odoo property IDs to internal BookAI property IDs."""
    result = await db.execute(
        select(Property.id).where(
            Property.instance_id == instance_id,
            Property.odoo_property_id.in_(odoo_property_ids),
        )
    )
    internal_ids = list(result.scalars().all())
    if len(internal_ids) != len(odoo_property_ids):
        log.warning(
            "Could not resolve all property IDs: odoo=%s → internal=%s",
            odoo_property_ids, internal_ids,
        )
    return internal_ids


async def _resolve_waba_from_properties(
    db: AsyncSession, template,
) -> list[tuple[str, str]]:
    """Resolve unique WABA credentials from a template's assigned properties.

    Returns list of (account_id, access_token) tuples, one per distinct WABA.
    """
    property_ids: set[int] = set()
    for trans in template.translations:
        for tp in trans.translation_properties:
            property_ids.add(tp.property_id)

    if not property_ids:
        return []

    result = await db.execute(
        select(ChannelEndpoint.account_id, ChannelEndpoint.access_token)
        .join(Property, Property.channel_endpoint_id == ChannelEndpoint.id)
        .where(
            Property.id.in_(property_ids),
            ChannelEndpoint.channel == "whatsapp",
            ChannelEndpoint.account_id.isnot(None),
            ChannelEndpoint.mock_mode.is_(False),
        )
        .distinct(ChannelEndpoint.account_id)
    )
    return [(row.account_id, row.access_token) for row in result.all()]


async def _get_waba_credentials_for(
    db: AsyncSession, waba_id: str,
) -> tuple[str, str] | None:
    """Get credentials for a specific WABA by account_id."""
    result = await db.execute(
        select(ChannelEndpoint).where(
            ChannelEndpoint.account_id == waba_id,
            ChannelEndpoint.channel == "whatsapp",
            ChannelEndpoint.mock_mode.is_(False),
        ).limit(1)
    )
    ep = result.scalar_one_or_none()
    if ep and ep.account_id and ep.access_token:
        return ep.account_id, ep.access_token
    return None


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


async def _notify_odoo_status(
    sdk_registry: InstanceSDKRegistry | None,
    instance: "Instance",
    template_code: str,
    language: str,
    meta_status: str,
    meta_template_id: str | None = None,
    waba_id: str | None = None,
) -> None:
    """Update template translation status in Odoo via SDK (fire-and-forget)."""
    if not sdk_registry:
        return
    try:
        client = sdk_registry.get_client(instance)
        if not client:
            return
        updated = await client.templates.update_translation_status(
            template_code=template_code,
            language=language,
            meta_status=meta_status,
            meta_template_id=meta_template_id,
            waba_id=waba_id,
        )
        log.info(
            "Odoo SDK: template '%s' (%s) → %s (updated=%s)",
            template_code, language, meta_status, updated,
        )
    except Exception as exc:
        log.warning(
            "Failed to notify Odoo SDK: %s (%s) → %s: %s",
            template_code, language, meta_status, exc,
        )


async def _register_in_meta(
    template, http, waba_id: str, access_token: str, db: AsyncSession,
    sdk_registry: InstanceSDKRegistry | None = None,
    instance: "Instance | None" = None,
) -> None:
    """Register or link each translation of a template in Meta Cloud API.

    Status and meta_template_id are stored per-WABA in template_translation_waba,
    NOT on the translation itself.
    """
    for trans in template.translations:
        if not trans.body_text:
            continue
        params = trans.parameters or []

        # Check existing WABA entry for this translation
        waba_entry = await template_repo.upsert_waba_entry(
            db, trans.id, waba_id,
        )

        if waba_entry.meta_template_id:
            # Already linked — validate status from Meta
            try:
                status = await check_template_status(
                    http, waba_id, access_token,
                    name=template.code, language=trans.language,
                )
                waba_entry.meta_status = status or "approved"
                log.info(
                    "Template '%s' (%s) linked to meta_id=%s status=%s",
                    template.code, trans.language, waba_entry.meta_template_id, waba_entry.meta_status,
                )
            except Exception as exc:
                log.warning(
                    "Meta status check failed for '%s' (%s): %s",
                    template.code, trans.language, exc,
                )
                waba_entry.meta_status = "approved"
        else:
            # No meta_id — check if it already exists in Meta first
            existing = await check_template_exists(
                http, waba_id, access_token,
                name=template.code, language=trans.language,
            )
            if existing:
                meta_id, status = existing
                waba_entry.meta_template_id = meta_id
                waba_entry.meta_status = status
                log.info(
                    "Template '%s' (%s) already exists in Meta: id=%s status=%s",
                    template.code, trans.language, meta_id, status,
                )
                if instance:
                    await _notify_odoo_status(
                        sdk_registry, instance, template.code,
                        trans.language, status, meta_id, waba_id,
                    )
            else:
                # Does not exist — create new in Meta
                meta_body = _named_to_meta(trans.body_text, params)
                meta_header = _named_to_meta(trans.header_text, params) if trans.header_text else None
                meta_footer = trans.footer_text
                meta_buttons = _buttons_named_to_meta(trans.button_texts, params)

                try:
                    meta_id, status = await create_template_in_meta(
                        http, waba_id, access_token,
                        name=template.code,
                        language=trans.language,
                        category=template.category,
                        header_text=meta_header,
                        body_text=meta_body,
                        footer_text=meta_footer,
                        button_texts=meta_buttons,
                        body_example=_filter_body_example(trans.body_text, params, trans.body_example),
                        header_example=trans.header_example,
                    )
                    waba_entry.meta_template_id = meta_id
                    waba_entry.meta_status = status
                    if instance:
                        await _notify_odoo_status(
                            sdk_registry, instance, template.code,
                            trans.language, status, meta_id, waba_id,
                        )
                except MetaTemplateError as exc:
                    log.error(
                        "Meta create failed for '%s' (%s): %s",
                        template.code, trans.language, exc,
                    )
                    waba_entry.meta_status = "error"
                    if instance:
                        await _notify_odoo_status(
                            sdk_registry, instance, template.code,
                            trans.language, "error", waba_id=waba_id,
                        )

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
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
) -> TemplateOut:
    existing = await template_repo.find_by_code_and_instance(
        db, body.code, instance.id,
    )
    if existing:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Template '{body.code}' already exists for this instance",
        )

    # Group translations by language (multiple entries per lang = multi-WABA)
    import asyncio
    from collections import defaultdict

    by_lang: dict[str, list] = defaultdict(list)
    for t in body.translations:
        by_lang[t.language].append(t)

    # Build one translation row per language (first entry wins for body/params)
    translations_data = []
    for lang, entries in by_lang.items():
        first = entries[0]
        data = first.model_dump()
        if data.get("property_ids"):
            data["property_ids"] = await _resolve_property_ids(
                db, instance.id, data["property_ids"],
            )
        data["whatsapp_name"] = body.code
        data["components"] = _build_send_components(
            first.parameters, first.body_text, first.header_text, first.button_texts,
        )
        # Remove waba_id from translation data (goes to waba table)
        data.pop("waba_id", None)
        translations_data.append(data)

    await template_repo.create_template(
        db,
        instance_id=instance.id,
        code=body.code,
        category=body.category,
        translations=translations_data,
    )
    await db.commit()

    template = await template_repo.find_by_code_and_instance(db, body.code, instance.id)

    # Create WABA entries and register in Meta (background)
    waba_tasks = []
    for lang, entries in by_lang.items():
        trans = next((t for t in template.translations if t.language == lang), None)
        if not trans:
            continue
        for entry in entries:
            if entry.waba_id:
                await template_repo.upsert_waba_entry(
                    db, trans.id, entry.waba_id,
                    meta_template_id=entry.meta_template_id,
                    meta_status="approved" if entry.meta_template_id else "draft",
                )
                # Schedule Meta registration if no meta_template_id
                if not entry.meta_template_id:
                    waba_tasks.append((trans, entry.waba_id))
    await db.commit()

    # Background Meta registration per WABA
    if waba_tasks:
        http_client = request.app.state.wa_client._http
        instance_id = instance.id
        code = body.code

        async def _bg_register():
            try:
                from app.core.database import SessionLocal
                async with SessionLocal() as bg_db:
                    tpl = await template_repo.find_by_code_and_instance(bg_db, code, instance_id)
                    if not tpl:
                        return
                    for trans_ref, wid in waba_tasks:
                        creds = await _get_waba_credentials_for(bg_db, wid)
                        if not creds:
                            log.warning("No credentials for WABA %s", wid)
                            continue
                        waba_account_id, access_token = creds
                        await _register_in_meta(
                            tpl, http_client, waba_account_id, access_token, bg_db,
                            sdk_registry=sdk_registry, instance=instance,
                        )
                    await bg_db.commit()
                    log.info("Template '%s' registered in Meta (background)", code)
            except Exception as exc:
                log.error("Background Meta registration failed for '%s': %s", code, exc)

        asyncio.create_task(_bg_register())
    elif not any(e.waba_id for entries in by_lang.values() for e in entries):
        # No explicit waba_id → resolve from template properties
        template = await template_repo.find_by_code_and_instance(db, body.code, instance.id)
        waba_set = await _resolve_waba_from_properties(db, template) if template else []
        if not waba_set:
            log.warning(
                "Template '%s': no properties with WhatsApp configured, skipping Meta registration",
                body.code,
            )
        else:
            http_client = request.app.state.wa_client._http
            instance_id = instance.id
            code = body.code

            async def _bg_from_properties():
                try:
                    from app.core.database import SessionLocal
                    async with SessionLocal() as bg_db:
                        tpl = await template_repo.find_by_code_and_instance(bg_db, code, instance_id)
                        if not tpl:
                            return
                        for waba_account_id, access_token in waba_set:
                            await _register_in_meta(
                                tpl, http_client, waba_account_id, access_token, bg_db,
                                sdk_registry=sdk_registry, instance=instance,
                            )
                        await bg_db.commit()
                        log.info("Template '%s' registered in Meta (from properties)", code)
                except Exception as exc:
                    log.error("Background Meta registration failed for '%s': %s", code, exc)

            asyncio.create_task(_bg_from_properties())

    template = await template_repo.find_by_code_and_instance(db, body.code, instance.id)
    return await _to_out(template, db)


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
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
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
        # Resolve odoo property IDs → internal property IDs
        if "property_ids" in data and data["property_ids"]:
            data["property_ids"] = await _resolve_property_ids(
                db, instance.id, data["property_ids"],
            )
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

    # Recreate in Meta if body_text changed (background)
    if body_text_changed_langs:
        import asyncio
        template = await template_repo.find_by_code_and_instance(db, code, instance.id)
        waba_set = await _resolve_waba_from_properties(db, template) if template else []
        if not waba_set:
            log.warning(
                "Template '%s': no properties with WhatsApp configured, skipping Meta update",
                code,
            )
        else:
            http_client = request.app.state.wa_client._http
            instance_id = instance.id

            async def _bg_update():
                try:
                    from app.core.database import SessionLocal
                    async with SessionLocal() as bg_db:
                        tpl = await template_repo.find_by_code_and_instance(bg_db, code, instance_id)
                        if not tpl:
                            return
                        for waba_account_id, access_token in waba_set:
                            for trans in tpl.translations:
                                if trans.language not in body_text_changed_langs or not trans.body_text:
                                    continue
                                waba_entry = await template_repo.upsert_waba_entry(
                                    bg_db, trans.id, waba_account_id,
                                )
                                if waba_entry.meta_template_id:
                                    await delete_template_in_meta(http_client, waba_account_id, access_token, tpl.code)
                                params = trans.parameters or []
                                meta_body = _named_to_meta(trans.body_text, params)
                                meta_header = _named_to_meta(trans.header_text, params) if trans.header_text else None
                                meta_buttons = _buttons_named_to_meta(trans.button_texts, params)
                                try:
                                    meta_id, status = await create_template_in_meta(
                                        http_client, waba_account_id, access_token,
                                        name=tpl.code, language=trans.language, category=tpl.category,
                                        header_text=meta_header, body_text=meta_body,
                                        footer_text=trans.footer_text, button_texts=meta_buttons,
                                        body_example=_filter_body_example(trans.body_text, params, trans.body_example),
                                        header_example=trans.header_example,
                                    )
                                    waba_entry.meta_template_id = meta_id
                                    waba_entry.meta_status = status
                                    await _notify_odoo_status(
                                        sdk_registry, instance, tpl.code,
                                        trans.language, status, meta_id, waba_account_id,
                                    )
                                except MetaTemplateError as exc:
                                    log.error("Meta recreate failed: %s", exc)
                                    waba_entry.meta_status = "error"
                                    await _notify_odoo_status(
                                        sdk_registry, instance, tpl.code,
                                        trans.language, "error", waba_id=waba_account_id,
                                    )
                        await bg_db.commit()
                        log.info("Template '%s' updated in Meta (background)", code)
                except Exception as exc:
                    log.error("Background Meta update failed for '%s': %s", code, exc)

            asyncio.create_task(_bg_update())

    template = await template_repo.find_by_code_and_instance(db, code, instance.id)
    return await _to_out(template, db)


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

    http = request.app.state.wa_client._http
    results = []
    for trans in template.translations:
        entries = await template_repo.find_waba_entries(db, trans.id)
        for entry in entries:
            info = {
                "language": trans.language,
                "waba_id": entry.waba_id,
                "meta_status": entry.meta_status,
                "meta_template_id": entry.meta_template_id,
            }
            if entry.meta_template_id and entry.meta_status == "pending":
                creds = await _get_waba_credentials_for(db, entry.waba_id)
                if creds:
                    waba_account_id, access_token = creds
                    fresh = await check_template_status(
                        http, waba_account_id, access_token, template.code, trans.language,
                    )
                    if fresh and fresh != entry.meta_status:
                        entry.meta_status = fresh
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
    from app.models.template import (
        TemplateTranslationWaba,
        WhatsAppTemplate,
        WhatsAppTemplateTranslation,
    )

    result = await db.execute(
        select(TemplateTranslationWaba)
        .join(WhatsAppTemplateTranslation)
        .join(WhatsAppTemplate)
        .where(
            WhatsAppTemplate.instance_id == instance.id,
            TemplateTranslationWaba.meta_status == "pending",
        )
    )
    pending = list(result.scalars().all())
    if not pending:
        return {"status": "ok", "checked": 0, "updated": 0}

    http = request.app.state.wa_client._http
    updated = 0
    for entry in pending:
        creds = await _get_waba_credentials_for(db, entry.waba_id)
        if not creds:
            continue
        waba_account_id, access_token = creds
        trans = await db.get(WhatsAppTemplateTranslation, entry.translation_id)
        if not trans:
            continue
        fresh = await check_template_status(
            http, waba_account_id, access_token,
            trans.whatsapp_name, trans.language,
        )
        if fresh and fresh != "pending":
            entry.meta_status = fresh
            updated += 1

    if updated:
        await db.commit()

    return {"status": "ok", "checked": len(pending), "updated": updated}
