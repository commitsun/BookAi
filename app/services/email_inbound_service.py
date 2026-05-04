"""
Email inbound flow: Mailgun webhook -> BookAI -> Socket.IO.

Entry point is process_inbound_email(), designed to be called as a
background task from the webhook route so the route can respond 200
immediately (Mailgun expects a fast response).

Responsibilities:
  - Resolve ChannelEndpoint from recipient email address
  - Threading (RFC 2822 headers -> contact lookup -> subject fallback)
  - get_or_create Contact + Conversation
  - Update channel messaging window (last_inbound_at)
  - Route to AttentionSession
  - Persist Message + EmailMessageMetadata + optional attachments
  - Emit Socket.IO events

Also handles Mailgun delivery events (delivered/failed/bounced) to update
message delivery_status.
"""

from __future__ import annotations

import logging

import socketio
from sqlalchemy.ext.asyncio import AsyncSession

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
    instance_repo,
    message_repo,
    session_repo,
)
from app.repositories.email_message_repo import (
    normalize_subject,
    parse_message_ids,
)
from app.realtime.events import (
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    EVENT_MESSAGE_DELIVERY_UPDATED,
    build_conversation_payload,
    build_delivery_updated_payload,
    build_message_created_payload,
)
from app.services.session_service import is_session_active, pick_session

log = logging.getLogger("email_inbound_service")


# ---------------------------------------------------------------------------
# Threading resolution
# ---------------------------------------------------------------------------


async def _resolve_threading(
    db: AsyncSession,
    in_reply_to: str | None,
    references: str | None,
    sender_email: str,
    subject: str | None,
    channel_endpoint_id: int,
):
    """
    Resolve which (conversation, contact) this inbound email belongs to.

    Strategy:
    1. RFC 2822 headers (In-Reply-To / References -> provider_message_id)
    2. Known contact by sender email
    3. Normalised subject + endpoint + 72h window
    4. Fallback: new contact + new conversation
    """
    # Strategy 1: RFC 2822 headers
    candidate_ids = parse_message_ids(in_reply_to, references)
    for mid in candidate_ids:
        meta = await email_message_repo.find_by_provider_message_id(db, mid)
        if meta:
            msg = meta.message
            conv = msg.conversation
            return conv, conv.contact

    # Strategy 2: known contact by email
    contact = await contact_repo.find_by_email(db, sender_email)
    if contact:
        conv, _ = await conversation_repo.get_or_create(db, contact.id)
        return conv, contact

    # Strategy 3: subject + endpoint + 72h
    norm = normalize_subject(subject)
    if norm:
        meta = await email_message_repo.find_recent_by_subject_and_endpoint(
            db, norm, channel_endpoint_id, hours=72
        )
        if meta:
            msg = meta.message
            conv = msg.conversation
            return conv, conv.contact

    # Strategy 4: fallback — create new contact + conversation
    contact, _ = await contact_repo.get_or_create_by_email(
        db, sender_email
    )
    conv, _ = await conversation_repo.get_or_create(db, contact.id)
    return conv, contact


# ---------------------------------------------------------------------------
# Inbound email processing
# ---------------------------------------------------------------------------


