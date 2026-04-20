"""
Flow 2: WhatsApp guest message → BookAI persists + emits Socket.IO event.

Entry point is process_inbound_webhook(), called as a background task from the
webhook route handler. The route itself responds 200 immediately to Meta.

Responsibilities:
  - Deduplicate by wa_message_id (Meta may deliver the same webhook twice)
  - Resolve ChannelEndpoint from the phone_number_id in the webhook metadata
  - Get or create Contact + Conversation
  - Update the channel messaging window (last_inbound_at)
  - Route message to active AttentionSession (or mark unassigned/ambiguous)
  - Persist the inbound message
  - Mark the message as read (fire-and-forget)
  - Emit Socket.IO events

Also handles delivery status updates (delivered / read webhooks from Meta).
"""

import asyncio
import logging

import httpx
import socketio
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.models.instance import Property
from app.models.message import (
    DeliveryStatus,
    MessageDirection,
    MessageSender,
    RoutingStatus,
)
from app.models.session import SessionStatus
from app.repositories import (
    contact_repo,
    conversation_repo,
    instance_repo,
    message_repo,
    session_repo,
)
from app.services.session_service import is_session_active, pick_session
from app.realtime.events import (
    EVENT_CONVERSATION_CREATED,
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    EVENT_MESSAGE_DELIVERY_UPDATED,
    build_conversation_payload,
    build_message_created_payload,
    build_delivery_updated_payload,
)
from app.schemas.webhook import MetaWebhookPayload, WebhookMessage, WebhookStatus
from app.services.ai_response_service import try_ai_response
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("webhook_service")


def _extract_text(msg: WebhookMessage) -> str:
    """Return a best-effort text representation for any message type."""
    if msg.type == "text" and msg.text:
        return msg.text.body
    if msg.type == "interactive" and msg.interactive:
        inter = msg.interactive
        if inter.button_reply:
            return inter.button_reply.title
        if inter.list_reply:
            return inter.list_reply.title
    # Media messages: use caption if available, otherwise type placeholder
    if msg.type in ("image", "audio", "video", "document"):
        media = msg.media
        caption = media.caption if media else None
        return caption or f"[{msg.type}]"
    return f"[{msg.type}]"


async def process_inbound_webhook(
    payload: MetaWebhookPayload,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry | None = None,
    llm_client: LLMProvider | None = None,
) -> None:
    for entry in payload.entry:
        for change in entry.changes:
            if not change.value:
                continue
            value = change.value
            phone_number_id = value.metadata.phone_number_id if value.metadata else None

            # --- Delivery status updates ---
            if value.statuses:
                for status_event in value.statuses:
                    await _process_status_update(status_event, db, sio)

            # --- Inbound messages ---
            if value.messages:
                for wa_msg in value.messages:
                    await _process_message(
                        wa_msg,
                        phone_number_id=phone_number_id,
                        contacts=value.contacts or [],
                        db=db,
                        wa_client=wa_client,
                        sio=sio,
                        sdk_registry=sdk_registry,
                        llm_client=llm_client,
                    )


