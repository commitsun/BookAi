"""
Email send flow: Odoo/app -> BookAI -> Mailgun.

Analogous to template_service.py for WhatsApp:
  - Idempotency check
  - Resolve Property, ChannelEndpoint, Contact, Conversation, Session
  - Persist message (pending) before calling Mailgun
  - Call EmailChannelClient.send_email()
  - Persist EmailMessageMetadata with provider Message-ID (threading anchor)
  - Update delivery status
  - Emit Socket.IO events
"""

from __future__ import annotations

import logging
from datetime import date

import socketio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import Instance
from app.models.message import (
    DeliveryStatus,
    MessageDirection,
    MessageKind,
    MessageSender,
    RoutingStatus,
)
from app.repositories import (
    contact_repo,
    conversation_repo,
    email_message_repo,
    folio_repo,
    instance_repo,
    message_repo,
    session_repo,
)
from app.realtime.events import (
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.schemas.email import EmailSendRequest, EmailSendResponse
from app.services.email_channel_client import (
    EmailChannelClient,
    EmailChannelError,
)

log = logging.getLogger("email_send_service")


async def _resolve_odoo_path(
    request: EmailSendRequest,
    instance: Instance,
    db: AsyncSession,
):
    """Return (prop, channel_endpoint, contact, conversation, conv_created)."""
    if not request.recipient or not request.recipient.email:
        raise HTTPException(
            status_code=422,
            detail=(
                "recipient.email is required when source.hotel is provided"
            ),
        )

    prop = await instance_repo.find_property_by_roomdoo_code(
        db, request.source.hotel.external_code, instance.id  # type: ignore[union-attr]
    )
    if prop is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Property not found: "
                f"external_code={request.source.hotel.external_code}"  # type: ignore[union-attr]
            ),
        )
    if prop.channel_endpoint_id is None:
        raise HTTPException(
            status_code=422,
            detail=f"Property {prop.id} has no linked channel endpoint",
        )

    ep = await instance_repo.find_channel_endpoint_by_id(
        db, prop.channel_endpoint_id
    )
    if ep is None:
        raise HTTPException(
            status_code=500, detail="Channel endpoint not found in database"
        )
    if ep.channel != "email":
        raise HTTPException(
            status_code=422,
            detail=(
                f"Channel endpoint {ep.id} is not an email endpoint "
                f"(channel={ep.channel})"
            ),
        )

    email = str(request.recipient.email).lower()
    contact, _ = await contact_repo.get_or_create_by_email(
        db, email, request.recipient.name
    )
    conversation, conv_created = await conversation_repo.get_or_create(
        db, contact.id
    )
    return prop, ep, contact, conversation, conv_created


async def _resolve_app_path(
    request: EmailSendRequest,
    instance: Instance,
    db: AsyncSession,
):
    """Return (prop, channel_endpoint, contact, conversation)."""
    conv_obj = await conversation_repo.find_by_id(db, request.conversation_id)  # type: ignore[arg-type]
    if conv_obj is None:
        raise HTTPException(
            status_code=404,
            detail=f"Conversation {request.conversation_id} not found",
        )
    contact = conv_obj.contact

    ep_id = request.channel_endpoint_id
    if ep_id is None:
        ep_id = await conversation_repo.find_default_channel_endpoint_id(
            db, conv_obj.id
        )
    if ep_id is None:
        raise HTTPException(
            status_code=422,
            detail="No email channel endpoint found for this conversation",
        )

    ep = await instance_repo.find_channel_endpoint_by_id(db, ep_id)
    if ep is None or ep.channel != "email":
        raise HTTPException(
            status_code=422,
            detail="Specified channel endpoint is not an email endpoint",
        )

    prop = None
    active_sessions = await session_repo.find_active_for_conversation(
        db, conv_obj.id
    )
    if active_sessions and active_sessions[0].property_id is not None:
        prop = await instance_repo.find_property_by_id(
            db, active_sessions[0].property_id, instance.id
        )

    return prop, ep, contact, conv_obj


async def _resolve_folio(
    request: EmailSendRequest,
    db: AsyncSession,
    session_id: int,
) -> None:
    if not (request.source and request.source.origin_folio):
        return
    of = request.source.origin_folio
    if not of.code:
        return

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
    await folio_repo.attach_to_session(db, session_id, folio.id)


