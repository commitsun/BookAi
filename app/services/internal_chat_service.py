"""
Internal chat service — handles conversations from hotel staff and Roomdoo team.

These conversations don't go through WhatsApp. Messages come from the app's
REST API and AI responses are delivered via Socket.IO only.
"""

import asyncio
import logging

import socketio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.instance import Instance, Property
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageSender
from app.models.session import AttentionSession
from app.repositories import contact_repo, conversation_repo, message_repo
from app.services.instance_sdk_registry import InstanceSDKRegistry

log = logging.getLogger("internal_chat")


async def create_internal_conversation(
    db: AsyncSession,
    property_id: int,
    conversation_type: str = "internal",
    odoo_user_id: int | None = None,
    odoo_user_login: str | None = None,
) -> Conversation:
    """Create a new internal conversation thread.

    Creates a synthetic contact for the user if needed, then a conversation
    and an attention session linked to the property.
    """
    # Get or create contact for this internal user
    phone_code = f"internal:{odoo_user_id or odoo_user_login or 'anonymous'}"
    display_name = odoo_user_login or f"User {odoo_user_id}"

    contact, _ = await contact_repo.get_or_create(db, phone_code, display_name)

    # Create conversation (always new thread for internal)
    conv = Conversation(
        contact_id=contact.id,
        conversation_type=conversation_type,
        odoo_user_id=odoo_user_id,
        odoo_user_login=odoo_user_login,
    )
    db.add(conv)
    await db.flush()

    # Create attention session for the property
    session = AttentionSession(
        conversation_id=conv.id,
        property_id=property_id,
        caller_type=conversation_type,
        ai_enabled=True,
    )
    if odoo_user_id:
        session.odoo_user_id = odoo_user_id
    db.add(session)
    await db.flush()

    log.info(
        "Created internal conversation %d (type=%s, user=%s, property=%d)",
        conv.id, conversation_type, odoo_user_login or odoo_user_id, property_id,
    )
    return conv


async def list_internal_conversations(
    db: AsyncSession,
    property_id: int,
    conversation_type: str = "internal",
    odoo_user_id: int | None = None,
    odoo_user_login: str | None = None,
    limit: int = 50,
) -> list[Conversation]:
    """List internal conversations for a user, with last message."""
    from app.models.message import Message

    # Subquery for last message
    last_msg_at = (
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )

    stmt = (
        select(Conversation)
        .where(
            Conversation.id.in_(
                select(AttentionSession.conversation_id)
                .where(AttentionSession.property_id == property_id)
                .distinct()
            ),
            Conversation.conversation_type == conversation_type,
        )
    )

    # Filter by user
    if odoo_user_id:
        stmt = stmt.where(Conversation.odoo_user_id == odoo_user_id)
    elif odoo_user_login:
        stmt = stmt.where(Conversation.odoo_user_login == odoo_user_login)

    # Eager load last message
    stmt = (
        stmt
        .options(
            selectinload(Conversation.messages.and_(
                Message.id.in_(
                    select(func.max(Message.id))
                    .where(Message.conversation_id == Conversation.id)
                    .group_by(Message.conversation_id)
                )
            ))
        )
        .order_by(last_msg_at.desc().nullslast())
        .limit(limit)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def send_internal_message(
    db: AsyncSession,
    conversation: Conversation,
    content: str,
    odoo_user_id: int | None,
    instance: Instance,
    sdk_registry: InstanceSDKRegistry,
    llm_client,
    wa_client,
    sio: socketio.AsyncServer,
    mcp_manager=None,
) -> Message:
    """Persist an internal user message and trigger the AI pipeline.

    Unlike WhatsApp messages, the AI response is NOT sent via any channel —
    it's only persisted and emitted via Socket.IO.
    """
    # Get the active session
    session_result = await db.execute(
        select(AttentionSession)
        .where(
            AttentionSession.conversation_id == conversation.id,
            AttentionSession.status == "active",
        )
        .limit(1)
    )
    session = session_result.scalar_one_or_none()
    if not session:
        raise ValueError(f"No active session for conversation {conversation.id}")

    # Persist user message
    msg = await message_repo.create(
        db,
        conversation_id=conversation.id,
        channel_endpoint_id=None,
        attention_session_id=session.id,
        direction=MessageDirection.inbound,
        sender=MessageSender.agent,  # Internal user
        content=content,
        delivery_status=DeliveryStatus.delivered,
    )
    await db.commit()

    # Emit message.created via Socket.IO
    from app.realtime.events import build_message_created_payload, EVENT_MESSAGE_CREATED
    contact = await db.get(Contact, conversation.contact_id)

    try:
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"internal:{odoo_user_id or conversation.odoo_user_id or 'anon'}",
        )
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)

    # Trigger AI pipeline in background
    property_id = session.property_id
    log.info("Triggering internal pipeline for conv=%d property=%d", conversation.id, property_id)
    asyncio.create_task(
        _run_internal_pipeline(
            conversation_id=conversation.id,
            message_content=content,
            attention_session_id=session.id,
            property_id=property_id,
            contact=contact,
            db_url=str(db.bind.url) if db.bind else None,
            instance=instance,
            sdk_registry=sdk_registry,
            llm_client=llm_client,
            wa_client=wa_client,
            sio=sio,
            mcp_manager=mcp_manager,
        )
    )

    return msg


async def _run_internal_pipeline(
    conversation_id: int,
    message_content: str,
    attention_session_id: int,
    property_id: int,
    contact,
    db_url: str | None,
    instance: Instance,
    sdk_registry: InstanceSDKRegistry,
    llm_client,
    wa_client,
    sio: socketio.AsyncServer,
    mcp_manager=None,
) -> None:
    """Run the AI pipeline for an internal message.

    Uses the same pipeline as WhatsApp messages but with a different
    send mechanism: Socket.IO only, no WhatsApp.
    """
    from app.core.database import SessionLocal
    from app.models.channel import ChannelEndpoint
    from app.services.ai_response_service import try_ai_response

    log.info("Internal pipeline starting for conv=%d", conversation_id)
    try:
        async with SessionLocal() as db:
            # Get channel endpoint (may be None for internal)
            prop = await db.get(Property, property_id)
            channel_endpoint = None
            if prop and prop.channel_endpoint_id:
                channel_endpoint = await db.get(ChannelEndpoint, prop.channel_endpoint_id)

            # If no channel endpoint, create a dummy one for the pipeline
            # (the pipeline needs it but won't send via WhatsApp for internal)
            if not channel_endpoint:
                # Use a mock endpoint — messages won't be sent externally
                channel_endpoint = ChannelEndpoint(
                    id=0,
                    channel="internal",
                    external_code="internal",
                    access_token="",
                )

            # Re-fetch contact in this session
            from app.models.contact import Contact
            contact_obj = await db.get(Contact, contact.id) if contact else None

            from app.services.usage_tracker import UsageTracker
            tracker = UsageTracker(conversation_id=conversation_id)

            await try_ai_response(
                conversation_id=conversation_id,
                message_content=message_content,
                attention_session_id=attention_session_id,
                routed_property_id=property_id,
                channel_endpoint=channel_endpoint,
                contact=contact_obj,
                db=db,
                wa_client=wa_client,
                sio=sio,
                sdk_registry=sdk_registry,
                llm_client=llm_client,
                tracker=tracker,
                mcp_manager=mcp_manager,
            )
    except Exception as exc:
        log.error("Internal pipeline failed for conv=%d: %s", conversation_id, exc, exc_info=True)
