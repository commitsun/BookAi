"""
Supervisor service — evaluates messages and validates responses.

The supervisor is an agent configured in Odoo (supervisor-external,
supervisor-internal, supervisor-roomdoo) that decides:
- Which worker agent should handle the message
- Whether to escalate to a human
- Whether a worker's response passes quality checks
"""

import json
import logging
from dataclasses import dataclass

from app.services.agent_loader import CachedAgent
from app.services.llm_client import LLMProvider

log = logging.getLogger("supervisor_service")

SUPERVISOR_COOLDOWN = 3  # Evaluate every N messages


@dataclass
class SupervisorDecision:
    action: str  # delegate | escalate | respond_direct
    worker_technical_name: str | None = None
    worker_id: int | None = None
    escalation_type: str | None = None
    escalation_reason: str | None = None
    direct_response: str | None = None


@dataclass
class OutputValidation:
    approved: bool
    escalation_type: str | None = None
    escalation_reason: str | None = None


def should_supervise(session, message_count: int) -> bool:
    """Decide if the supervisor should evaluate this message."""
    # Always supervise the first message
    if message_count <= 1:
        return True
    # After a worker change, supervise the next message too
    # (covered by checking if active_agent_id changed recently)
    # Cooldown: every N messages
    return message_count % SUPERVISOR_COOLDOWN == 0


async def run_supervisor(
    supervisor: CachedAgent,
    message: str,
    available_workers: list[CachedAgent],
    current_worker_name: str | None,
    llm_client: LLMProvider,
) -> SupervisorDecision:
    """Ask the supervisor to evaluate a message and decide what to do.

    The supervisor receives the message + list of available workers and returns
    a structured decision: delegate to a worker, escalate, or respond directly.
    """
    worker_list = "\n".join(
        f"- {w.config.technical_name}: {w.config.description}"
        for w in available_workers
    )

    current_info = ""
    if current_worker_name:
        current_info = f"\nCurrently active worker: {current_worker_name}"

    prompt = f"""{supervisor.config.system_prompt}

## Available worker agents
{worker_list}
{current_info}

## Guest message
{message}

## Your task
Evaluate this message and respond with a JSON object (no markdown, no explanation):
{{
    "action": "delegate" | "escalate" | "respond_direct",
    "worker": "technical_name of the best worker" (only if action=delegate),
    "escalation_type": "manual|info_not_found|bad_response|inappropriate" (only if action=escalate),
    "escalation_reason": "brief explanation" (only if action=escalate),
    "response": "your direct response" (only if action=respond_direct)
}}
"""

    try:
        response = await llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            provider=supervisor.config.llm_account.provider,
            api_key=supervisor.config.llm_account.api_key,
            model=supervisor.config.effective_model,
            api_base_url=supervisor.config.llm_account.api_base_url,
            temperature=0.1,
            max_tokens=300,
        )

        decision = _parse_decision(response.content or "", available_workers)
        log.info(
            "Supervisor decision: action=%s worker=%s",
            decision.action, decision.worker_technical_name,
        )
        return decision

    except Exception as exc:
        log.error("Supervisor failed: %s — defaulting to first worker", exc)
        if available_workers:
            return SupervisorDecision(
                action="delegate",
                worker_technical_name=available_workers[0].config.technical_name,
                worker_id=available_workers[0].config.id,
            )
        return SupervisorDecision(action="escalate", escalation_type="bad_response",
                                  escalation_reason="Supervisor failed")


async def validate_output(
    supervisor: CachedAgent,
    response_text: str,
    original_message: str,
    llm_client: LLMProvider,
) -> OutputValidation:
    """Post-check: validate a worker's response before sending to the guest."""
    prompt = f"""{supervisor.config.system_prompt}

## Quality check
The AI generated the following response to a guest message. Evaluate if it's safe and appropriate to send.

Guest message: {original_message}
AI response: {response_text}

Respond with a JSON object (no markdown):
{{
    "approved": true | false,
    "escalation_type": "bad_response|inappropriate" (only if not approved),
    "reason": "brief explanation" (only if not approved)
}}
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
        log.error("Output validation failed: %s — approving by default", exc)
        return OutputValidation(approved=True)


def _parse_decision(text: str, workers: list[CachedAgent]) -> SupervisorDecision:
    """Parse supervisor JSON response into a SupervisorDecision."""
    try:
        # Strip markdown fences if present
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(clean)
    except (json.JSONDecodeError, IndexError):
        # Default: delegate to first worker
        if workers:
            return SupervisorDecision(
                action="delegate",
                worker_technical_name=workers[0].config.technical_name,
                worker_id=workers[0].config.id,
            )
        return SupervisorDecision(action="escalate", escalation_type="bad_response",
                                  escalation_reason="Could not parse supervisor response")

    action = data.get("action", "delegate")

    if action == "delegate":
        worker_name = data.get("worker", "")
        for w in workers:
            if w.config.technical_name == worker_name:
                return SupervisorDecision(
                    action="delegate",
                    worker_technical_name=worker_name,
                    worker_id=w.config.id,
                )
        # Worker not found — use first
        if workers:
            return SupervisorDecision(
                action="delegate",
                worker_technical_name=workers[0].config.technical_name,
                worker_id=workers[0].config.id,
            )

    elif action == "escalate":
        return SupervisorDecision(
            action="escalate",
            escalation_type=data.get("escalation_type", "manual"),
            escalation_reason=data.get("escalation_reason", data.get("reason", "")),
        )

    elif action == "respond_direct":
        return SupervisorDecision(
            action="respond_direct",
            direct_response=data.get("response", ""),
        )

    return SupervisorDecision(action="delegate",
                              worker_technical_name=workers[0].config.technical_name if workers else None,
                              worker_id=workers[0].config.id if workers else None)


def _parse_validation(text: str) -> OutputValidation:
    """Parse validation JSON response."""
    try:
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(clean)
    except (json.JSONDecodeError, IndexError):
        return OutputValidation(approved=True)

    return OutputValidation(
        approved=data.get("approved", True),
        escalation_type=data.get("escalation_type"),
        escalation_reason=data.get("reason"),
    )
