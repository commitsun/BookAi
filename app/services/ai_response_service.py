"""
AI response pipeline with supervisor as active orchestrator.


Every message goes through the supervisor. The supervisor decides
whether to respond directly, delegate to a worker, escalate, or
reassign to a different supervisor. After the worker responds,
the supervisor validates. If rejected, it retries with another worker.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

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
from app.models.folio import SessionFolio
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
SUPERVISOR_HISTORY_LIMIT = 10


@dataclass
class WorkerResult:
    response: str | None
    tools_used: list[str] = field(default_factory=list)
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

    session_result = await db.execute(
        select(AttentionSession)
        .where(AttentionSession.id == attention_session_id)
        .options(
            selectinload(AttentionSession.session_folios)
            .selectinload(SessionFolio.folio)
        )
    )
    session = session_result.scalar_one_or_none()
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

    human_requested = detect_human_request(message_content)

    history_for_quick = await message_repo.find_recent_by_conversation(db, conversation_id, limit=2)
    last_ai = next((m.content for m in history_for_quick if m.sender == MessageSender.ai), None)
    quick = check_quick_response(message_content, last_ai, session.guest_language or "es")
    if quick:
        await _send_and_persist(
            quick, conversation_id, attention_session_id,
            channel_endpoint, contact, db, wa_client, sio,
            routed_property_id, tracker, ai_enabled=session.ai_enabled,
        )
        return

    # --- 4. Identify caller + detect language ---
    if not session.caller_type:
        identity = await identify_caller(
            contact.phone_code, sdk_registry, instance,
        )
        session.caller_type = identity.caller_type
        session.odoo_user_id = identity.odoo_user_id
        # Mark conversation type for internal/roomdoo (separate from guest inbox)
        if identity.caller_type in ("internal", "roomdoo"):
            from app.models.conversation import Conversation
            conv = await db.get(Conversation, conversation_id)
            if conv and conv.conversation_type == "guest":
                conv.conversation_type = identity.caller_type
                if identity.odoo_user_id:
                    conv.odoo_user_id = identity.odoo_user_id
        await db.flush()

    from app.services.language_detector import detect_language, SUPPORTED_LANGUAGES
    if not session.guest_language or session.guest_language not in SUPPORTED_LANGUAGES:
        detected = detect_language(message_content)
        if detected:
            session.guest_language = detected
            await db.flush()

    # --- 4. Load agents ---
    loader = await sdk_registry.get_or_load_agents(instance, db)
    if loader is None:
        return

    caller = session.caller_type or "external_guest"
    supervisor_name = SUPERVISOR_NAMES.get(caller, "supervisor-external")
    supervisor_entry = loader.get(supervisor_name)
    if not supervisor_entry or not supervisor_entry.config.llm_account:
        log.warning("Supervisor %s not available or has no LLM", supervisor_name)
        return

    # Permission-filtered worker list
    from app.repositories import agent_repo

    permitted = await agent_repo.find_permitted_workers(
        db, instance.id, caller,
        prop.odoo_property_id, session.odoo_user_id,
        supervisor_name,
    )
    permitted_names: set[str] | None = {a.technical_name for a in permitted}

    # Fallback if agents table is empty (first boot, migration not yet synced)
    if not permitted_names:
        total = await agent_repo.count_for_instance(db, instance.id)
        if total == 0:
            log.warning("No agents in DB for instance %d — unfiltered fallback", instance.id)
            permitted_names = None

    workers = [
        c for c in loader.list_for_caller_type(caller)
        if not c.config.is_supervisor
        and (permitted_names is None or c.config.technical_name in permitted_names)
    ]

    # --- 5. Supervisor orchestrates (ALWAYS) ---
    # Load recent history so the supervisor has conversation context
    sup_history = await message_repo.find_recent_by_conversation(db, conversation_id, limit=SUPERVISOR_HISTORY_LIMIT)
    sup_history.reverse()  # chronological order

    # Deferred human-request escalation (needs supervisor for handoff message)
    if human_requested:
        await _send_handoff(
            "manual", "Guest explicitly requested human intervention",
            message_content, prop, session, llm_client,
            supervisor_entry, sup_history,
            conversation_id, attention_session_id, channel_endpoint,
            contact, db, wa_client, sio, routed_property_id, tracker,
        )
        await _create_escalation(
            db, sio, conversation_id, session,
            "manual", "Guest explicitly requested human intervention",
            message_content, routed_property_id,
        )
        return

    current_worker = loader.get_by_id(session.active_agent_id) if session.active_agent_id else None
    sup_result = await supervisor_orchestrate(
        supervisor_entry, message_content, workers,
        current_worker.config.technical_name if current_worker else None,
        llm_client,
        conversation_history=sup_history,
    )

    # Handle reassign
    if sup_result.action == "reassign_supervisor" and sup_result.new_supervisor_name:
        new_sup = loader.get(sup_result.new_supervisor_name)
        if new_sup and new_sup.config.llm_account:
            supervisor_entry = new_sup
            # Re-filter workers for new supervisor's delegation rules
            permitted = await agent_repo.find_permitted_workers(
                db, instance.id, caller,
                prop.odoo_property_id, session.odoo_user_id,
                new_sup.config.technical_name,
            )
            permitted_names = {a.technical_name for a in permitted} or None
            workers = [
                c for c in loader.list_for_caller_type(caller)
                if not c.config.is_supervisor
                and (permitted_names is None or c.config.technical_name in permitted_names)
            ]
            sup_result = await supervisor_orchestrate(
                new_sup, message_content, workers,
                current_worker.config.technical_name if current_worker else None,
                llm_client,
                conversation_history=sup_history,
            )

    # Handle respond directly
    if sup_result.action == "respond" and sup_result.response:
        await _send_and_persist(
            sup_result.response, conversation_id, attention_session_id,
            channel_endpoint, contact, db, wa_client, sio,
            routed_property_id, tracker, ai_enabled=session.ai_enabled,
        )
        return

    # Handle escalate
    if sup_result.action == "escalate":
        await _send_handoff(
            sup_result.escalation_type or "manual",
            sup_result.escalation_reason or "Supervisor decided to escalate",
            message_content, prop, session, llm_client,
            supervisor_entry, sup_history,
            conversation_id, attention_session_id, channel_endpoint,
            contact, db, wa_client, sio, routed_property_id, tracker,
        )
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

    # --- 7. Compute effective policies (supervisor × worker) ---
    from app.services.execution_policy import (
        resolve_effective_role,
        resolve_effective_confirmation,
        resolve_effective_log_level,
    )
    from app.services.execution_service import (
        create_execution, complete_execution, fail_execution, log_step,
    )

    effective_role = resolve_effective_role(
        supervisor_entry.config.execution_role,
        worker_entry.config.execution_role,
    )
    effective_confirm = resolve_effective_confirmation(
        supervisor_entry.config.confirmation_policy,
        worker_entry.config.confirmation_policy,
    )
    effective_log = resolve_effective_log_level(
        supervisor_entry.config.log_level,
        worker_entry.config.log_level,
    )

    # --- 8. Create execution + log delegation ---
    roomdoo_client = sdk_registry.get_client(instance)
    execution_id = None
    if roomdoo_client:
        execution_id = await create_execution(
            roomdoo_client, worker_entry.config.id,
            prop.odoo_property_id, conversation_id,
            caller_info=contact.phone_code if contact else "",
            effective_role=effective_role,
            effective_confirmation=effective_confirm,
            effective_log_level=effective_log,
        )
        if execution_id:
            await log_step(
                roomdoo_client, execution_id, "delegation",
                supervisor_entry.config.id, effective_role, effective_log,
                description=f"Delegated to {worker_entry.config.technical_name}",
                delegated_agent_id=worker_entry.config.id,
            )

    # --- 9. Execute worker + validate loop ---
    for attempt in range(MAX_WORKER_RETRIES + 1):
        result = await _run_worker(
            worker_entry, message_content, conversation_id,
            prop, instance, db, sdk_registry, llm_client,
            tracker, mcp_manager, channel_endpoint, session, contact,
            execution_id=execution_id,
            effective_role=effective_role,
            effective_confirm=effective_confirm,
            effective_log=effective_log,
        )
        worker_response = result.response
        log.info("Worker %s response: %s", worker_entry.config.technical_name, worker_response[:200] if worker_response else "None")
        if worker_response is None:
            if execution_id and roomdoo_client:
                await fail_execution(roomdoo_client, execution_id, "Worker returned None")
            break

        # Supervisor validates
        validation = await supervisor_validate(
            supervisor_entry, worker_response, message_content,
            worker_entry.config.technical_name, workers, llm_client,
            tools_used=result.tools_used,
            has_folio_context=bool(session and session.session_folios),
        )

        if validation.approved:
            await _send_and_persist(
                worker_response, conversation_id, attention_session_id,
                channel_endpoint, contact, db, wa_client, sio,
                routed_property_id, tracker, ai_enabled=session.ai_enabled,
            )
            session.active_agent_id = worker_entry.config.id
            await db.commit()
            if execution_id and roomdoo_client:
                await complete_execution(roomdoo_client, execution_id, worker_response[:500])
            # Auto-generate title for internal conversations
            asyncio.create_task(
                _maybe_generate_title(
                    conversation_id, message_content, llm_client, sio,
                )
            )
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
            if execution_id and roomdoo_client:
                await fail_execution(roomdoo_client, execution_id, f"Escalated: {validation.escalation_reason}")
            await _send_handoff(
                validation.escalation_type,
                validation.escalation_reason or "Worker response rejected",
                message_content, prop, session, llm_client,
                supervisor_entry, sup_history,
                conversation_id, attention_session_id, channel_endpoint,
                contact, db, wa_client, sio, routed_property_id, tracker,
            )
            await _create_escalation(
                db, sio, conversation_id, session,
                validation.escalation_type,
                validation.escalation_reason or "Worker response rejected",
                message_content, routed_property_id,
            )
            return

    # Exhausted retries
    if execution_id and roomdoo_client:
        await fail_execution(roomdoo_client, execution_id, "All workers failed after retries")
    await _send_handoff(
        "bad_response", "All workers failed after retries",
        message_content, prop, session, llm_client,
        supervisor_entry, sup_history,
        conversation_id, attention_session_id, channel_endpoint,
        contact, db, wa_client, sio, routed_property_id, tracker,
    )
    await _create_escalation(
        db, sio, conversation_id, session,
        "bad_response", "All workers failed after retries",
        message_content, routed_property_id,
    )


# ── Run worker (tool loop) ──────────────────────────────────────────

async def _run_worker(
    worker_entry, message_content, conversation_id,
    prop, instance, db, sdk_registry, llm_client,
    tracker, mcp_manager, channel_endpoint, session=None, contact=None,
    execution_id: int | None = None,
    effective_role: str = "assistant",
    effective_confirm: str = "sensitive",
    effective_log: str = "basic",
) -> WorkerResult:
    """Execute a worker agent with tool loop. Returns WorkerResult."""
    agent = worker_entry.config
    docs = worker_entry.documents

    if not agent.llm_account or not agent.llm_account.api_key or not agent.effective_model:
        return WorkerResult(None)

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
        llm_tools = tool_executor.build_llm_tools(agent, effective_role=effective_role)

    # Defense-in-depth: strip god_mode tools for external guests
    caller = session.caller_type if session else None
    if agent.god_mode and caller == "external_guest":
        log.warning("god_mode agent %s blocked for external_guest — stripping tools", agent.technical_name)
        llm_tools = None

    log.info("Worker %s: god=%s tools=%d model=%s", agent.technical_name, agent.god_mode, len(llm_tools or []), model)

    history = await message_repo.find_recent_by_conversation(db, conversation_id, limit=20)
    history.reverse()

    # Build property context from SDK (cached per request)
    property_context = None
    if roomdoo_client and prop.odoo_property_id:
        try:
            pdata = await roomdoo_client.properties.get(prop.odoo_property_id)
            property_context = {
                "property_id": prop.odoo_property_id,
                "Hotel name": pdata.name,
                "Address": f"{pdata.street or ''}, {pdata.city or ''}".strip(", "),
                "Country": pdata.country_name or "",
                "Phone": pdata.phone or prop.phone or "",
                "Email": pdata.email or prop.email or "",
                "Timezone": pdata.tz or prop.tz or "",
            }
            if pdata.bookai_sale_channel_id:
                property_context["sale_channel_id (BookAI)"] = pdata.bookai_sale_channel_id
        except Exception:
            pass

        # Load pricelists and room types — IDs critical for SDK tool calls
        try:
            pricelists = await roomdoo_client.properties.get_pricelists(prop.odoo_property_id)
            if pricelists and property_context is not None:
                pl_summary = [
                    f"{pl.name} (pricelist_id={pl.id})"
                    for pl in pricelists
                ]
                property_context["Available pricelists"] = ", ".join(pl_summary)
        except Exception:
            pass

        try:
            room_types = await roomdoo_client.properties.get_room_types(prop.odoo_property_id)
            if room_types and property_context is not None:
                rt_summary = [
                    f"{rt.name} (room_type_id={rt.id})"
                    for rt in room_types
                ]
                property_context["Room types"] = ", ".join(rt_summary)
        except Exception:
            pass

    if not property_context:
        property_context = {
            "property_id": prop.odoo_property_id,
            "Hotel name": prop.name,
            "Phone": prop.phone or "",
            "Email": prop.email or "",
        }

    # Build folio context from session
    folio_context = None
    if session and session.session_folios:
        folio_lines = []
        for sf in session.session_folios:
            f = sf.folio
            if not f:
                continue
            # Show both the Odoo ID and the display code for the LLM
            odoo_code = f.odoo_external_code or ""
            parts = [f"Code: {odoo_code}"]
            if f.odoo_folio_id:
                parts.append(f"Odoo ID: {f.odoo_folio_id}")
            if f.checkin_date:
                parts.append(f"Check-in: {f.checkin_date}")
            if f.checkout_date:
                parts.append(f"Check-out: {f.checkout_date}")
            if f.status:
                parts.append(f"Status: {f.status.value}")
            if f.pending_payment_amount:
                currency = f.pending_payment_currency or "EUR"
                parts.append(f"Pending: {f.pending_payment_amount} {currency}")
            folio_lines.append(" | ".join(parts))
        if folio_lines:
            folio_context = folio_lines

    # Build guest context from contact
    guest_context = None
    if contact:
        guest_context = {
            "Name": contact.display_name or "",
            "Phone": contact.phone_code or "",
        }

    prompt_messages = build_prompt(
        agent=agent, docs=docs,
        conversation_history=history,
        current_message=message_content,
        property_name=prop.name,
        tools=llm_tools,
        property_context=property_context,
        folio_context=folio_context,
        guest_context=guest_context,
        worker_context=session.worker_context if session else None,
    )

    llm_messages = [{"role": m.role, "content": m.content} for m in prompt_messages]
    total_in = total_out = 0
    tool_results_log: list[dict] = []  # Collected for worker_context
    tools_used: list[str] = []  # Track tool names for supervisor validation
    pending_audit_id: int | None = None  # Track audit awaiting confirmation summary

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
            return WorkerResult(None, tools_used)
        except Exception as exc:
            print(f"[WORKER] UNEXPECTED ERROR round={round_num}: {exc}")
            return WorkerResult(None, tools_used)

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
                        pms_property_id=prop.odoo_property_id,
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
            except Exception as exc:
                log.warning("Failed to log usage to Odoo: %s", exc)

            # Save confirmation summary to audit log
            if pending_audit_id and response.content and roomdoo_client:
                from app.services.audit_service import update_audit_status
                await update_audit_status(
                    roomdoo_client, pending_audit_id, "pending",
                    confirmation_summary=response.content[:1000],
                )

            # Save tool results to session for next worker
            if tool_results_log and session:
                ctx = session.worker_context or {}
                existing = ctx.get("tool_results", [])
                existing.extend(tool_results_log)
                ctx["tool_results"] = existing
                session.worker_context = ctx
                await db.flush()

            return WorkerResult(response.content, tools_used)

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
            # Security: force caller's phone on my_* tools for external_guest
            caller_type = session.caller_type if session else None
            if caller_type == "external_guest" and contact and isinstance(fn_args, dict):
                if "phone" in fn_args:
                    fn_args["phone"] = contact.phone_code

            tool_result = await _execute_tool(
                fn_name, fn_args, agent, tool_executor,
                roomdoo_client, conversation_id, message_content,
                effective_confirm=effective_confirm,
                execution_id=execution_id,
                effective_role=effective_role,
                effective_log=effective_log,
            )
            result_str = json.dumps(tool_result, default=str)
            log.info("Tool %s(%s) → %s", fn_name, json.dumps(fn_args, default=str)[:200], result_str[:500])
            llm_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_str})

            # Classify result and log step — always track the tool name
            canonical = fn_name.replace("__", ".")
            if canonical not in tools_used:
                tools_used.append(canonical)

            if isinstance(tool_result, dict):
                if tool_result.get("status") == "confirmation_required":
                    pending_audit_id = tool_result.get("audit_id")
                    if execution_id and roomdoo_client:
                        from app.services.execution_service import log_step
                        await log_step(
                            roomdoo_client, execution_id, "confirmation",
                            agent.id, effective_role, effective_log,
                            tool_name=fn_name, tool_args=fn_args,
                            status="pending",
                            description=f"Confirmation requested for {fn_name}",
                        )
                elif "error" in tool_result:
                    if execution_id and roomdoo_client:
                        from app.services.execution_service import log_step
                        await log_step(
                            roomdoo_client, execution_id, "error",
                            agent.id, effective_role, effective_log,
                            tool_name=fn_name, tool_args=fn_args,
                            tool_result=tool_result,
                            status="error",
                            description=tool_result.get("error", "")[:200],
                        )
                else:
                    tool_results_log.append({"tool": fn_name, "result": tool_result})
                    if execution_id and roomdoo_client:
                        from app.services.execution_service import log_step
                        await log_step(
                            roomdoo_client, execution_id, "tool_call",
                            agent.id, effective_role, effective_log,
                            tool_name=fn_name, tool_args=fn_args,
                            tool_result=tool_result,
                            status="success",
                        )

    return WorkerResult(None, tools_used)


# ── Title generation ──────────────────────────────────────────────────

async def _maybe_generate_title(
    conversation_id: int,
    first_message: str,
    llm_client,
    sio: socketio.AsyncServer,
) -> None:
    """Generate a title for internal conversations that don't have one yet."""
    try:
        from app.core.database import SessionLocal
        from app.models.conversation import Conversation

        async with SessionLocal() as db:
            conv = await db.get(Conversation, conversation_id)
            if not conv or conv.conversation_type == "guest" or conv.title:
                return

            if not first_message:
                return

            try:
                from litellm import acompletion
                response = await acompletion(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Generate a short title (max 50 chars) for this conversation. Return ONLY the title, nothing else. Language: same as user message."},
                        {"role": "user", "content": first_message[:500]},
                    ],
                    temperature=0.3,
                    max_tokens=60,
                )
            except Exception:
                return

            content = response.choices[0].message.content if response and response.choices else None
            if content:
                title = content.strip().strip('"').strip("'")[:255]
                conv.title = title
                await db.commit()

                # Emit title update
                try:
                    from app.realtime.events import EVENT_CONVERSATION_UPDATED
                    await sio.emit(
                        EVENT_CONVERSATION_UPDATED,
                        {"id": conversation_id, "title": title},
                        room=f"internal:{conv.odoo_user_id or 'anon'}",
                    )
                except Exception:
                    pass

                log.info("Generated title for conv %d: %s", conversation_id, title)
    except Exception as exc:
        log.debug("Title generation failed for conv %d: %s", conversation_id, exc)


