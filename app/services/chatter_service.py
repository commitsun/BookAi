"""
Flow 3: App user (hotel operator) sends a message → BookAI sends via channel + persists.

Responsibilities:
  - Resolve Conversation (must exist; 404 if not)
  - Resolve the target channel endpoint:
      · Use channel_endpoint_id from request if provided
      · Otherwise default to the most recently active channel for this conversation
  - Verify the channel's messaging window is still open (422 if closed)
  - Send text via WhatsAppClient
  - Persist message (outbound, sender=agent) with traceability fields
  - Emit Socket.IO events
"""

import logging
from datetime import datetime, timedelta, timezone

import socketio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.instance import Instance
from app.models.message import DeliveryStatus, MessageDirection, MessageSender
from app.repositories import (
    conversation_repo,
    instance_repo,
    message_repo,
    session_repo,
)
from app.realtime.events import (
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.schemas.message import SendMessageRequest, SendMessageResponse
from app.services.whatsapp_client import ChannelError, WhatsAppClient

log = logging.getLogger("chatter_service")


def _messaging_window_hours(channel: str) -> int | None:
    """Return the outbound messaging window in hours, or None if no restriction.

    WhatsApp enforces a 24-hour window after the last inbound message.
    Other channels (Telegram, SMS, email, …) have no such restriction.
    """
    return 24 if channel == "whatsapp" else None


async def process_send_message(
    request: SendMessageRequest,
    instance: Instance,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
) -> SendMessageResponse:
    # --- Resolve conversation ---
    conversation = await conversation_repo.find_by_id(db, request.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not conversation.contact:
        raise HTTPException(status_code=500, detail="Cannot resolve guest phone number")
    phone_code = conversation.contact.phone_code

    # --- Resolve channel endpoint ---
    if request.channel_endpoint_id:
        channel_endpoint = await instance_repo.find_channel_endpoint_by_id(
            db, request.channel_endpoint_id
        )
        if channel_endpoint is None:
            raise HTTPException(
                status_code=404,
                detail=f"Channel endpoint {request.channel_endpoint_id} not found",
            )
    else:
        # Default: most recently active channel for this conversation
        default_id = await conversation_repo.find_default_channel_endpoint_id(
            db, conversation.id
        )
        if default_id is None:
            raise HTTPException(
                status_code=422,
                detail="No channel has been used in this conversation yet",
            )
        channel_endpoint = await instance_repo.find_channel_endpoint_by_id(db, default_id)
        if channel_endpoint is None:
            raise HTTPException(status_code=500, detail="Default channel endpoint not found")

    # --- Channel window check (channel-specific; WhatsApp=24h, others=no limit) ---
    channel_state = await conversation_repo.find_channel_state(
        db, conversation.id, channel_endpoint.id
    )
    last_inbound = channel_state.last_inbound_at if channel_state else None
    window_hours = _messaging_window_hours(channel_endpoint.channel)

    if not settings.debug and not _window_open(last_inbound, channel_endpoint.channel):
        if last_inbound is None:
            reason = (
                f"No inbound message has ever been received on channel "
                f"'{channel_endpoint.channel}' (endpoint {channel_endpoint.id}) "
                f"for conversation {conversation.id}."
            )
        elif window_hours is not None:
            elapsed = datetime.now(timezone.utc) - last_inbound
            elapsed_h = round(elapsed.total_seconds() / 3600, 1)
            reason = (
                f"The {window_hours}h messaging window for channel "
                f"'{channel_endpoint.channel}' (endpoint {channel_endpoint.id}) "
                f"is closed. Last inbound message was {elapsed_h}h ago "
                f"({last_inbound.isoformat()})."
            )
        else:
            reason = (
                f"Messaging window is closed for channel "
                f"'{channel_endpoint.channel}' (endpoint {channel_endpoint.id})."
            )

        raise HTTPException(
            status_code=422,
            detail=reason,
        )

    # --- Routing: find active session for context ---
    active_sessions = await session_repo.find_active_for_conversation(db, conversation.id)
    attention_session_id: int | None = None
    property_id: int | None = None
    session_ai_enabled: bool | None = None
    if len(active_sessions) == 1:
        attention_session_id = active_sessions[0].id
        property_id = active_sessions[0].property_id
        session_ai_enabled = active_sessions[0].ai_enabled

    # --- Format for target channel ---
    from app.services.channel_formatter import format_for_channel
    send_text = format_for_channel(request.content, channel_endpoint.channel)

    # --- Send via channel ---
    wa_message_id: str | None = None
    try:
        wa_message_id = await wa_client.send_text(
            to=phone_code,
            channel_endpoint=channel_endpoint,
            text=send_text,
        )
    except ChannelError as exc:
        log.error(
            "send_text failed conversation=%s channel=%s status=%s",
            conversation.id,
            channel_endpoint.id,
            exc.status_code,
        )
        raise HTTPException(status_code=502, detail=f"Channel API error: {exc.body[:200]}")

    # --- Persist message ---
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session_id,
        direction=MessageDirection.outbound,
        sender=MessageSender.agent,
        content=request.content,
        agent_user_id=request.agent_user_id,
        agent_display_name=request.agent_display_name,
        wa_message_id=wa_message_id,
        delivery_status=DeliveryStatus.sent,
    )

    await db.commit()

    # --- Socket.IO events ---
    # message.created → only to the open conversation view (chat room)
    # conversation.updated → inbox update for the property panel
    try:
        msg_payload = build_message_created_payload(msg, conversation.contact)
        await sio.emit(EVENT_MESSAGE_CREATED, msg_payload, room=f"chat:{phone_code}")
        if property_id:
            counts = await conversation_repo.get_unread_counts(
                db, [conversation.id], property_id
            )
            await sio.emit(
                EVENT_CONVERSATION_UPDATED,
                build_conversation_payload(
                    conversation, conversation.contact,
                    last_message=msg,
                    unread_count=counts.get(conversation.id, 0),
                    ai_enabled=session_ai_enabled,
                ),
                room=f"property:{property_id}",
            )
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)

    return SendMessageResponse(
        status="ok",
        message_id=msg.id,
        wa_message_id=wa_message_id,
        conversation_id=conversation.id,
    )


def _window_open(last_inbound_at: datetime | None, channel: str) -> bool:
    """True if the outbound messaging window is open for the given channel.

    Channels with no window restriction (non-WhatsApp) always return True,
    provided at least one inbound message has been received.
    WhatsApp requires the last inbound message within the last 24 hours.
    """
    window_hours = _messaging_window_hours(channel)
    if window_hours is None:
        # No restriction: any prior inbound is sufficient
        return last_inbound_at is not None
    if last_inbound_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    return last_inbound_at >= cutoff
