"""
AI response pipeline with supervisor as active orchestrator.

Every message goes through the supervisor. The supervisor decides
whether to respond directly, delegate to a worker, escalate, or
reassign to a different supervisor. After the worker responds,
the supervisor validates. If rejected, it retries with another worker.
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
from app.models.escalation import ESCALATION_PRIORITY  # noqa: F401
from app.models.instance import Instance, Property
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageSender
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
from app.services.supervisor_service import supervisor_orchestrate, supervisor_validate
from app.services.tool_executor import ConfirmationRequired, ToolExecutor
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("ai_response_service")

MAX_TOOL_ROUNDS = 10
MAX_WORKER_RETRIES = 2
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

    # --- 3. Quick rules (zero tokens, deterministic) ---
    from app.services.quick_rules import detect_human_request, check_quick_response

    if detect_human_request(message_content):
        await _create_escalation(
            db, sio, conversation_id, session,
            "manual", "Guest explicitly requested human intervention",
            message_content, routed_property_id,
        )
        return

    history_for_quick = await message_repo.find_recent_by_conversation(db, conversation_id, limit=2)
    last_ai = next((m.content for m in history_for_quick if m.sender == MessageSender.ai), None)
    quick = check_quick_response(message_content, last_ai, session.guest_language or "es")
    if quick:
        await _send_and_persist(
            quick, conversation_id, attention_session_id,
            channel_endpoint, contact, db, wa_client, sio,
            routed_property_id, tracker,
        )
        return

    # --- 4. Identify caller + detect language ---
    if not session.caller_type:
        session.caller_type = await identify_caller(
            contact.phone_code, sdk_registry, instance,
        )
        await db.flush()

    if not session.guest_language:
        from app.services.language_detector import detect_language
        detected = detect_language(message_content)
        if detected:
            session.guest_language = detected
            await db.flush()

    # --- 4. Load agents ---
    loader = await sdk_registry.get_or_load_agents(instance)
    if loader is None:
        return

    supervisor_name = SUPERVISOR_NAMES.get(session.caller_type, "supervisor-external")
    supervisor_entry = loader.get(supervisor_name)
    if not supervisor_entry or not supervisor_entry.config.llm_account:
        log.warning("Supervisor %s not available or has no LLM", supervisor_name)
        return

    workers = [
        c for c in loader.list_for_caller_type(session.caller_type or "external_guest")
        if not c.config.is_supervisor
    ]

    # --- 5. Supervisor orchestrates (ALWAYS) ---
    current_worker = loader.get_by_id(session.active_agent_id) if session.active_agent_id else None
    sup_result = await supervisor_orchestrate(
        supervisor_entry, message_content, workers,
        current_worker.config.technical_name if current_worker else None,
        llm_client,
    )

    # Handle reassign
    if sup_result.action == "reassign_supervisor" and sup_result.new_supervisor_name:
        new_sup = loader.get(sup_result.new_supervisor_name)
        if new_sup and new_sup.config.llm_account:
            supervisor_entry = new_sup
            sup_result = await supervisor_orchestrate(
                new_sup, message_content, workers,
                current_worker.config.technical_name if current_worker else None,
                llm_client,
            )

    # Handle respond directly
    if sup_result.action == "respond" and sup_result.response:
        await _send_and_persist(
            sup_result.response, conversation_id, attention_session_id,
            channel_endpoint, contact, db, wa_client, sio,
            routed_property_id, tracker,
        )
        return

    # Handle escalate
    if sup_result.action == "escalate":
        await _create_escalation(
            db, sio, conversation_id, session,
            sup_result.escalation_type or "manual",
            sup_result.escalation_reason or "Supervisor decided to escalate",
            message_content, routed_property_id,
        )
        return

    # --- 6. Delegate to worker ---
    if sup_result.action != "delegate" or not sup_result.worker_technical_name:
        return

    worker_entry = None
    for w in workers:
        if w.config.technical_name == sup_result.worker_technical_name:
            worker_entry = w
            break
    if not worker_entry and workers:
        worker_entry = workers[0]

    if not worker_entry:
        return

    session.active_agent_id = worker_entry.config.id

    # --- 7. Execute worker + validate loop ---
    for attempt in range(MAX_WORKER_RETRIES + 1):
        worker_response = await _run_worker(
            worker_entry, message_content, conversation_id,
            prop, instance, db, sdk_registry, llm_client,
            tracker, mcp_manager, channel_endpoint,
        )
        print(f"[PIPELINE] Worker {worker_entry.config.technical_name} response: {worker_response[:200] if worker_response else 'None'}")
        if worker_response is None:
            break

        # Supervisor validates
        validation = await supervisor_validate(
            supervisor_entry, worker_response, message_content,
            worker_entry.config.technical_name, workers, llm_client,
        )

        if validation.approved:
            await _send_and_persist(
                worker_response, conversation_id, attention_session_id,
                channel_endpoint, contact, db, wa_client, sio,
                routed_property_id, tracker,
            )
            session.active_agent_id = worker_entry.config.id
            await db.commit()
            return

        # Retry with different worker
        if validation.retry_with:
            next_worker = next(
                (w for w in workers if w.config.technical_name == validation.retry_with),
                None,
            )
            if next_worker:
                log.info("Supervisor retry: %s → %s", worker_entry.config.technical_name, validation.retry_with)
                worker_entry = next_worker
                continue

        # Escalate
        if validation.escalation_type:
            await _create_escalation(
                db, sio, conversation_id, session,
                validation.escalation_type,
                validation.escalation_reason or "Worker response rejected",
                message_content, routed_property_id,
            )
            return

    # Exhausted retries
    await _create_escalation(
        db, sio, conversation_id, session,
        "bad_response", "All workers failed after retries",
        message_content, routed_property_id,
    )


# ── Run worker (tool loop) ──────────────────────────────────────────

async def _run_worker(
    worker_entry, message_content, conversation_id,
    prop, instance, db, sdk_registry, llm_client,
    tracker, mcp_manager, channel_endpoint,
) -> str | None:
    """Execute a worker agent with tool loop. Returns response text or None."""
    agent = worker_entry.config
    docs = worker_entry.documents

    if not agent.llm_account or not agent.llm_account.api_key or not agent.effective_model:
        return None

    provider = agent.llm_account.provider
    api_key = agent.llm_account.api_key
    api_base_url = agent.llm_account.api_base_url
    model = agent.effective_model

    if agent.sensitive_data and settings.ollama_url:
        provider = "ollama"
        api_key = ""
        api_base_url = settings.ollama_url

    roomdoo_client = sdk_registry.get_client(instance)
    tool_executor = (
        ToolExecutor(roomdoo_client, mcp_manager, instance.id)
        if roomdoo_client else None
    )
    llm_tools = None
    if tool_executor and (agent.tools or agent.god_mode):
        llm_tools = tool_executor.build_llm_tools(agent)
    print(f"[WORKER] agent={agent.technical_name} god={agent.god_mode} tools={len(llm_tools or [])} model={model}")

    history = await message_repo.find_recent_by_conversation(db, conversation_id, limit=20)
    history.reverse()

    prompt_messages = build_prompt(
        agent=agent, docs=docs,
        conversation_history=history,
        current_message=message_content,
        property_name=prop.name,
        tools=llm_tools,
    )

    llm_messages = [{"role": m.role, "content": m.content} for m in prompt_messages]
    total_in = total_out = 0

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            response = await llm_client.chat(
                messages=llm_messages, provider=provider, api_key=api_key,
                model=model, api_base_url=api_base_url,
                temperature=agent.temperature, max_tokens=agent.max_tokens,
                tools=llm_tools,
            )
        except LLMClientError as exc:
            print(f"[WORKER] LLM ERROR round={round_num}: {exc}")
            return None
        except Exception as exc:
            print(f"[WORKER] UNEXPECTED ERROR round={round_num}: {exc}")
            return None

        total_in += response.tokens_in
        total_out += response.tokens_out

        print(f"[WORKER] round={round_num} finish={response.finish_reason} tool_calls={len(response.tool_calls or [])} content={response.content[:100] if response.content else 'None'}")
        if response.finish_reason != "tool_calls" or not response.tool_calls:
            # Track costs
            if tracker:
                cost = 0.0
                try:
                    from litellm import cost_per_token
                    p, c = cost_per_token(response.model, prompt_tokens=total_in, completion_tokens=total_out)
                    cost = p + c
                except Exception:
                    pass
                tracker.add_llm(total_in, total_out, cost, response.model)

            # Log usage
            try:
                from roomdoo_sdk.models import UsageRecord
                if roomdoo_client and tracker:
                    await roomdoo_client.usage.log(UsageRecord(
                        pms_property_id=prop.id,
                        agent_id=agent.id,
                        llm_account_id=agent.llm_account.id,
                        tokens_in=tracker.tokens_in,
                        tokens_out=tracker.tokens_out,
                        model=tracker.llm_model or model,
                        conversation_id=str(conversation_id),
                        status="success",
                        cost_usd=tracker.llm_cost_usd,
                        total_cost_usd=tracker.total_cost_usd,
                    ))
            except Exception:
                pass

            return response.content

        # Tool calls
        assistant_msg = {"role": "assistant", "content": response.content or ""}
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": {
                    "name": tc["function"]["name"],
                    "arguments": json.dumps(tc["function"]["arguments"])
                    if isinstance(tc["function"]["arguments"], dict)
                    else tc["function"]["arguments"],
                }} for tc in response.tool_calls
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
            tool_result = await _execute_tool(fn_name, fn_args, agent, tool_executor, roomdoo_client, conversation_id)
            llm_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(tool_result, default=str)})

    return None


# ── Helpers ──────────────────────────────────────────────────────────

GOD_MODE_TOOL_NAMES = {"odoo_list_models", "odoo_get_fields", "odoo_search_read", "odoo_write"}

async def _execute_tool(fn_name, fn_args, agent, tool_executor, roomdoo_client, conversation_id):
    if fn_name in GOD_MODE_TOOL_NAMES and agent.god_mode and tool_executor:
        try:
            result = await tool_executor.execute_god_mode(fn_name, fn_args)
            if roomdoo_client:
                from app.services.audit_service import log_audit
                await log_audit(
                    roomdoo_client, agent.id, fn_name,
                    fn_args.get("model_name", ""), fn_name,
                    conversation_id, fn_args.get("ids"),
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


async def _create_escalation(db, sio, conversation_id, session, escalation_type, reason, guest_message, property_id):
    esc = await escalation_repo.create(
        db, conversation_id=conversation_id, session_id=session.id,
        escalation_type=escalation_type, reason=reason,
        guest_message=guest_message, ai_was_enabled=session.ai_enabled,
    )
    session.ai_enabled = False
    await db.commit()
    log.info("Escalation: conv=%d type=%s reason=%s", conversation_id, escalation_type, reason)
    try:
        await sio.emit("escalation.created", {
            "conversation_id": conversation_id, "escalation_id": esc.id,
            "type": escalation_type, "reason": reason,
            "priority": ESCALATION_PRIORITY.get(escalation_type, 1),
        }, room=f"property:{property_id}")
    except Exception:
        pass


def _fragment_message(text: str, max_len: int = 1000) -> list[str]:
    """Split a long message into fragments at paragraph boundaries."""
    if len(text) <= max_len:
        return [text]

    fragments = []
    current = ""
    for paragraph in text.split("\n\n"):
        if current and len(current) + len(paragraph) + 2 > max_len:
            fragments.append(current.strip())
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph

    if current.strip():
        fragments.append(current.strip())

    # If a single paragraph is too long, split by newlines
    result = []
    for frag in fragments:
        if len(frag) <= max_len:
            result.append(frag)
        else:
            sub = ""
            for line in frag.split("\n"):
                if sub and len(sub) + len(line) + 1 > max_len:
                    result.append(sub.strip())
                    sub = line
                else:
                    sub = f"{sub}\n{line}" if sub else line
            if sub.strip():
                result.append(sub.strip())

    return result if result else [text]


async def _send_and_persist(content, conversation_id, attention_session_id, channel_endpoint, contact, db, wa_client, sio, routed_property_id, tracker):
    import asyncio

    fragments = _fragment_message(content)
    last_wa_id = None
    last_msg = None

    for i, fragment in enumerate(fragments):
        wa_message_id = None
        try:
            wa_message_id = await wa_client.send_text(
                to=contact.phone_code, channel_endpoint=channel_endpoint, text=fragment,
            )
            last_wa_id = wa_message_id
        except Exception as exc:
            log.error("Failed to send fragment %d: %s", i, exc)

        last_msg = await message_repo.create(
            db, conversation_id=conversation_id, channel_endpoint_id=channel_endpoint.id,
            attention_session_id=attention_session_id, direction=MessageDirection.outbound,
            sender=MessageSender.ai, content=fragment, wa_message_id=wa_message_id,
            delivery_status=DeliveryStatus.sent if wa_message_id else DeliveryStatus.failed,
        )

        # Small delay between fragments for correct ordering on the phone
        if i < len(fragments) - 1:
            await asyncio.sleep(1.5)

    await db.commit()
    msg = last_msg

    try:
        await sio.emit(EVENT_MESSAGE_CREATED, build_message_created_payload(msg, contact), room=f"chat:{contact.phone_code}")
        counts = await conversation_repo.get_unread_counts(db, [conversation_id], routed_property_id)
        conversation = await conversation_repo.find_by_id(db, conversation_id)
        if conversation:
            await sio.emit(EVENT_CONVERSATION_UPDATED, build_conversation_payload(
                conversation, contact, last_message=msg, unread_count=counts.get(conversation_id, 0),
            ), room=f"property:{routed_property_id}")
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)