async def _process_message(
    wa_msg: WebhookMessage,
    phone_number_id: str | None,
    contacts: list,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry | None = None,
    llm_client: LLMProvider | None = None,
) -> None:
    # --- Deduplication ---
    existing = await message_repo.find_by_provider_message_id(db, wa_msg.id)
    if existing:
        log.debug("duplicate webhook wa_message_id=%s — skipped", wa_msg.id)
        return

    # --- Resolve ChannelEndpoint ---
    if not phone_number_id:
        log.warning("webhook missing phone_number_id, skipping message id=%s", wa_msg.id)
        return
    channel_endpoint = await instance_repo.find_channel_endpoint_by_external_code(
        db, phone_number_id
    )
    if channel_endpoint is None:
        log.warning("unknown phone_number_id=%s, skipping", phone_number_id)
        return

    # --- Resolve display name from contacts array ---
    display_name: str | None = None
    for c in contacts:
        if hasattr(c, "display_name"):
            display_name = c.display_name
            break

    # --- Contact + Conversation (get_or_create) ---
    phone_code = wa_msg.from_
    contact, _ = await contact_repo.get_or_create(db, phone_code, display_name)
    conversation, conv_created = await conversation_repo.get_or_create(
        db, contact.id
    )

    # Update per-channel last inbound timestamp (WA 24-hour window)
    channel_state, _ = await conversation_repo.get_or_create_channel_state(
        db, conversation.id, channel_endpoint.id
    )
    await conversation_repo.update_channel_last_inbound(db, channel_state)

    # Properties linked to this endpoint — determines routing when no session exists
    props_result = await db.execute(
        select(Property.id).where(
            Property.channel_endpoint_id == channel_endpoint.id
        )
    )
    endpoint_property_ids = list(props_result.scalars().all())

    # --- Routing ---
    all_sessions, conv_last_msg = await session_repo.find_sessions_with_context(
        db, conversation.id
    )

    def _folios(s) -> list:
        return [sf.folio for sf in (s.session_folios or []) if sf.folio is not None]

    active_sessions = [
        s for s in all_sessions if is_session_active(_folios(s), conv_last_msg)
    ]
    routed_property_id: int | None = None

    if len(active_sessions) == 1:
        routing_status = RoutingStatus.routed
        attention_session_id = active_sessions[0].id
        routed_property_id = active_sessions[0].property_id

    elif len(active_sessions) == 0:
        if len(endpoint_property_ids) == 1:
            # Close any stale DB-active sessions (logically inactive)
            stale = [
                s for s in all_sessions
                if s.status == SessionStatus.active
            ]
            if stale:
                await session_repo.close_sessions(db, stale)
            # Single property — no ambiguity, auto-create session
            auto_session, _ = await session_repo.get_or_create_active(
                db, conversation.id, endpoint_property_ids[0]
            )
            routing_status = RoutingStatus.routed
            attention_session_id = auto_session.id
            routed_property_id = endpoint_property_ids[0]
        else:
            # Multiple properties → park in admin inbox (property:0)
            unrouted, _ = await session_repo.find_or_create_unrouted(
                db, conversation.id
            )
            routing_status = RoutingStatus.unassigned
            attention_session_id = unrouted.id

    else:
        # 2+ active sessions → pick the most recently engaged one
        chosen = pick_session(active_sessions)
        routing_status = RoutingStatus.routed
        attention_session_id = chosen.id
        routed_property_id = chosen.property_id

    # --- Persist message ---
    content = _extract_text(wa_msg)
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session_id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content=content,
        wa_message_id=wa_msg.id,
        wa_message_type=wa_msg.type,
        routing_status=routing_status,
        delivery_status=DeliveryStatus.delivered,
    )

    await db.commit()

    # --- Process media (download, transcribe, describe) ---
    from app.services.usage_tracker import UsageTracker
    tracker = UsageTracker(conversation_id=conversation.id)

    if wa_msg.type in ("image", "audio", "video", "document") and wa_msg.media:
        await _process_media(
            wa_msg, msg, channel_endpoint, db,
            wa_client._http if hasattr(wa_client, '_http') else None,
            tracker=tracker,
        )

    # --- Mark read (fire-and-forget) ---
    asyncio.create_task(wa_client.mark_read(wa_msg.id, channel_endpoint))

    # --- Socket.IO events ---
    # Routed → notify only the assigned property.
    # Unassigned/ambiguous → property:0 only (admin inbox).
    if routed_property_id is not None:
        property_rooms = [f"property:{routed_property_id}"]
    else:
        property_rooms = ["property:0"]

    try:
        # message.created → only to the open conversation view
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"chat:{phone_code}",
        )
        # conversation.updated → inbox update (always, not just on new conversation)
        if property_rooms:
            conv_event = (
                EVENT_CONVERSATION_CREATED if conv_created
                else EVENT_CONVERSATION_UPDATED
            )
            for room in property_rooms:
                pid_str = room.split(":")[1]
                if pid_str != "0":
                    counts = await conversation_repo.get_unread_counts(
                        db, [conversation.id], int(pid_str)
                    )
                    unread = counts.get(conversation.id, 0)
                else:
                    unread = 0
                conv_payload = build_conversation_payload(
                    conversation, contact,
                    last_message=msg, unread_count=unread,
                )
                await sio.emit(conv_event, conv_payload, room=room)
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)

    # --- AI response (if enabled for this property) ---
    if routed_property_id is not None and sdk_registry and llm_client:
        await try_ai_response(
            conversation_id=conversation.id,
            message_content=msg.content or content,
            attention_session_id=attention_session_id,
            routed_property_id=routed_property_id,
            channel_endpoint=channel_endpoint,
            contact=contact,
            db=db,
            wa_client=wa_client,
            sio=sio,
            sdk_registry=sdk_registry,
            llm_client=llm_client,
            tracker=tracker,
        )


