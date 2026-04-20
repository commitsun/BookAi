"""
Audit log for agent operations — records every tool execution
to bookai.agent.audit.log in Odoo via SDK.

Mandatory for god_mode agents, optional for regular tool executions.
"""

import logging

from roomdoo_sdk import RoomdooClient

log = logging.getLogger("audit_service")


async def log_audit(
    client: RoomdooClient,
    agent_id: int,
    operation: str,
    model_name: str,
    method_name: str,
    conversation_id: int,
    record_ids: list[int] | None = None,
    args_summary: str = "",
    confirmed_by: str | None = None,
    user_id: int | None = None,
    status: str = "success",
) -> None:
    """Write an audit log entry to Odoo."""
    try:
        await client._transport.create(
            "bookai.agent.audit.log",
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "operation": operation,
                "model_name": model_name,
                "method_name": method_name,
                "record_ids": str(record_ids) if record_ids else "[]",
                "args_summary": args_summary[:500],
                "confirmed_by": confirmed_by,
                "conversation_id": str(conversation_id),
                "status": status,
            },
        )
    except Exception as exc:
        log.warning("Failed to write audit log: %s", exc)
