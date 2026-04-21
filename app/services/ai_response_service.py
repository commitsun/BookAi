"""
AI response pipeline with supervisor pattern.

Full sequence:
1. Check escalation pending → attach message, don't respond
2. Identify caller type (cached in session)
3. Resolve supervisor agent (supervisor-external/internal/roomdoo)
4. Supervisor evaluates message (with cooldown) → delegate/escalate/respond
5. Worker agent processes message with tools
6. Supervisor validates output (with cooldown)
7. Send response via channel
8. Log usage
"""

import json
import logging

import socketio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.escalation import Escalation, ESCALATION_PRIORITY
from app.models.instance import Instance, Property
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageKind, MessageSender
from app.models.session import AttentionSession
from app.repositories import conversation_repo, escalation_repo, message_repo
from app.realtime.events import (
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.services.caller_identifier import identify_caller
from app.services.context_builder import build_prompt
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMClientError, LLMProvider
from app.services.supervisor_service import (
    SupervisorDecision,
    run_supervisor,
    should_supervise,
    validate_output,
)
from app.services.tool_executor import ConfirmationRequired, ToolExecutor
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("ai_response_service")

MAX_TOOL_ROUNDS = 5
SUPERVISOR_NAMES = {
    "external_guest": "supervisor-external",
    "internal": "supervisor-internal",
    "roomdoo": "supervisor-roomdoo",
}


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
    tracker=None,
    mcp_manager=None,
) -> None:
    """Fire-and-forget safe entry point."""
    try:
        await _pipeline(
            conversation_id, message_content, attention_session_id,
            routed_property_id, channel_endpoint, contact,
            db, wa_client, sio, sdk_registry, llm_client, tracker,
            mcp_manager,
        )
    except Exception as exc:
        log.error(
            "AI response failed for conversation=%d: %s",
            conversation_id, exc, exc_info=True,
        )


