"""
POST /webhooks/agent-updated — receive agent change notifications from Odoo
GET  /api/v1/sdk/tools        — expose SDK tool catalog for Odoo sync
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_instance, get_sdk_registry
from app.models.instance import Instance
from app.services.instance_sdk_registry import InstanceSDKRegistry

log = logging.getLogger("ai_webhooks")

router = APIRouter(prefix="/webhooks", tags=["ai-webhooks"])


class AgentUpdatedPayload(BaseModel):
    type: Literal["agent_updated"]
    agent_id: int
    technical_name: str
    action: str  # upsert | delete


@router.post(
    "/agent-updated",
    status_code=200,
    summary="Receive agent change notifications from Odoo",
)
async def agent_updated(
    payload: AgentUpdatedPayload,
    instance: Instance = Depends(get_instance),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.repositories import agent_repo

    loader = sdk_registry.get_loader(instance.id)
    if loader is None:
        return {"status": "ok", "detail": "No agent cache for this instance"}

    if payload.action == "delete":
        loader.remove(payload.technical_name)
        # Mark inactive in DB
        agent_db = await agent_repo.find_by_technical_name(
            db, instance.id, payload.technical_name,
        )
        if agent_db:
            agent_db.active = False
            await db.commit()
        log.info("Agent '%s' removed from cache + DB", payload.technical_name)
    else:
        try:
            await loader.reload_agent(payload.technical_name)
            log.info("Agent '%s' reloaded in cache", payload.technical_name)
        except Exception as exc:
            log.error("Failed to reload agent '%s': %s", payload.technical_name, exc)
            return {"status": "error", "detail": str(exc)}

        # Persist to DB
        cached = loader.get(payload.technical_name)
        if cached:
            ac = cached.config
            # Resolve allowed_agent_ids → names using cache
            id_to_name = {
                c.config.id: c.config.technical_name
                for c in loader._cache.values()
            }
            allowed_names = [
                id_to_name[aid] for aid in (ac.allowed_agent_ids or [])
                if aid in id_to_name
            ]
            await agent_repo.upsert_from_odoo(
                db,
                instance_id=instance.id,
                odoo_agent_id=ac.id,
                technical_name=ac.technical_name,
                is_supervisor=ac.is_supervisor,
                god_mode=ac.god_mode,
                caller_type=ac.caller_type,
                property_scope_ids=ac.property_scope_ids or [],
                allowed_user_ids=ac.allowed_user_ids or [],
                allowed_agent_names=allowed_names,
                active=ac.active,
            )
            await db.commit()
            log.info("Agent '%s' persisted to DB", payload.technical_name)

    return {"status": "ok", "action": payload.action}


# ── SDK tool catalog ─────────────────────────────────────────────────

sdk_router = APIRouter(prefix="/sdk", tags=["sdk"])


@sdk_router.get(
    "/tools",
    summary="List all SDK tools available for agent bindings",
    description=(
        "Returns the declarative catalog of tools the SDK exposes. "
        "Odoo syncs this nightly to populate the bookai.tool model."
    ),
)
async def list_sdk_tools(
    _instance: Instance = Depends(get_instance),
) -> dict:
    from roomdoo_sdk.tools import SDK_TOOLS
    return {"tools": SDK_TOOLS}
