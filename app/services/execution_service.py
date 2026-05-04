"""
Execution lifecycle and step tracing via Odoo models.

Writes to bookai.execution and bookai.execution.step.
All functions are fire-and-forget — failures are logged but never
propagate to the caller. The AI pipeline must not fail because
traceability failed.
"""

import json
import logging
from datetime import datetime, timezone

from roomdoo_sdk import RoomdooClient

from app.services.execution_policy import should_log_step

log = logging.getLogger("execution_service")

_EXECUTION_MODEL = "bookai.execution"
_STEP_MODEL = "bookai.execution.step"


async def create_execution(
    client: RoomdooClient,
    agent_id: int,
    pms_property_id: int | None,
    conversation_id: int,
    caller_info: str,
    effective_role: str,
    effective_confirmation: str,
    effective_log_level: str,
) -> int | None:
    """Create a bookai.execution record. Returns execution_id."""
    try:
        vals = {
            "agent_id": agent_id,
            "conversation_id": str(conversation_id),
            "caller_info": caller_info or "",
            "effective_role": effective_role,
            "effective_confirmation": effective_confirmation,
            "effective_log_level": effective_log_level,
            "state": "running",
        }
        if pms_property_id:
            vals["pms_property_id"] = pms_property_id
        execution_id = await client._transport.create(
            _EXECUTION_MODEL, vals,
        )
        log.info(
            "Execution %d created (agent=%d, conv=%d, role=%s)",
            execution_id, agent_id, conversation_id, effective_role,
        )
        return execution_id
    except Exception as exc:
        log.warning("Failed to create execution: %s", exc)
        return None


async def complete_execution(
    client: RoomdooClient,
    execution_id: int,
    result_summary: str | None = None,
) -> None:
    """Mark execution as completed."""
    try:
        vals = {
            "state": "completed",
            "end_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        if result_summary:
            vals["result_summary"] = result_summary[:500]
        await client._transport.write(
            _EXECUTION_MODEL, [execution_id], vals,
        )
    except Exception as exc:
        log.warning("Failed to complete execution %d: %s", execution_id, exc)


async def fail_execution(
    client: RoomdooClient,
    execution_id: int,
    error_message: str,
) -> None:
    """Mark execution as errored."""
    try:
        await client._transport.write(
            _EXECUTION_MODEL, [execution_id], {
                "state": "error",
                "end_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "error_message": error_message[:500],
            },
        )
    except Exception as exc:
        log.warning("Failed to fail execution %d: %s", execution_id, exc)


async def log_step(
    client: RoomdooClient,
    execution_id: int,
    step_type: str,
    agent_id: int,
    effective_role: str,
    effective_log_level: str,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: dict | None = None,
    status: str = "success",
    description: str | None = None,
    confirmation_summary: str | None = None,
    confirmation_response: str | None = None,
    confirmed: bool | None = None,
    delegated_agent_id: int | None = None,
    parent_step_id: int | None = None,
) -> int | None:
    """Create a bookai.execution.step. Respects log_level filtering."""
    do_log, include_args, include_result = should_log_step(
        effective_log_level, step_type,
    )
    if not do_log:
        return None

    try:
        vals: dict = {
            "execution_id": execution_id,
            "step_type": step_type,
            "agent_id": agent_id,
            "effective_role": effective_role,
            "status": status,
        }
        if tool_name:
            vals["tool_name"] = tool_name
        if include_args and tool_args:
            vals["tool_args"] = json.dumps(tool_args, default=str)[:1000]
        if include_result and tool_result:
            vals["tool_result"] = json.dumps(tool_result, default=str)[:2000]
        if description:
            vals["description"] = description[:500]
        if confirmation_summary:
            vals["confirmation_summary"] = confirmation_summary[:1000]
        if confirmation_response:
            vals["confirmation_response"] = confirmation_response[:500]
        if confirmed is not None:
            vals["confirmed"] = confirmed
        if delegated_agent_id:
            vals["delegated_agent_id"] = delegated_agent_id
        if parent_step_id:
            vals["parent_step_id"] = parent_step_id

        step_id = await client._transport.create(_STEP_MODEL, vals)
        return step_id
    except Exception as exc:
        log.warning("Failed to log step: %s", exc)
        return None