async def process_send_email(
    request: EmailSendRequest,
    instance: Instance,
    db: AsyncSession,
    email_client: EmailChannelClient,
    sio: socketio.AsyncServer,
) -> EmailSendResponse:
    # --- Validate content ---
    if not request.text_body and not request.html_body:
        raise HTTPException(
            status_code=422,
            detail="At least one of text_body or html_body is required",
        )

    # --- Idempotency ---
    if request.idempotency_key:
        existing = await message_repo.find_by_idempotency_key(
            db, request.idempotency_key
        )
        if existing:
            log.info(
                "idempotent hit key=%s msg_id=%s",
                request.idempotency_key,
                existing.id,
            )
            return EmailSendResponse(
                status="ok",
                message_id=existing.id,
                conversation_id=existing.conversation_id,
                idempotent=True,
            )

    # --- Resolve caller path ---
    conv_created = False
    if request.source and request.source.hotel:
        prop, ep, contact, conversation, conv_created = (
            await _resolve_odoo_path(request, instance, db)
        )
    elif request.conversation_id is not None:
        prop, ep, contact, conversation = await _resolve_app_path(
            request, instance, db
        )
    else:
        raise HTTPException(
            status_code=422,
            detail=(
                "Provide either source.hotel.external_code or "
                "conversation_id"
            ),
        )

    # --- Channel state ---
    await conversation_repo.get_or_create_channel_state(
        db, conversation.id, ep.id
    )

    # --- AttentionSession ---
    prop_id = prop.id if prop else None
    if prop_id is not None:
        attention_session, _ = await session_repo.get_or_create_active(
            db, conversation.id, prop_id
        )
    else:
        attention_session, _ = await session_repo.find_or_create_unrouted(
            db, conversation.id
        )

    # --- Folio (Odoo path only) ---
    await _resolve_folio(request, db, attention_session.id)

    # --- Sender ---
    if request.agent_user_id is not None:
        sender = MessageSender.agent
    elif request.source is not None:
        sender = MessageSender.system
    else:
        sender = MessageSender.agent

    # --- Recipient email ---
    if request.recipient and request.recipient.email:
        to_email = str(request.recipient.email).lower()
        to_name = request.recipient.name
    elif contact.email:
        to_email = contact.email
        to_name = contact.display_name
    else:
        raise HTTPException(
            status_code=422,
            detail="Cannot determine recipient email address",
        )

    # --- Persist message (pending) ---
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=ep.id,
        attention_session_id=attention_session.id,
        kind=MessageKind.message,
        direction=MessageDirection.outbound,
        sender=sender,
        content=request.text_body,
        routing_status=RoutingStatus.routed if prop_id else None,
        idempotency_key=request.idempotency_key,
        delivery_status=DeliveryStatus.pending,
        agent_user_id=request.agent_user_id,
        agent_display_name=request.agent_display_name,
        wa_message_type="email",
    )

    # --- Thread detection: reply vs. new email ---
    prev_meta = await email_message_repo.find_last_in_conversation(
        db, conversation.id
    )
    thread_in_reply_to: str | None = None
    thread_references: str | None = None
    if prev_meta and prev_meta.provider_message_id:
        thread_in_reply_to = prev_meta.provider_message_id
        ref_parts = [
            r for r in [prev_meta.references, prev_meta.provider_message_id] if r
        ]
        thread_references = " ".join(ref_parts) or None

    # --- Send via Mailgun ---
    provider_message_id: str | None = None
    try:
        provider_message_id = await email_client.send_email(
            to_address=to_email,
            to_name=to_name,
            subject=request.subject,
            text_body=request.text_body,
            html_body=request.html_body,
            channel_endpoint=ep,
            in_reply_to=thread_in_reply_to,
            references=thread_references,
        )
        await message_repo.update_delivery(db, msg, DeliveryStatus.sent)
    except EmailChannelError as exc:
        log.error(
            "email_send failed msg_id=%s status=%s body=%s",
            msg.id,
            exc.status_code,
            exc.body,
        )
        await message_repo.update_delivery(
            db, msg, DeliveryStatus.failed, error=exc.body
        )
        await db.commit()
        raise HTTPException(
            status_code=502,
            detail=f"Email provider error: {exc.body[:200]}",
        ) from exc

    # --- Persist EmailMessageMetadata ---
    to_entry = {"email": to_email, "name": to_name or ""}
    await email_message_repo.create(
        db,
        message_id=msg.id,
        provider_message_id=provider_message_id,
        in_reply_to=thread_in_reply_to,
        references=thread_references,
        subject=request.subject,
        from_address=ep.external_code,
        from_name=ep.display_number,
        to_addresses=[to_entry],
        text_body=request.text_body,
        html_body=request.html_body,
    )

    await db.commit()

    # --- Socket.IO events ---
    try:
        unread_counts = await conversation_repo.get_unread_counts(
            db, [conversation.id], prop_id or 0
        )
        conv_event = (
            EVENT_CONVERSATION_CREATED if conv_created
            else EVENT_CONVERSATION_UPDATED
        )
        if prop_id:
            await sio.emit(
                conv_event,
                build_conversation_payload(
                    conversation,
                    contact,
                    last_message=msg,
                    unread_count=unread_counts.get(conversation.id, 0),
                    ai_enabled=attention_session.ai_enabled,
                ),
                room=f"property:{prop_id}",
            )
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"chat:{contact.phone_code}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Socket.IO emit failed: %s", exc)

    return EmailSendResponse(
        status="ok",
        message_id=msg.id,
        conversation_id=conversation.id,
        provider_message_id=provider_message_id,
    )