async def _pipeline(
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
    mcp_manager=None,
) -> None:
    # --- 1. Load property + session ---
    result = await db.execute(
        select(Property)
        .options(selectinload(Property.instance))
        .where(Property.id == routed_property_id)
    )
    prop = result.scalar_one_or_none()
    if not prop or not prop.ai_enabled:
        return

    session = await db.get(AttentionSession, attention_session_id)
    if not session or not session.ai_enabled:
        return

    instance: Instance = prop.instance

    # --- 2. Check pending escalation ---
    pending = await escalation_repo.find_pending_for_session(db, session.id)
    if pending:
        # Attach guest message to the escalation, don't respond
        esc = pending[0]
        await message_repo.create(
            db,
            conversation_id=conversation_id,
            channel_endpoint_id=channel_endpoint.id,
            attention_session_id=attention_session_id,
            escalation_id=esc.id,
            direction=MessageDirection.inbound,
            sender=MessageSender.guest,
            content=message_content,
            delivery_status=DeliveryStatus.delivered,
        )
        await db.commit()
        return

    # --- 3. Identify caller (cached in session) ---
    if not session.caller_type:
        session.caller_type = await identify_caller(
            contact.phone_code, sdk_registry, instance,
        )
        await db.flush()

    # --- 4. Load agents ---
    loader = await sdk_registry.get_or_load_agents(instance)
    if loader is None:
        return

    # Count messages in session for cooldown
    msg_count_result = await db.execute(
        select(func.count(Message.id)).where(
            Message.attention_session_id == session.id,
            Message.escalation_id.is_(None),
        )
    )
    message_count = msg_count_result.scalar() or 0

    # --- 5. Supervisor evaluation (with cooldown) ---
    supervisor_name = SUPERVISOR_NAMES.get(session.caller_type, "supervisor-external")
    supervisor_entry = loader.get(supervisor_name)

    # Get available workers for this caller type
    workers = [
        c for c in loader.list_for_caller_type(session.caller_type or "external_guest")
        if not c.config.is_supervisor
    ]

    if supervisor_entry and should_supervise(session, message_count):
        current_worker = loader.get_by_id(session.active_agent_id) if session.active_agent_id else None
        decision = await run_supervisor(
            supervisor_entry, message_content, workers,
            current_worker.config.technical_name if current_worker else None,
            llm_client,
        )

        if decision.action == "escalate":
            await _create_escalation(
                db, sio, conversation_id, session,
                decision.escalation_type or "manual",
                decision.escalation_reason or "Supervisor decided to escalate",
                message_content, routed_property_id,
            )
            return

        if decision.action == "delegate" and decision.worker_id:
            session.active_agent_id = decision.worker_id

        if decision.action == "respond_direct" and decision.direct_response:
            await _send_and_persist(
                decision.direct_response, conversation_id, attention_session_id,
                channel_endpoint, contact, db, wa_client, sio,
                routed_property_id, tracker,
            )
            return

    # --- 6. Resolve worker agent ---
    worker_entry = None
    if session.active_agent_id:
        worker_entry = loader.get_by_id(session.active_agent_id)
    if not worker_entry and workers:
        worker_entry = workers[0]
        session.active_agent_id = worker_entry.config.id

    if not worker_entry:
        log.warning("No worker agent available for conversation %d", conversation_id)
        return

    agent = worker_entry.config
    docs = worker_entry.documents

    if not agent.llm_account or not agent.llm_account.api_key:
        return
    model = agent.effective_model
    if not model:
        return

    # --- 7. Provider selection (sensitive_data → ollama) ---
    provider = agent.llm_account.provider
    api_key = agent.llm_account.api_key
    api_base_url = agent.llm_account.api_base_url
    if agent.sensitive_data and settings.ollama_url:
        provider = "ollama"
        api_key = ""
        api_base_url = settings.ollama_url

    # --- 8. Build tools + prompt ---
    roomdoo_client = sdk_registry.get_client(instance)
    tool_executor = (
        ToolExecutor(roomdoo_client, mcp_manager, instance.id)
        if roomdoo_client else None
    )
    llm_tools = None
    if tool_executor and (agent.tools or agent.god_mode):
        llm_tools = tool_executor.build_llm_tools(agent)

    history = await message_repo.find_recent_by_conversation(
        db, conversation_id, limit=20,
    )
    history.reverse()

    prompt_messages = build_prompt(
        agent=agent, docs=docs,
        conversation_history=history,
        current_message=message_content,
        property_name=prop.name,
        tools=llm_tools,
    )

    # --- 9. LLM call with tool execution loop ---
    llm_messages = [{"role": m.role, "content": m.content} for m in prompt_messages]
    final_content = None
    total_tokens_in = 0
    total_tokens_out = 0

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            response = await llm_client.chat(
                messages=llm_messages,
                provider=provider,
                api_key=api_key,
                model=model,
                api_base_url=api_base_url,
                temperature=agent.temperature,
                max_tokens=agent.max_tokens,
                tools=llm_tools,
            )
        except LLMClientError as exc:
            log.error("LLM call failed (round %d): %s", round_num, exc)
            return

        total_tokens_in += response.tokens_in
        total_tokens_out += response.tokens_out

        if response.finish_reason != "tool_calls" or not response.tool_calls:
            final_content = response.content
            break

        # Process tool calls
        assistant_msg = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": (
                            json.dumps(tc["function"]["arguments"])
                            if isinstance(tc["function"]["arguments"], dict)
                            else tc["function"]["arguments"]
                        ),
                    },
                }
                for tc in response.tool_calls
            ]
        llm_messages.append(assistant_msg)

        for tc in response.tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = tc["function"]["arguments"]
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except json.JSONDecodeError:
                    fn_args = {}

            tool_result = await _execute_tool(
                fn_name, fn_args, agent, tool_executor, roomdoo_client,
                conversation_id,
            )

            llm_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, default=str),
            })

    if not final_content:
        return

    # --- 10. Supervisor output validation (with cooldown) ---
    if supervisor_entry and should_supervise(session, message_count):
        validation = await validate_output(
            supervisor_entry, final_content, message_content, llm_client,
        )
        if not validation.approved:
            await _create_escalation(
                db, sio, conversation_id, session,
                validation.escalation_type or "bad_response",
                validation.escalation_reason or "Response rejected by supervisor",
                message_content, routed_property_id,
            )
            return

    # --- 11. Track costs ---
    llm_cost = 0.0
    try:
        from litellm import cost_per_token
        p, c = cost_per_token(
            response.model,
            prompt_tokens=total_tokens_in,
            completion_tokens=total_tokens_out,
        )
        llm_cost = p + c
    except Exception:
        pass

    if tracker:
        tracker.add_llm(total_tokens_in, total_tokens_out, llm_cost, response.model)

    # --- 12. Log usage ---
    try:
        from roomdoo_sdk.models import UsageRecord
        if roomdoo_client and tracker:
            await roomdoo_client.usage.log(UsageRecord(
                pms_property_id=routed_property_id,
                agent_id=agent.id,
                llm_account_id=agent.llm_account.id,
                tokens_in=tracker.tokens_in,
                tokens_out=tracker.tokens_out,
                model=tracker.llm_model or model,
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
        log.warning("Failed to log usage: %s", exc)

    # --- 13. Send + persist + events ---
    await _send_and_persist(
        final_content, conversation_id, attention_session_id,
        channel_endpoint, contact, db, wa_client, sio,
        routed_property_id, tracker,
    )

    # Pin agent to session
    if session.active_agent_id != agent.id:
        session.active_agent_id = agent.id
        await db.commit()


# ── Helpers ──────────────────────────────────────────────────────────

async def _execute_tool(fn_name, fn_args, agent, tool_executor, roomdoo_client, conversation_id):
    """Execute a tool call (SDK, MCP, or god mode)."""
    if fn_name == "odoo_execute" and agent.god_mode and tool_executor:
        try:
            result = await tool_executor.execute_god_mode(
                fn_args.get("model_name", ""),
                fn_args.get("method", "search_read"),
                fn_args,
            )
            if roomdoo_client:
                from app.services.audit_service import log_audit
                await log_audit(
                    roomdoo_client, agent.id,
                    fn_args.get("method", "search_read"),
                    fn_args.get("model_name", ""),
                    fn_args.get("method", ""),
                    conversation_id,
                    fn_args.get("ids"),
                    json.dumps(fn_args)[:500],
                )
            return result
        except ConfirmationRequired as cr:
            return {"status": "confirmation_required", "message": str(cr)}
        except Exception as exc:
            return {"error": str(exc)}
    elif tool_executor:
        try:
            return await tool_executor.execute(fn_name, fn_args, agent)
        except ConfirmationRequired as cr:
            return {"status": "confirmation_required", "message": str(cr)}
        except Exception as exc:
            return {"error": str(exc)}
    return {"error": "Tool execution not available"}


async def _create_escalation(
    db, sio, conversation_id, session,
    escalation_type, reason, guest_message, property_id,
):
    """Create an escalation and disable AI."""
    esc = await escalation_repo.create(
        db,
        conversation_id=conversation_id,
        session_id=session.id,
        escalation_type=escalation_type,
        reason=reason,
        guest_message=guest_message,
        ai_was_enabled=session.ai_enabled,
    )
    session.ai_enabled = False
    await db.commit()

    log.info(
        "Escalation created: conv=%d type=%s reason=%s",
        conversation_id, escalation_type, reason,
    )

    try:
        await sio.emit(
            "escalation.created",
            {
                "conversation_id": conversation_id,
                "escalation_id": esc.id,
                "type": escalation_type,
                "reason": reason,
                "priority": ESCALATION_PRIORITY.get(escalation_type, 1),
            },
            room=f"property:{property_id}",
        )
    except Exception:
        pass


async def _send_and_persist(
    content, conversation_id, attention_session_id,
    channel_endpoint, contact, db, wa_client, sio,
    routed_property_id, tracker,
):
    """Send response via WhatsApp, persist, emit Socket.IO."""
    wa_message_id = None
    try:
        wa_message_id = await wa_client.send_text(
            to=contact.phone_code,
            channel_endpoint=channel_endpoint,
            text=content,
        )
    except Exception as exc:
        log.error("Failed to send AI response: %s", exc)

    delivery = DeliveryStatus.sent if wa_message_id else DeliveryStatus.failed
    msg = await message_repo.create(
        db,
        conversation_id=conversation_id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session_id,
        direction=MessageDirection.outbound,
        sender=MessageSender.ai,
        content=content,
        wa_message_id=wa_message_id,
        delivery_status=delivery,
    )
    await db.commit()

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
        log.warning("Socket.IO emit failed: %s", exc)