async def _process_media(
    wa_msg: WebhookMessage,
    msg,  # Message ORM object
    channel_endpoint,
    db: AsyncSession,
    http: httpx.AsyncClient | None,
    tracker=None,  # UsageTracker | None
) -> None:
    """Download, store, and AI-process media attachments."""
    from app.core.config import settings
    from app.models.message import MessageMedia
    from app.services.media_service import download_and_store, MediaDownloadError
    from app.services.media_storage import LocalStorage

    media = wa_msg.media
    if not media or not media.id or not http:
        return

    storage = LocalStorage("/app/media")

    try:
        key, size, mime = await download_and_store(
            http, channel_endpoint.access_token, media.id,
            wa_msg.type, storage, media.mime_type, media.filename,
        )
    except MediaDownloadError as exc:
        log.warning("Media download failed: %s", exc)
        return

    media_record = MessageMedia(
        message_id=msg.id,
        media_type=wa_msg.type,
        mime_type=mime,
        filename=media.filename,
        size_bytes=size,
        wa_media_id=media.id,
        storage_backend="local",
        storage_key=key,
    )
    db.add(media_record)
    await db.flush()

    # AI processing: transcription for audio, vision for images
    file_path = f"/app/media/{key}"

    api_key = settings.openai_api_key
    if not api_key:
        log.debug("No openai_api_key configured — skipping AI media processing")
        await db.commit()
        return

    if wa_msg.type == "audio":
        from app.services.transcription_service import transcribe_audio
        text, duration = await transcribe_audio(http, api_key, file_path)
        if text:
            media_record.transcription = text
            msg.content = f"[audio transcrito] {text}"
        if tracker and duration > 0:
            tracker.add_whisper(duration)

    elif wa_msg.type == "image":
        from app.services.vision_service import describe_image
        desc, v_in, v_out, v_cost = await describe_image(
            file_path, api_key=api_key, model="gpt-4o-mini",
        )
        if desc:
            media_record.vision_description = desc
            caption = media.caption or ""
            msg.content = f"[imagen: {desc}]" + (f" {caption}" if caption else "")
        if tracker and (v_in or v_out):
            tracker.add_vision(v_in, v_out, v_cost)

    await db.commit()


async def _process_status_update(
    status_event: WebhookStatus,
    db: AsyncSession,
    sio: socketio.AsyncServer,
) -> None:
    msg = await message_repo.find_by_provider_message_id(db, status_event.id)
    if msg is None:
        return

    status_map = {
        "sent": DeliveryStatus.sent,
        "delivered": DeliveryStatus.delivered,
        "read": DeliveryStatus.read,
        "failed": DeliveryStatus.failed,
    }
    new_status = status_map.get(status_event.status)
    if new_status is None:
        return

    error: str | None = None
    if status_event.errors:
        error = str(status_event.errors[0])

    await message_repo.update_delivery(db, msg, new_status, error=error)
    await db.commit()

    try:
        conv = await conversation_repo.find_by_id(db, msg.conversation_id)
        if conv:
            phone_code = conv.contact.phone_code if conv.contact else None
            await sio.emit(
                EVENT_MESSAGE_DELIVERY_UPDATED,
                build_delivery_updated_payload(msg),
                room=f"chat:{phone_code}",
            )
    except Exception as exc:
        log.warning("Socket.IO delivery_updated emit failed: %s", exc)
