"""
POST /webhooks/agent-updated — receive agent change notifications from Odoo
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
) -> dict:
    loader = sdk_registry.get_loader(instance.id)
    if loader is None:
        return {"status": "ok", "detail": "No agent cache for this instance"}

    if payload.action == "delete":
        loader.remove(payload.technical_name)
        log.info("Agent '%s' removed from cache", payload.technical_name)
    else:
        try:
            await loader.reload_agent(payload.technical_name)
            log.info("Agent '%s' reloaded in cache", payload.technical_name)
        except Exception as exc:
            log.error("Failed to reload agent '%s': %s", payload.technical_name, exc)
            return {"status": "error", "detail": str(exc)}

    return {"status": "ok", "action": payload.action}
