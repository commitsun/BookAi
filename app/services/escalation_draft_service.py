"""
Service for generating and refining escalation draft responses.

The operator sends instructions via the app; this service uses the
supervisor's LLM credentials to produce a polished draft that the
operator can review, refine, and eventually send to the guest.
"""

import logging

import socketio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.instance import Instance, Property
from app.models.message import (
    DeliveryStatus,
    MessageDirection,
    MessageKind,
    MessageSender,
)
from app.models.session import AttentionSession
from app.repositories import escalation_repo, message_repo
from app.realtime.events import (
    EVENT_ESCALATION_DRAFT_UPDATED,
    build_escalation_draft_payload,
)
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider

log = logging.getLogger("escalation_draft_service")

SUPERVISOR_NAMES = {
    "external_guest": "supervisor-external",
    "internal": "supervisor-internal",
    "roomdoo": "supervisor-roomdoo",
}

SYSTEM_PROMPT = (
    "You are a hotel communication assistant helping an operator "
    "compose a response for a guest.\n\n"
    "Rules:\n"
    "- Write in the same language as the guest's original message\n"
    "- Be brief: 1-3 sentences, max ~350 characters\n"
    "- No markdown, no asterisks, plain text only\n"
    "- Do not mention internal terms (manager, operator, staff, "
    "reception team)\n"
    "- If offering to check something, use neutral phrasing "
    '("let me check", "I\'ll look into it")\n'
    "- Return ONLY the draft message text, nothing else"
)

HISTORY_LIMIT = 6


# ── Public API ──────────────────────────────────────────────────────


class DraftError(Exception):
    """Raised when draft generation/refinement fails."""

    def __init__(self, detail: str, status_code: int = 500):
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


async def generate_draft(
    escalation_id: int,
    instruction: str,
    agent_user_id: int | None,
    agent_display_name: str | None,
    db: AsyncSession,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry,
    llm_client: LLMProvider,
) -> tuple[str | None, list]:
    """Operator sends an instruction; AI generates a draft for the guest.

    Returns ``(draft_text, escalation_messages)``.
    """
    esc = await escalation_repo.find_by_id_with_messages(db, escalation_id)
    if esc is None:
        raise DraftError("Escalation not found", 404)
    if esc.status != "pending":
        raise DraftError("Escalation is not pending", 409)

    session, prop, instance = await _resolve_context(db, esc.session_id)
    supervisor = await _resolve_supervisor(sdk_registry, instance, session, db)

    # Persist operator instruction as a note in the escalation thread
    await message_repo.create(
        db,
        conversation_id=esc.conversation_id,
        attention_session_id=esc.session_id,
        escalation_id=esc.id,
        kind=MessageKind.note,
        direction=MessageDirection.inbound,
        sender=MessageSender.agent,
        content=instruction,
        agent_user_id=agent_user_id,
        agent_display_name=agent_display_name,
        delivery_status=DeliveryStatus.skipped,
    )

    # Load recent conversation history for context
    history = await message_repo.find_recent_by_conversation(
        db, esc.conversation_id, limit=HISTORY_LIMIT,
    )
    history.reverse()  # chronological

    # Build the escalation thread so far (including the instruction just added)
    await db.refresh(esc, ["messages"])
    thread = sorted(esc.messages, key=lambda m: m.id)

    user_prompt = _build_generate_prompt(esc, history, thread, instruction)

    draft = await _call_llm(llm_client, supervisor, user_prompt)

    # Persist AI draft as a note
    await message_repo.create(
        db,
        conversation_id=esc.conversation_id,
        attention_session_id=esc.session_id,
        escalation_id=esc.id,
        kind=MessageKind.note,
        direction=MessageDirection.outbound,
        sender=MessageSender.ai,
        content=draft,
        delivery_status=DeliveryStatus.skipped,
    )

    await escalation_repo.update_draft_response(db, esc, draft)
    await db.commit()

    # Refresh messages after commit
    await db.refresh(esc, ["messages"])
    messages = sorted(esc.messages, key=lambda m: m.id)

    await _emit_draft_updated(sio, esc, prop)

    return draft, messages