# ── Helpers ──────────────────────────────────────────────────────────

def _build_confirmation_response(cr: ConfirmationRequired) -> dict:
    """Build a rich confirmation_required response for the LLM."""
    return {
        "status": "confirmation_required",
        "message": (
            f"Action '{cr.tool_name}' requires guest confirmation before executing. "
            f"Present a clear summary of what will happen using the details below, "
            f"then ask the guest to confirm."
        ),
        "action_details": cr.args,
        "audit_id": cr.audit_id,
    }


GOD_MODE_TOOL_NAMES = {"odoo_list_models", "odoo_get_fields", "odoo_search_read", "odoo_write"}

async def _execute_tool(fn_name, fn_args, agent, tool_executor, roomdoo_client, conversation_id, guest_message=None, effective_confirm="sensitive", execution_id=None, effective_role="assistant", effective_log="basic"):
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
            return _build_confirmation_response(cr)
        except Exception as exc:
            return {"error": str(exc)}
    elif tool_executor:
        try:
            return await tool_executor.execute(
                fn_name, fn_args, agent,
                conversation_id=conversation_id,
                guest_message=guest_message,
                effective_confirmation=effective_confirm,
                execution_id=execution_id,
                effective_role=effective_role,
                effective_log=effective_log,
            )
        except ConfirmationRequired as cr:
            return _build_confirmation_response(cr)
        except Exception as exc:
            return {"error": str(exc)}
    return {"error": "Tool execution not available"}


