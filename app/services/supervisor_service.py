"""
Supervisor as active orchestrator.

The supervisor participates in EVERY message. It decides:
- respond: answer directly (simple greetings, etc.)
- delegate: which worker should handle this message
- escalate: create an escalation (last resort)
- reassign_supervisor: wrong supervisor, switch to another

After the worker responds, the supervisor validates. If rejected,
it can retry with a different worker (max 2 retries).
"""

import json
import logging
from dataclasses import dataclass

from app.services.agent_loader import CachedAgent
from app.services.llm_client import LLMProvider

log = logging.getLogger("supervisor_service")


@dataclass
class SupervisorResult:
    action: str  # respond | delegate | escalate | reassign_supervisor
    response: str | None = None
    worker_technical_name: str | None = None
    worker_id: int | None = None
    escalation_type: str | None = None
    escalation_reason: str | None = None
    new_supervisor_name: str | None = None


@dataclass
class ValidationResult:
    approved: bool
    retry_with: str | None = None  # technical_name of alternative worker
    escalation_type: str | None = None
    escalation_reason: str | None = None


# ── Orchestrate ──────────────────────────────────────────────────────

async def supervisor_orchestrate(
    supervisor: CachedAgent,
    message: str,
    available_workers: list[CachedAgent],
    current_worker_name: str | None,
    llm_client: LLMProvider,
    conversation_history: list | None = None,
) -> SupervisorResult:
    """Supervisor evaluates every message and decides what to do."""

    worker_list = "\n".join(
        f"- {w.config.technical_name}: {w.config.description}"
        for w in available_workers
    )
    current_info = f"\nCurrently active worker: {current_worker_name}" if current_worker_name else ""

    history_block = _format_history(conversation_history) if conversation_history else ""

    technical_block = f"""
## ORCHESTRATION INSTRUCTIONS (system — do not share with the user)

You are a supervisor agent. For every message, you MUST respond with a JSON object (no markdown, no explanation, just JSON).

### Available workers
{worker_list}
{current_info}
{history_block}

### Possible actions

1. **delegate** — assign a worker to handle this message
   {{"action": "delegate", "worker": "technical_name"}}

2. **respond** — you answer directly (only for very simple cases: greetings, thanks, goodbyes)
   {{"action": "respond", "response": "your direct response"}}

3. **escalate** — no worker can handle this, human intervention needed (LAST RESORT)
   {{"action": "escalate", "escalation_type": "manual|info_not_found|bad_response|inappropriate", "escalation_reason": "brief explanation"}}

4. **reassign_supervisor** — you are the wrong supervisor for this caller
   {{"action": "reassign_supervisor", "new_supervisor": "supervisor-external|supervisor-internal|supervisor-roomdoo"}}

### Rules
- ALWAYS prefer delegate over escalate
- Only escalate if the user EXPLICITLY asks for a human, or if the situation truly cannot be handled
- If in doubt, delegate to the most relevant worker
- If there is a currently active worker, short confirmations (yes, sí, ok, confirm, vale, de acuerdo, por favor, sí por favor) MUST be delegated to that worker — they likely need the confirmation to complete an action
- Respond directly ONLY for greetings or goodbyes when NO worker is active
- JSON only. No text before or after.
"""

    system_prompt = f"{supervisor.config.system_prompt}\n{technical_block}"

    try:
        response = await llm_client.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
            provider=supervisor.config.llm_account.provider,
            api_key=supervisor.config.llm_account.api_key,
            model=supervisor.config.effective_model,
            api_base_url=supervisor.config.llm_account.api_base_url,
            temperature=0.1,
            max_tokens=300,
        )
        result = _parse_orchestration(response.content or "", available_workers)
        log.info("Supervisor: action=%s worker=%s", result.action, result.worker_technical_name)
        return result

    except Exception as exc:
        log.error("Supervisor orchestrate failed: %s", exc)
        return _fallback_delegate(available_workers)


# ── Validate ─────────────────────────────────────────────────────────

