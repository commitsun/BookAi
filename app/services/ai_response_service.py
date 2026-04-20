"""
AI response flow: after an inbound message is persisted, optionally generate
an AI response and send it back via the channel.

Called from webhook_service._process_message when the property has ai_enabled=True.
If anything fails, the inbound message is already safe — errors are logged, not raised.
"""

import logging

import socketio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.instance import Instance, Property
from app.models.message import DeliveryStatus, MessageDirection, MessageSender
from app.repositories import conversation_repo, message_repo
from app.realtime.events import (
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.services.context_builder import build_prompt
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMClientError, LLMProvider
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("ai_response_service")


async def try_ai_response(
    conversation_id: int,
    message_content: str,
    attention_session_id: int,
    routed_property_id: int,
    channel_endpoint: ChannelEndpoint,
    contact: Contact,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry,
    llm_client: LLMProvider,
    tracker=None,  # UsageTracker | None
) -> None:
    """Generate and send an AI response if the property has AI enabled.

    This function is fire-and-forget safe: all exceptions are caught and logged.
    The inbound message is already persisted before this is called.
    """
    try:
        await _generate_and_send(
            conversation_id, message_content, attention_session_id,
            routed_property_id, channel_endpoint, contact,
            db, wa_client, sio, sdk_registry, llm_client, tracker,
        )
    except Exception as exc:
        log.error(
            "AI response failed for conversation=%d: %s",
            conversation_id, exc, exc_info=True,
        )


async def _generate_and_send(
    conversation_id: int,
    message_content: str,
    attention_session_id: int,
    routed_property_id: int,
    channel_endpoint: ChannelEndpoint,
    contact: Contact,
    db: AsyncSession,
    wa_client: WhatsAppClient,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry,
    llm_client: LLMProvider,
    tracker=None,
) -> None:
    # --- Load property and check ai_enabled ---
    from sqlalchemy import select
    result = await db.execute(
        select(Property)
        .options(selectinload(Property.instance))
        .where(Property.id == routed_property_id)
    )
    prop = result.scalar_one_or_none()
    if not prop or not prop.ai_enabled:
        return

    # --- Check session-level AI toggle ---
    from app.models.session import AttentionSession
    session = await db.get(AttentionSession, attention_session_id)
    if session and not session.ai_enabled:
        return

    instance: Instance = prop.instance

    # --- Load agents from Odoo via SDK registry ---
    loader = await sdk_registry.get_or_load_agents(instance)
    if loader is None:
        log.debug("No SDK config for instance %d, skipping AI", instance.id)
        return

    candidates = loader.list_for_caller_type("external_guest")
    if not candidates:
        log.debug("No AI agents for external_guest, skipping")
        return

    # Pick agent (single candidate or first — AgentSelector is a future PR)
    agent_entry = candidates[0]
    agent = agent_entry.config
    docs = agent_entry.documents

    if not agent.llm_account or not agent.llm_account.api_key:
        log.warning("Agent %s has no LLM credentials, skipping AI", agent.technical_name)
        return

    model = agent.effective_model
    if not model:
        log.warning("Agent %s has no model configured, skipping AI", agent.technical_name)
        return

    # --- Build prompt with conversation history ---
    history = await message_repo.find_recent_by_conversation(db, conversation_id, limit=20)
    history.reverse()  # oldest first

    prompt_messages = build_prompt(
        agent=agent,
        docs=docs,
        conversation_history=history,
        current_message=message_content,
        property_name=prop.name,
    )

    # --- Call LLM ---
    llm_messages = [{"role": m.role, "content": m.content} for m in prompt_messages]

    try:
        response = await llm_client.chat(
            messages=llm_messages,
            provider=agent.llm_account.provider,
            api_key=agent.llm_account.api_key,
            model=model,
            api_base_url=agent.llm_account.api_base_url,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
        )
    except LLMClientError as exc:
        log.error("LLM call failed for agent=%s: %s", agent.technical_name, exc)
        return

    # --- Track LLM cost ---
    llm_cost = 0.0
    try:
        from litellm import cost_per_token
        p, c = cost_per_token(
            response.model,
            prompt_tokens=response.tokens_in,
            completion_tokens=response.tokens_out,
        )
        llm_cost = p + c
    except Exception:
        pass

    if tracker:
        tracker.add_llm(response.tokens_in, response.tokens_out, llm_cost, response.model)

    log.info(
        "AI response generated: agent=%s model=%s tokens=%d/%d cost=$%.6f",
        agent.technical_name, response.model,
        response.tokens_in, response.tokens_out, llm_cost,
    )

    # --- Log unified usage to Odoo ---
    try:
        from roomdoo_sdk.models import UsageRecord
        roomdoo_client = sdk_registry.get_client(instance)
        if roomdoo_client and tracker:
            log.info("Usage: %s", tracker.summary())
            await roomdoo_client.usage.log(UsageRecord(
                pms_property_id=routed_property_id,
                agent_id=agent.id,
                llm_account_id=agent.llm_account.id,
                tokens_in=tracker.tokens_in,
                tokens_out=tracker.tokens_out,
                model=tracker.llm_model or response.model,
                conversation_id=str(conversation_id),
                status="success",
                cost_usd=tracker.llm_cost_usd,
                whisper_seconds=tracker.whisper_seconds or None,
                whisper_cost_usd=tracker.whisper_cost_usd or None,
                vision_calls=tracker.vision_calls or None,
                vision_cost_usd=tracker.vision_cost_usd or None,
                total_cost_usd=tracker.total_cost_usd,
            ))
    except Exception as exc:
        log.warning("Failed to log usage to Odoo: %s", exc)

    # --- Send via WhatsApp ---
    wa_message_id: str | None = None
    try:
        wa_message_id = await wa_client.send_text(
            to=contact.phone_code,
            channel_endpoint=channel_endpoint,
            text=response.content,
        )
    except Exception as exc:
        log.error("Failed to send AI response via WhatsApp: %s", exc)
        # Still persist the message even if sending fails

    # --- Persist AI message ---
    delivery = DeliveryStatus.sent if wa_message_id else DeliveryStatus.failed
    msg = await message_repo.create(
        db,
        conversation_id=conversation_id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session_id,
        direction=MessageDirection.outbound,
        sender=MessageSender.ai,
        content=response.content,
        wa_message_id=wa_message_id,
        delivery_status=delivery,
    )
    await db.commit()

    # --- Socket.IO events ---
    try:
        await sio.emit(
            EVENT_MESSAGE_CREATED,
            build_message_created_payload(msg, contact),
            room=f"chat:{contact.phone_code}",
        )
        counts = await conversation_repo.get_unread_counts(
            db, [conversation_id], routed_property_id,
        )
        conversation = await conversation_repo.find_by_id(db, conversation_id)
        if conversation:
            await sio.emit(
                EVENT_CONVERSATION_UPDATED,
                build_conversation_payload(
                    conversation, contact,
                    last_message=msg,
                    unread_count=counts.get(conversation_id, 0),
                ),
                room=f"property:{routed_property_id}",
            )
    except Exception as exc:
        log.warning("Socket.IO emit for AI response failed: %s", exc)
