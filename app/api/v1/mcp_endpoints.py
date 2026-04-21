"""
MCP server management endpoints — called by Odoo to manage MCP servers.
All scoped by instance (via Bearer auth).
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.dependencies import get_instance, get_mcp_manager
from app.models.instance import Instance
from app.services.mcp_manager import MCPManager

log = logging.getLogger("mcp_endpoints")


class MCPServerConfig(BaseModel):
    server_id: int
    name: str
    transport_type: str = "stdio"
    command: str | None = None
    args: str | None = None
    env_vars: dict[str, str] | None = None
    url: str | None = None
    api_key: str | None = None
    auth_type: str | None = None


class MCPServerUpdatedPayload(BaseModel):
    type: Literal["mcp_server_updated"]
    server_id: int
    action: str


api_router = APIRouter(prefix="/mcp/servers", tags=["mcp"])


@api_router.post("/{server_id}/connect", summary="Connect to an MCP server")
async def connect_server(
    server_id: int,
    body: MCPServerConfig,
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    return await mcp.connect(instance.id, server_id, body.model_dump())


@api_router.post("/{server_id}/discover", summary="Discover tools from an MCP server")
async def discover_server(
    server_id: int,
    body: MCPServerConfig | None = None,
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    config = body.model_dump() if body else None
    tools = await mcp.discover(instance.id, server_id, config)
    return {"status": "ok", "tools": tools}


@api_router.post("/{server_id}/disconnect", summary="Disconnect an MCP server")
async def disconnect_server(
    server_id: int,
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    await mcp.disconnect(instance.id, server_id)
    return {"status": "ok"}


@api_router.get("/{server_id}/status", summary="Check MCP server connection status")
async def server_status(
    server_id: int,
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    key = (instance.id, server_id)
    state = mcp._servers.get(key)
    if state is None:
        return {"server_id": server_id, "connected": False}
    return {
        "server_id": server_id,
        "connected": True,
        "name": state.name,
        "transport_type": state.transport_type,
        "tools_count": len(state.tools),
    }


@api_router.get("/status", summary="List all connected MCP servers for this instance")
async def list_servers_status(
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    servers = [
        s for s in mcp.connected_servers
        if s["instance_id"] == instance.id
    ]
    return {"servers": servers}


webhook_router = APIRouter(prefix="/webhooks", tags=["mcp-webhooks"])


@webhook_router.post("/mcp-server-updated", status_code=200)
async def mcp_server_updated(
    payload: MCPServerUpdatedPayload,
    instance: Instance = Depends(get_instance),
    mcp: MCPManager = Depends(get_mcp_manager),
) -> dict:
    if payload.action == "delete":
        await mcp.disconnect(instance.id, payload.server_id)
    return {"status": "ok", "action": payload.action}