async def supervisor_validate(
    supervisor: CachedAgent,
    worker_response: str,
    original_message: str,
    worker_name: str,
    available_workers: list[CachedAgent],
    llm_client: LLMProvider,
    tools_used: list[str] | None = None,
    has_folio_context: bool = False,
) -> ValidationResult:
    """Supervisor validates a worker's response before sending."""

    other_workers = [w for w in available_workers if w.config.technical_name != worker_name]
    alternatives = ", ".join(w.config.technical_name for w in other_workers) if other_workers else "none"

    data_sources_block = ""
    if tools_used:
        tools_list = ", ".join(tools_used)
        data_sources_block += (
            f"\nTools executed by the worker: {tools_list}\n"
            "The tool calls were verified by the system — the data in the response comes from real tool results, not hallucination.\n"
        )
    if has_folio_context:
        data_sources_block += (
            "\nThe worker had access to verified reservation data (folio codes, dates, status) "
            "injected by the system from the database. This data is real — do NOT reject for 'unverified data'.\n"
        )

    prompt = f"""{supervisor.config.system_prompt}

## VALIDATION TASK (system)

Check if this response is appropriate to send to the user.

Guest message: {original_message}
Worker ({worker_name}) response: {worker_response}
{data_sources_block}Alternative workers available: {alternatives}

Respond with JSON only:
- Approved: {{"approved": true}}
- Reject + retry with another worker: {{"approved": false, "retry_with": "other_worker_technical_name", "reason": "brief explanation"}}
- Reject + escalate (no alternatives): {{"approved": false, "escalate": true, "escalation_type": "bad_response", "reason": "brief explanation"}}

Rules:
- Approve unless the response is clearly wrong, offensive, or completely off-topic
- Do NOT reject for stylistic reasons or minor imperfections
- If the response addresses the user's question reasonably, approve it
- If the worker used tools or had system-injected data, the data is verified — do NOT reject for "unverified data"
"""

    try:
        result = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            provider=supervisor.config.llm_account.provider,
            api_key=supervisor.config.llm_account.api_key,
            model=supervisor.config.effective_model,
            api_base_url=supervisor.config.llm_account.api_base_url,
            temperature=0.0,
            max_tokens=200,
        )
        return _parse_validation(result.content or "")

    except Exception as exc:
        log.error("Supervisor validate failed: %s — approving by default", exc)
        return ValidationResult(approved=True)


# ── Parsers ──────────────────────────────────────────────────────────

def _parse_orchestration(text: str, workers: list[CachedAgent]) -> SupervisorResult:
    data = _parse_json(text)
    if data is None:
        return _fallback_delegate(workers)

    action = data.get("action", "delegate")

    if action == "respond":
        return SupervisorResult(
            action="respond",
            response=data.get("response", ""),
        )

    if action == "delegate":
        name = data.get("worker", "")
        for w in workers:
            if w.config.technical_name == name:
                return SupervisorResult(
                    action="delegate",
                    worker_technical_name=name,
                    worker_id=w.config.id,
                )
        return _fallback_delegate(workers)

    if action == "escalate":
        return SupervisorResult(
            action="escalate",
            escalation_type=data.get("escalation_type", "manual"),
            escalation_reason=data.get("escalation_reason", data.get("reason", "")),
        )

    if action == "reassign_supervisor":
        return SupervisorResult(
            action="reassign_supervisor",
            new_supervisor_name=data.get("new_supervisor", ""),
        )

    return _fallback_delegate(workers)


def _parse_validation(text: str) -> ValidationResult:
    data = _parse_json(text)
    if data is None:
        return ValidationResult(approved=True)

    if data.get("approved", True):
        return ValidationResult(approved=True)

    if data.get("escalate"):
        return ValidationResult(
            approved=False,
            escalation_type=data.get("escalation_type", "bad_response"),
            escalation_reason=data.get("reason", ""),
        )

    if data.get("retry_with"):
        return ValidationResult(
            approved=False,
            retry_with=data["retry_with"],
        )

    return ValidationResult(approved=True)


def _parse_json(text: str) -> dict | None:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(clean)
    except (json.JSONDecodeError, IndexError):
        return None


def _format_history(messages: list) -> str:
    """Format recent conversation messages as context for the supervisor."""
    if not messages:
        return ""
    lines = []
    for m in messages:
        sender = getattr(m, "sender", None)
        sender_val = sender.value if hasattr(sender, "value") else str(sender)
        content = getattr(m, "content", None) or ""
        label = "Guest" if sender_val == "guest" else "AI/Agent"
        # Truncate long messages to keep supervisor context lean
        if len(content) > 150:
            content = content[:150] + "…"
        lines.append(f"  {label}: {content}")
    return "\n### Recent conversation context\n" + "\n".join(lines)


def _fallback_delegate(workers: list[CachedAgent]) -> SupervisorResult:
    if workers:
        return SupervisorResult(
            action="delegate",
            worker_technical_name=workers[0].config.technical_name,
            worker_id=workers[0].config.id,
        )
    return SupervisorResult(
        action="escalate",
        escalation_type="bad_response",
        escalation_reason="No workers available",
    )