async def _generate_handoff_message(
    escalation_type: str,
    escalation_reason: str,
    guest_message: str,
    prop,
    guest_language: str,
    llm_client: LLMProvider,
    supervisor,
    conversation_history: list | None = None,
) -> str | None:
    """Generate a contextual handoff message via supervisor LLM before escalating."""
    from app.services.language_detector import LANGUAGE_NAMES

    contact_parts = []
    if prop.phone:
        contact_parts.append(f"Phone: {prop.phone}")
    if prop.email:
        contact_parts.append(f"Email: {prop.email}")
    contact_block = ", ".join(contact_parts) if contact_parts else "none available"

    history_block = ""
    if conversation_history:
        lines = []
        for m in conversation_history[-6:]:
            sender_val = m.sender.value if hasattr(m.sender, "value") else str(m.sender)
            label = "Guest" if sender_val == "guest" else "AI"
            content = (m.content or "")[:150]
            lines.append(f"  {label}: {content}")
        history_block = "\nRecent conversation:\n" + "\n".join(lines)

    lang_name = LANGUAGE_NAMES.get(guest_language, "Spanish")

    prompt = f"""Generate a brief handoff message for a hotel guest whose request is being transferred to the hotel team.

Hotel: {prop.name}
Escalation type: {escalation_type}
Reason: {escalation_reason}
Guest message: {guest_message}
Hotel contact info: {contact_block}
{history_block}

Rules:
- Write in {lang_name}
- Acknowledge what the guest asked
- Explain that a team member will follow up as soon as possible
- Include hotel contact info (phone/email) if available
- Max 3 sentences, warm tone
- No markdown, no asterisks, plain text only
- Return ONLY the message, nothing else"""

    try:
        response = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            provider=supervisor.config.llm_account.provider,
            api_key=supervisor.config.llm_account.api_key,
            model=supervisor.config.effective_model,
            api_base_url=supervisor.config.llm_account.api_base_url,
            temperature=0.3,
            max_tokens=200,
        )
        return response.content.strip() if response.content else None
    except Exception as exc:
        log.warning("Handoff message generation failed: %s", exc)
        return None