async def refine_draft(
    escalation_id: int,
    instruction: str,
    current_draft: str | None,
    db: AsyncSession,
    sio: socketio.AsyncServer,
    sdk_registry: InstanceSDKRegistry,
    llm_client: LLMProvider,
) -> str:
    """Refine the current draft with operator adjustments.

    Returns the refined draft text.
    """
    esc = await escalation_repo.find_by_id(db, escalation_id)
    if esc is None:
        raise DraftError("Escalation not found", 404)
    if esc.status != "pending":
        raise DraftError("Escalation is not pending", 409)

    base_draft = current_draft or esc.draft_response or esc.guest_message

    session, prop, instance = await _resolve_context(db, esc.session_id)
    supervisor = await _resolve_supervisor(sdk_registry, instance, session, db)

    user_prompt = _build_refine_prompt(esc, base_draft, instruction)

    draft = await _call_llm(llm_client, supervisor, user_prompt)

    await escalation_repo.update_draft_response(db, esc, draft)
    await db.commit()

    await _emit_draft_updated(sio, esc, prop)

    return draft


# ── Internal helpers ────────────────────────────────────────────────


async def _resolve_context(
    db: AsyncSession, session_id: int,
) -> tuple[AttentionSession, Property, Instance]:
    """Load the session → property → instance chain."""
    from sqlalchemy import select

    result = await db.execute(
        select(AttentionSession)
        .options(
            selectinload(AttentionSession.property)
            .selectinload(Property.instance),
        )
        .where(AttentionSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session or not session.property:
        raise DraftError("Session or property not found", 404)
    return session, session.property, session.property.instance


async def _resolve_supervisor(
    sdk_registry: InstanceSDKRegistry,
    instance: Instance,
    session: AttentionSession,
    db: AsyncSession,
):
    """Load the supervisor agent for the session's caller type."""
    loader = await sdk_registry.get_or_load_agents(instance, db)
    if loader is None:
        raise DraftError("AI not configured for this instance", 503)

    caller = session.caller_type or "external_guest"
    sup_name = SUPERVISOR_NAMES.get(caller, "supervisor-external")
    sup = loader.get(sup_name)

    if not sup or not sup.config.llm_account:
        raise DraftError(
            f"Supervisor {sup_name} not available or has no LLM",
            503,
        )
    return sup


async def _call_llm(llm_client: LLMProvider, supervisor, user_prompt: str) -> str:
    """Call the LLM via the supervisor's credentials."""
    account = supervisor.config.llm_account
    try:
        response = await llm_client.chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            provider=account.provider,
            api_key=account.api_key,
            model=supervisor.config.effective_model,
            api_base_url=account.api_base_url,
            temperature=0.3,
            max_tokens=400,
        )
    except Exception as exc:
        log.error("LLM call failed: %s", exc)
        raise DraftError("AI generation failed", 502) from exc

    return (response.content or "").strip()


def _build_generate_prompt(esc, history, thread, instruction: str) -> str:
    """Build the user prompt for draft generation."""
    parts = [
        f"## Escalation context",
        f"- Type: {esc.escalation_type}",
        f"- Reason: {esc.reason}",
        f"- Guest message: {esc.guest_message}",
    ]

    if history:
        parts.append("\n## Recent conversation")
        for m in history[-HISTORY_LIMIT:]:
            sender = m.sender.value if m.sender else "system"
            label = "Guest" if sender == "guest" else "AI"
            parts.append(f"  {label}: {(m.content or '')[:200]}")

    # Exclude the instruction we just added (last message in thread)
    prior_thread = [
        m for m in thread
        if m.sender != MessageSender.agent or m.content != instruction
    ]
    if prior_thread:
        parts.append("\n## Escalation discussion thread")
        for m in prior_thread:
            sender = m.sender.value if m.sender else "system"
            role = "Operator" if sender == "agent" else "AI"
            parts.append(f"  {role}: {(m.content or '')[:300]}")

    if esc.draft_response:
        parts.append(f"\n## Current draft\n{esc.draft_response}")

    parts.append(f"\n## Operator instruction\n{instruction}")
    parts.append(
        "\nGenerate a draft response for the guest "
        "based on the operator's instruction."
    )

    return "\n".join(parts)


def _build_refine_prompt(esc, base_draft: str, instruction: str) -> str:
    """Build the user prompt for draft refinement."""
    return (
        f"## Guest's original message\n{esc.guest_message}\n\n"
        f"## Current draft\n{base_draft}\n\n"
        f"## Operator's adjustment instructions\n{instruction}\n\n"
        "Refine the draft incorporating these adjustments. "
        "Return ONLY the updated draft text."
    )


async def _emit_draft_updated(sio, esc, prop) -> None:
    """Emit the draft_updated socket event."""
    try:
        await sio.emit(
            EVENT_ESCALATION_DRAFT_UPDATED,
            build_escalation_draft_payload(
                esc.id, esc.conversation_id, esc.draft_response,
            ),
            room=f"property:{prop.id}",
        )
    except Exception as exc:
        log.warning("Socket emit failed: %s", exc)
