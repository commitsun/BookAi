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
    prop = await instance_repo.find_property_by_roomdoo_code(
        db, request.source.hotel.external_code, instance.id
    )
    if prop is None:
        raise HTTPException(
            status_code=404,
            detail=f"Property not found: external_code={request.source.hotel.external_code}",
        )
    if prop.channel_endpoint_id is None:
        raise HTTPException(
            status_code=422,
            detail=f"Property {prop.id} has no linked channel endpoint",
        )

    # --- Resolve Template translation ---
    translation = await template_repo.find_translation_for_property(
        db, request.template.code, request.template.language, prop.id
    )
    if translation is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Template not found: code={request.template.code} "
                f"language={request.template.language} property={prop.id}"
            ),
        )

    if translation.meta_status not in ("approved", "draft"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Template '{request.template.code}' ({request.template.language}) "
                f"is not approved by Meta (status: {translation.meta_status}). "
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

    # --- Persist message (pending) ---
    template_payload = {
        "template_code": request.template.code,
        "template_name": translation.whatsapp_name,
        "language": translation.language,
        "components": request.template.components,
    }
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session.id,
        direction=MessageDirection.outbound,
        sender=MessageSender.system,
        content=None,
        template_code=request.template.code,
        template_language=translation.language,
        template_payload=template_payload,
        routing_status=RoutingStatus.routed,
        idempotency_key=request.idempotency_key,
        delivery_status=DeliveryStatus.pending,
    )

    # --- Send via channel provider ---
    wa_message_id: str | None = None
    try:
        wa_message_id = await wa_client.send_template(
            to=phone_code,
            channel_endpoint=channel_endpoint,
            template_name=translation.whatsapp_name,
            language=translation.language,
            components=request.template.components,
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