async def _send_handoff(
    escalation_type, escalation_reason, guest_message,
    prop, session, llm_client, supervisor, conversation_history,
    conversation_id, attention_session_id, channel_endpoint,
    contact, db, wa_client, sio, routed_property_id, tracker,
):
    """Best-effort: generate and send a contextual handoff message before escalation."""
    lang = (session.guest_language or "es")[:2]
    handoff = await _generate_handoff_message(
        escalation_type, escalation_reason, guest_message,
        prop, lang, llm_client, supervisor, conversation_history,
    )
    if handoff:
        await _send_and_persist(
            handoff, conversation_id, attention_session_id,
            channel_endpoint, contact, db, wa_client, sio,
            routed_property_id, tracker, ai_enabled=session.ai_enabled,
        )


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


async def _send_and_persist(content, conversation_id, attention_session_id, channel_endpoint, contact, db, wa_client, sio, routed_property_id, tracker, ai_enabled=None):
    from app.services.message_fragmenter import (
        compute_typing_delay,
        fragment_message,
        is_topic_transition,
    )

    from app.services.channel_formatter import format_for_channel

    channel = getattr(channel_endpoint, "channel", "") or ""
    formatted = format_for_channel(content, channel)

    fragments = fragment_message(formatted)
    last_msg = None
    is_internal = channel == "internal"

    # Resolve chat room once before the loop
    chat_room = f"chat:{contact.phone_code}"
    if is_internal:
        conv = await conversation_repo.find_by_id(db, conversation_id)
        if conv and conv.odoo_user_id:
            chat_room = f"internal:{conv.odoo_user_id}"

    for i, fragment in enumerate(fragments):
        # Typing delay before sending (skip first fragment — LLM already took time)
        if i > 0 and not is_internal:
            delay = compute_typing_delay(fragment, is_topic_transition(fragment))
            await asyncio.sleep(delay)

        wa_message_id = None
        if not is_internal:
            try:
                wa_message_id = await wa_client.send_text(
                    to=contact.phone_code, channel_endpoint=channel_endpoint, text=fragment,
                )
            except Exception as exc:
                log.error("Failed to send fragment %d: %s", i, exc)

        ep_id = channel_endpoint.id if channel_endpoint.id else None
        msg = await message_repo.create(
            db, conversation_id=conversation_id, channel_endpoint_id=ep_id,
            attention_session_id=attention_session_id, direction=MessageDirection.outbound,
            sender=MessageSender.ai, content=fragment, wa_message_id=wa_message_id,
            delivery_status=DeliveryStatus.sent if wa_message_id else (DeliveryStatus.delivered if is_internal else DeliveryStatus.failed),
        )
        last_msg = msg

        # Emit message.created per fragment so frontend sees them arrive in real-time
        try:
            await sio.emit(EVENT_MESSAGE_CREATED, build_message_created_payload(msg, contact), room=chat_room)
        except Exception:
            pass

    await db.commit()

    # Emit conversation.updated once with the last message
    try:
        counts = await conversation_repo.get_unread_counts(db, [conversation_id], routed_property_id)
        conversation = await conversation_repo.find_by_id(db, conversation_id)
        if conversation and last_msg:
            await sio.emit(EVENT_CONVERSATION_UPDATED, build_conversation_payload(
                conversation, contact, last_message=last_msg, unread_count=counts.get(conversation_id, 0),
                ai_enabled=ai_enabled,
            ), room=f"property:{routed_property_id}")
    except Exception as exc:
        log.warning("Socket.IO emit failed: %s", exc)
