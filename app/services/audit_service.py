"""
Audit log for agent operations — records every tool execution
to bookai.agent.audit.log in Odoo via SDK.

Also used for the confirmation flow: tools with requires_confirm
create a "pending" audit entry. When the guest confirms, the entry
is found and the tool executes.
"""

import json
import logging

from roomdoo_sdk import RoomdooClient

log = logging.getLogger("audit_service")

_MODEL = "bookai.agent.audit.log"


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
            _MODEL,
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


async def create_pending_audit(
    client: RoomdooClient,
    agent_id: int,
    conversation_id: int,
    method_name: str,
    args: dict,
    pms_property_id: int | None = None,
) -> int | None:
    """Create a pending audit entry for a tool that needs confirmation.

    Invalidates any previous pending audits for the same tool+conversation.
    Returns the audit log ID, or None on failure.
    """
    try:
        # Cancel stale pendings for this tool+conversation
        old = await client._transport.search_read(
            _MODEL,
            [
                ("conversation_id", "=", str(conversation_id)),
                ("method_name", "=", method_name),
                ("status", "=", "pending"),
            ],
            ["id"],
        )
        if old:
            old_ids = [r["id"] for r in old]
            await client._transport.write(_MODEL, old_ids, {"status": "rejected"})
            log.info("Cancelled %d stale pending audits for %s", len(old_ids), method_name)

        vals = {
            "agent_id": agent_id,
            "operation": "create",
            "method_name": method_name,
            "args_summary": json.dumps(args, default=str)[:500],
            "conversation_id": str(conversation_id),
            "status": "pending",
        }
        if pms_property_id:
            vals["pms_property_id"] = pms_property_id
        audit_id = await client._transport.create(_MODEL, vals)
        log.info(
            "Created pending audit %d for %s (conv=%d)",
            audit_id, method_name, conversation_id,
        )
        return audit_id
    except Exception as exc:
        log.warning("Failed to create pending audit: %s", exc)
        return None


async def find_pending_audit(
    client: RoomdooClient,
    conversation_id: int,
    method_name: str,
) -> dict | None:
    """Find the most recent pending audit for a tool+conversation.

    Returns dict with 'id' and 'args_summary', or None.
    """
    try:
        records = await client._transport.search_read(
            _MODEL,
            [
                ("conversation_id", "=", str(conversation_id)),
                ("method_name", "=", method_name),
                ("status", "=", "pending"),
            ],
            fields=["id", "args_summary", "agent_id"],
            limit=1,
        )
        return records[0] if records else None
    except Exception as exc:
        log.warning("Failed to find pending audit: %s", exc)
        return None


async def update_audit_status(
    client: RoomdooClient,
    audit_id: int,
    status: str,
    confirmed_by: str | None = None,
    error_message: str | None = None,
    confirmation_summary: str | None = None,
) -> None:
    """Update an audit entry's status (pending → confirmed → success/error)."""
    try:
        vals: dict = {"status": status}
        if confirmed_by:
            vals["confirmed_by"] = confirmed_by
        if error_message:
            vals["error_message"] = error_message[:500]
        if confirmation_summary:
            vals["confirmation_summary"] = confirmation_summary[:1000]
        await client._transport.write(_MODEL, [audit_id], vals)
    except Exception as exc:
        log.warning("Failed to update audit %d: %s", audit_id, exc)
