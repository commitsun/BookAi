"""
AI response flow: after an inbound message is persisted, optionally generate
an AI response using the agent's tools and send it back via the channel.

Full sequence:
1. Check property.ai_enabled + session.ai_enabled
2. Load agents → select agent (router or pinned)
3. Verify caller_type access
4. Resolve SDK credentials per identity_mode
5. Build prompt + inject tools
6. LLM call → tool execution loop → final text
7. Send response via channel
8. Log unified usage to Odoo
9. Audit log for god_mode operations
"""

import json
import logging

import socketio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.instance import Instance, Property
from app.models.message import DeliveryStatus, MessageDirection, MessageSender
from app.models.session import AttentionSession
from app.repositories import conversation_repo, message_repo
from app.realtime.events import (
    EVENT_CONVERSATION_UPDATED,
    EVENT_MESSAGE_CREATED,
    build_conversation_payload,
    build_message_created_payload,
)
from app.services.agent_selector import select_agent
from app.services.audit_service import log_audit
from app.services.context_builder import build_prompt
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMClientError, LLMProvider
from app.services.tool_executor import ConfirmationRequired, ToolExecutor
from app.services.whatsapp_client import WhatsAppClient

log = logging.getLogger("ai_response_service")

MAX_TOOL_ROUNDS = 5


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
    """Fire-and-forget safe. All exceptions are caught and logged."""
    try:
        await _generate_and_send(
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
    mcp_manager=None,
) -> None:
    # --- 1. Check property + session AI flags ---
    result = await db.execute(
        select(Property)
        .options(selectinload(Property.instance))
        .where(Property.id == routed_property_id)
    )
    prop = result.scalar_one_or_none()
    if not prop or not prop.ai_enabled:
        return

    session = await db.get(AttentionSession, attention_session_id)
    if session and not session.ai_enabled:
        return

    instance: Instance = prop.instance

    # --- 2. Load agents + select ---
    loader = await sdk_registry.get_or_load_agents(instance)
    if loader is None:
        return

    candidates = loader.list_for_caller_type("external_guest")
    if not candidates:
        return

    router_config = {
        "provider": instance.router_llm_provider or "openai",
        "api_key": instance.router_llm_api_key or "",
        "model": instance.router_llm_model or "gpt-4o-mini",
    }
    agent_entry = await select_agent(
        message_content, candidates,
        session.active_agent_id if session else None,
        llm_client, router_config,
    )
    if agent_entry is None:
        return

    agent = agent_entry.config
    docs = agent_entry.documents

    # --- 3. Verify credentials ---
    if not agent.llm_account or not agent.llm_account.api_key:
        log.warning("Agent %s has no LLM credentials", agent.technical_name)
        return
    model = agent.effective_model
    if not model:
        log.warning("Agent %s has no model configured", agent.technical_name)
        return

    # --- 4. Resolve provider (sensitive_data → ollama) ---
    provider = agent.llm_account.provider
    api_key = agent.llm_account.api_key
    api_base_url = agent.llm_account.api_base_url
    if agent.sensitive_data and settings.ollama_url:
        provider = "ollama"
        api_key = ""
        api_base_url = settings.ollama_url
        log.info("Agent %s uses local model (sensitive_data)", agent.technical_name)

    # --- 5. Build prompt + tools ---
    history = await message_repo.find_recent_by_conversation(
        db, conversation_id, limit=20,
    )
    history.reverse()

    prompt_messages = build_prompt(
        agent=agent, docs=docs,
        conversation_history=history,
        current_message=message_content,
        property_name=prop.name,
    )

    # Get SDK client for tool execution
    roomdoo_client = sdk_registry.get_client(instance)
    tool_executor = (
        ToolExecutor(roomdoo_client, mcp_manager, instance.id)
        if roomdoo_client else None
    )

    llm_tools = None
    if tool_executor and agent.tools:
        llm_tools = tool_executor.build_llm_tools(agent)
    elif tool_executor and agent.god_mode:
        llm_tools = tool_executor.build_llm_tools(agent)

    # --- 6. LLM call with tool execution loop ---
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

        # No tool calls → final response
        if response.finish_reason != "tool_calls" or not response.tool_calls:
            final_content = response.content
            break

        # Process tool calls
        # Add assistant message with tool_calls to conversation
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

            tool_result = None
            if fn_name == "odoo_execute" and agent.god_mode and tool_executor:
                # God mode execution
                try:
                    tool_result = await tool_executor.execute_god_mode(
                        fn_args.get("model_name", ""),
                        fn_args.get("method", "search_read"),
                        fn_args,
                    )
                    # Audit log
                    if roomdoo_client:
                        await log_audit(
                            roomdoo_client, agent.id,
                            fn_args.get("method", "search_read"),
                            fn_args.get("model_name", ""),
                            fn_args.get("method", ""),
                            conversation_id,
                            fn_args.get("ids"),
                            json.dumps(fn_args)[:500],
                        )
                except ConfirmationRequired as cr:
                    tool_result = {
                        "status": "confirmation_required",
                        "message": f"Operation '{cr.description}' requires confirmation.",
                    }
                except Exception as exc:
                    tool_result = {"error": str(exc)}

            elif tool_executor:
                # Regular tool execution
                try:
                    tool_result = await tool_executor.execute(
                        fn_name, fn_args, agent,
                    )
                    # Audit log for god_mode agents
                    if agent.god_mode and roomdoo_client:
                        await log_audit(
                            roomdoo_client, agent.id,
                            "call", fn_name, fn_name,
                            conversation_id,
                            args_summary=json.dumps(fn_args)[:500],
                        )
                except ConfirmationRequired as cr:
                    tool_result = {
                        "status": "confirmation_required",
                        "message": f"Action '{cr.description}' requires confirmation.",
                    }
                except Exception as exc:
                    tool_result = {"error": str(exc)}
            else:
                tool_result = {"error": "Tool execution not available"}

            llm_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, default=str),
            })

    if not final_content:
        log.warning("No final content after %d rounds", MAX_TOOL_ROUNDS)
        return

    # --- 7. Track costs ---
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

    log.info(
        "AI response: agent=%s model=%s tokens=%d/%d rounds=%d cost=$%.6f",
        agent.technical_name, model,
        total_tokens_in, total_tokens_out,
        min(round_num + 1, MAX_TOOL_ROUNDS), llm_cost,
    )

    # --- 8. Log unified usage to Odoo ---
    try:
        from roomdoo_sdk.models import UsageRecord
        if roomdoo_client and tracker:
            log.info("Usage: %s", tracker.summary())
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
        log.warning("Failed to log usage to Odoo: %s", exc)

    # --- 9. Send via WhatsApp ---
    wa_message_id: str | None = None
    try:
        wa_message_id = await wa_client.send_text(
            to=contact.phone_code,
            channel_endpoint=channel_endpoint,
            text=final_content,
        )
    except Exception as exc:
        log.error("Failed to send AI response via WhatsApp: %s", exc)

    # --- 10. Persist + update session ---
    delivery = DeliveryStatus.sent if wa_message_id else DeliveryStatus.failed
    msg = await message_repo.create(
        db,
        conversation_id=conversation_id,
        channel_endpoint_id=channel_endpoint.id,
        attention_session_id=attention_session_id,
        direction=MessageDirection.outbound,
        sender=MessageSender.ai,
        content=final_content,
        wa_message_id=wa_message_id,
        delivery_status=delivery,
    )

    # Pin agent to session
    if session and session.active_agent_id != agent.id:
        session.active_agent_id = agent.id

    await db.commit()

    # --- 11. Socket.IO events ---
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
