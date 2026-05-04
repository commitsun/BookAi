"""
Flow 1: Roomdoo sends a template → BookAI persists + sends to Meta + emits Socket.IO event.

Responsibilities:
  - Resolve Property, Template, Contact, Conversation, AttentionSession, Folio
  - Idempotency check (if idempotency_key already processed, return cached result)
  - Persist the message with status=pending before calling Meta
  - Update delivery status after the Meta call
  - Emit Socket.IO events for real-time updates
"""

import logging
from datetime import date

import socketio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import Instance
from app.models.message import DeliveryStatus, MessageDirection, MessageSender, RoutingStatus
from app.repositories import (
    contact_repo,
    conversation_repo,
    folio_repo,
    instance_repo,
    message_repo,
    session_repo,
    template_repo,
)
from app.realtime.events import (
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.schemas.template import SendTemplateRequest, SendTemplateResponse
from app.services.phone_utils import normalize_phone
from app.services.whatsapp_client import ChannelError, WhatsAppClient

log = logging.getLogger("template_service")


import copy
import re

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render_text(template_text: str | None, param_values: dict[str, str]) -> str | None:
    """Replace {{param_name}} placeholders in a template string with actual values."""
    if not template_text:
        return None
    def _replacer(m: re.Match) -> str:
        return param_values.get(m.group(1), m.group(0))
    return _PLACEHOLDER_RE.sub(_replacer, template_text)


def _render_template_content(
    translation, param_values: dict[str, str],
) -> str:
    """Render the full template text (header + body + footer) with parameters filled in."""
    parts: list[str] = []
    header = _render_text(translation.header_text, param_values)
    if header:
        parts.append(header)
    body = _render_text(translation.body_text, param_values)
    if body:
        parts.append(body)
    footer = _render_text(translation.footer_text, param_values)
    if footer:
        parts.append(footer)
    return "\n\n".join(parts)


def _resolve_named_params(
    stored_components: list[dict], param_values: dict[str, str],
) -> list[dict]:
    """Replace {{param_name}} placeholders in stored components with actual values.

    stored_components has the shape built by _build_send_components, e.g.:
      [{"type": "body", "parameters": [{"type": "text", "text": "{{buyer_name}}"}]},
       {"type": "button", "sub_type": "url", "index": "0",
        "parameters": [{"type": "text", "text": "https://x.com/{{folio_details_url}}"}]}]
    """
    result = copy.deepcopy(stored_components)
    for comp in result:
        for param in comp.get("parameters", []):
            text = param.get("text", "")
            for name, value in param_values.items():
                text = re.sub(
                    r"\{\{\s*" + re.escape(name) + r"\s*\}\}",
                    value, text,
                )
            param["text"] = text
    return result


async def process_send_template(
    request: SendTemplateRequest,
    instance: Instance,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
) -> SendTemplateResponse:
    # --- Idempotency ---
    if request.idempotency_key:
        existing = await message_repo.find_by_idempotency_key(db, request.idempotency_key)
        if existing:
            log.info("idempotent hit key=%s msg_id=%s", request.idempotency_key, existing.id)
            return SendTemplateResponse(
                status="ok",
                message_id=existing.id,
                wa_message_id=existing.wa_message_id,
                conversation_id=existing.conversation_id,
                idempotent=True,
            )

    # --- Resolve Property ---
    hotel = request.source.hotel
    prop = None
    if hotel.odoo_id is not None:
        prop = await instance_repo.find_property_by_odoo_property_id(
            db, hotel.odoo_id, instance.id
        )
    elif hotel.external_code:
        prop = await instance_repo.find_property_by_roomdoo_code(
            db, hotel.external_code, instance.id
        )
    else:
        raise HTTPException(
            status_code=422,
            detail="source.hotel must include 'odoo_id' or 'external_code'",
        )
    if prop is None:
        if hotel.odoo_id is not None:
            identifier = f"odoo_id={hotel.odoo_id}"
        else:
            identifier = f"external_code={hotel.external_code}"
        raise HTTPException(
            status_code=404,
            detail=f"Property not found: {identifier}",
        )
    if prop.channel_endpoint_id is None:
        raise HTTPException(
            status_code=422,
            detail=f"Property {prop.id} has no linked channel endpoint",
        )

    # --- Resolve Template translation ---
    lang = request.template.language
    translation = await template_repo.find_translation_for_property(
        db, request.template.code, lang, prop.id
    )
    # Fallback: "en" → search for "en_*" (e.g. en_US)
    if translation is None and "_" not in lang:
        translation = await template_repo.find_translation_for_property_by_prefix(
            db, request.template.code, lang, prop.id
        )
    if translation is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Template not found: code={request.template.code} "
                f"language={lang} property={prop.id}"
            ),
        )

    # Validate template approval via WABA entry (per-WABA status)
    waba_entries = await template_repo.find_waba_entries(db, translation.id)
    if waba_entries:
        # Check if any WABA has the template approved
        any_approved = any(
            e.meta_status in ("approved", "draft") for e in waba_entries
        )
        if not any_approved:
            statuses = ", ".join(f"{e.waba_id}={e.meta_status}" for e in waba_entries)
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Template '{request.template.code}' ({request.template.language}) "
                    f"is not approved by Meta ({statuses}). "
                    "Wait for approval before sending."
                ),
            )

    # --- Resolve ChannelEndpoint ---
    channel_endpoint = await instance_repo.find_channel_endpoint_by_id(
        db, prop.channel_endpoint_id
    )
    if channel_endpoint is None:
        raise HTTPException(
            status_code=500, detail="Channel endpoint not found in database"
        )

    # --- Normalize phone ---
    try:
        phone_code = normalize_phone(request.recipient.phone, request.recipient.country)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # --- Contact + Conversation (get_or_create) ---
    contact, contact_created = await contact_repo.get_or_create(
        db, phone_code, request.recipient.display_name
    )
    conversation, conv_created = await conversation_repo.get_or_create(
        db, contact.id
    )

    # Ensure the channel state row exists for this conversation+endpoint
    await conversation_repo.get_or_create_channel_state(
        db, conversation.id, channel_endpoint.id
    )

    # --- AttentionSession (get_or_create active) ---
    attention_session, _ = await session_repo.get_or_create_active(
        db, conversation.id, prop.id
    )

    # --- Folio (optional) ---
    folio = None
    if request.source.origin_folio and request.source.origin_folio.code:
        of = request.source.origin_folio
        checkin: date | None = None
        checkout: date | None = None
        if of.checkin:
            try:
                checkin = date.fromisoformat(of.checkin)
            except ValueError:
                pass
        if of.checkout:
            try:
                checkout = date.fromisoformat(of.checkout)
            except ValueError:
                pass
        folio, _ = await folio_repo.get_or_create(
            db,
            odoo_external_code=of.code,
            odoo_folio_id=of.id,
            checkin_date=checkin,
            checkout_date=checkout,
        )
        await folio_repo.attach_to_session(db, attention_session.id, folio.id)

    # --- Resolve send components ---
    # If Odoo sends named parameters (dict) instead of pre-built components,
    # build the Meta-format components by substituting values into the
    # stored component template from the translation.
    send_components = request.template.components
    if not send_components and request.template.parameters and translation.components:
        send_components = _resolve_named_params(
            translation.components, request.template.parameters,
        )

    # --- Render content + build payload ---
    param_values = request.template.parameters or {}
    rendered_content = _render_template_content(translation, param_values) or None

    template_payload: dict = {
        "template_code": request.template.code,
        "template_name": translation.whatsapp_name,
        "language": translation.language,
        "components": send_components,
    }
    if translation.header_text:
        template_payload["header_text"] = translation.header_text
    if translation.body_text:
        template_payload["body_text"] = translation.body_text
    if translation.footer_text:
        template_payload["footer_text"] = translation.footer_text
    if translation.button_texts:
        template_payload["button_texts"] = translation.button_texts
    if param_values:
        template_payload["parameters"] = param_values

    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session.id,
        direction=MessageDirection.outbound,
        sender=MessageSender.system,
        content=rendered_content,
        template_code=request.template.code,
        template_language=translation.language,
        template_payload=template_payload,
        routing_status=RoutingStatus.routed,
        idempotency_key=request.idempotency_key,
        delivery_status=DeliveryStatus.pending,
    )

    # --- Send via channel provider ---
    # Use the exact language code from the translation — must match what was
    # registered in Meta (e.g. "es", "en", "es_ES").
    send_language = translation.language

    wa_message_id: str | None = None
    try:
        wa_message_id = await wa_client.send_template(
            to=phone_code,
            channel_endpoint=channel_endpoint,
            template_name=translation.whatsapp_name,
            language=send_language,
            components=send_components,
        )
        await message_repo.update_delivery(
            db, msg, DeliveryStatus.sent, wa_message_id=wa_message_id
        )
    except ChannelError as exc:
        log.error(
            "send_template failed msg_id=%s status=%s body=%s",
            msg.id,
            exc.status_code,
            exc.body,
        )
        await message_repo.update_delivery(db, msg, DeliveryStatus.failed, error=exc.body)
        await db.commit()
        raise HTTPException(status_code=502, detail=f"Channel API error: {exc.body[:200]}")

    await db.commit()

    # --- Socket.IO events ---
    try:
        conv_event = EVENT_CONVERSATION_CREATED if conv_created else EVENT_CONVERSATION_UPDATED
        unread_counts = await conversation_repo.get_unread_counts(db, [conversation.id], prop.id)
        await sio.emit(
            conv_event,
            build_conversation_payload(
                conversation, contact,
                last_message=msg,
                unread_count=unread_counts.get(conversation.id, 0),
                ai_enabled=attention_session.ai_enabled,
            ),
            room=f"property:{prop.id}",
        )
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"chat:{phone_code}",
        )
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)

    return SendTemplateResponse(
        status="ok",
        message_id=msg.id,
        wa_message_id=wa_message_id,
        conversation_id=conversation.id,
    )