async def process_inbound_email(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body_plain: str | None,
    body_html: str | None,
    message_id: str | None,
    in_reply_to: str | None,
    references: str | None,
    db: AsyncSession,
    sio: socketio.AsyncServer,
) -> None:
    sender_email = sender.lower()
    recipient_email = recipient.lower()

    # --- Resolve ChannelEndpoint from recipient ---
    ep = await instance_repo.find_channel_endpoint_by_external_code(
        db, recipient_email
    )
    if ep is None:
        log.warning(
            "No ChannelEndpoint found for recipient=%s, discarding",
            recipient_email,
        )
        return

    # --- Threading ---
    conversation, contact = await _resolve_threading(
        db,
        in_reply_to=in_reply_to,
        references=references,
        sender_email=sender_email,
        subject=subject,
        channel_endpoint_id=ep.id,
    )

    # --- Channel state (update last_inbound_at) ---
    state, _ = await conversation_repo.get_or_create_channel_state(
        db, conversation.id, ep.id
    )
    await conversation_repo.update_channel_last_inbound(db, state)

    # --- Routing ---
    sessions, conv_last_msg_at = await session_repo.find_sessions_with_context(
        db, conversation.id
    )
    active_sessions = [
        s for s in sessions
        if is_session_active(s, conv_last_msg_at, ep.channel)
    ]

    session = pick_session(active_sessions)
    conv_created = False

    if session is None:
        routing_status = RoutingStatus.unassigned
        attention_session, conv_created = (
            await session_repo.find_or_create_unrouted(db, conversation.id)
        )
        prop_id = None
    elif len(active_sessions) > 1:
        routing_status = RoutingStatus.ambiguous
        attention_session = session
        prop_id = session.property_id
    else:
        routing_status = RoutingStatus.routed
        attention_session = session
        prop_id = session.property_id

    # --- Persist message ---
    content = body_plain or "[email]"
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=ep.id,
        attention_session_id=attention_session.id,
        kind=MessageKind.message,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content=content,
        routing_status=routing_status,
        delivery_status=DeliveryStatus.delivered,
        wa_message_type="email",
    )

    # --- Persist EmailMessageMetadata ---
    norm_subject = normalize_subject(subject) or subject
    to_entry = {"email": recipient_email, "name": ""}
    await email_message_repo.create(
        db,
        message_id=msg.id,
        provider_message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=norm_subject,
        from_address=sender_email,
        to_addresses=[to_entry],
        text_body=body_plain,
        html_body=body_html,
    )

    await db.commit()

    # --- Socket.IO events ---
    try:
        room_id = prop_id or 0
        unread_counts = await conversation_repo.get_unread_counts(
            db, [conversation.id], room_id
        )
        needs_attn = await conversation_repo.get_needs_attention(
            db, [conversation.id], room_id
        )
        conv_event = (
            EVENT_CONVERSATION_CREATED if conv_created
            else EVENT_CONVERSATION_UPDATED
        )
        await sio.emit(
            conv_event,
            build_conversation_payload(
                conversation,
                contact,
                last_message=msg,
                unread_count=unread_counts.get(conversation.id, 0),
                needs_attention=needs_attn.get(conversation.id, False),
                ai_enabled=attention_session.ai_enabled,
            ),
            room=f"property:{room_id}",
        )
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"chat:{contact.phone_code}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Socket.IO emit failed: %s", exc)


# ---------------------------------------------------------------------------
# Delivery event processing
# ---------------------------------------------------------------------------


async def process_delivery_event(
    *,
    event: str,
    provider_message_id: str,
    error: str | None,
    db: AsyncSession,
    sio: socketio.AsyncServer,
) -> None:
    meta = await email_message_repo.find_by_provider_message_id(
        db, provider_message_id
    )
    if meta is None:
        log.warning(
            "Delivery event for unknown provider_message_id=%s event=%s",
            provider_message_id,
            event,
        )
        return

    msg = meta.message
    status_map: dict[str, DeliveryStatus] = {
        "delivered": DeliveryStatus.delivered,
        "failed": DeliveryStatus.failed,
        "bounced": DeliveryStatus.bounced,
        "opened": DeliveryStatus.read,
        "accepted": DeliveryStatus.accepted,
    }
    new_status = status_map.get(event)
    if new_status is None:
        log.debug(
            "Unhandled delivery event=%s for msg_id=%s", event, msg.id
        )
        return

    await message_repo.update_delivery(
        db,
        msg,
        new_status,
        error=error if event in ("failed", "bounced") else None,
    )
    await db.commit()

    try:
        await sio.emit(
            EVENT_MESSAGE_DELIVERY_UPDATED,
            build_delivery_updated_payload(msg),
            room=f"chat:{msg.conversation.contact.phone_code}",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Socket.IO delivery emit failed: %s", exc)
