from __future__ import annotations

from dataclasses import dataclass

from ..transports.base import Transport


@dataclass
class McpServer:
    id: int
    name: str
    transport_type: str
    connection_status: str
    active: bool = True
    last_discovery_at: str | None = None
    tool_count: int = 0


class McpRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def list_servers(self) -> list[McpServer]:
        """All MCP servers."""
        records = await self._transport.search_read(
            "bookai.mcp.server",
            [],
            fields=[
                "id", "name", "transport_type",
                "connection_status", "active",
                "last_discovery_at", "tool_ids",
            ],
        )
        return [
            McpServer(
                id=r["id"],
                name=r.get("name", ""),
                transport_type=r.get(
                    "transport_type", ""
                ),
                connection_status=r.get(
                    "connection_status", "disconnected"
                ),
                active=r.get("active", True),
                last_discovery_at=(
                    r.get("last_discovery_at") or None
                ),
                tool_count=len(r.get("tool_ids", [])),
            )
            for r in records
        ]

    async def get_server_status(
        self, server_id: int
    ) -> dict:
        """Get server connection status."""
        records = await self._transport.read(
            "bookai.mcp.server",
            [server_id],
            fields=[
                "name", "connection_status",
                "status_message",
            ],
        )
        if not records:
            return {"connected": False, "message": "Not found"}
        r = records[0]
        return {
            "name": r.get("name", ""),
            "connected": r.get("connection_status")
            == "connected",
            "status": r.get("connection_status", ""),
            "message": r.get("status_message", ""),
        }

    async def connect_server(
        self, server_id: int
    ) -> None:
        """Trigger connect action on a server."""
        await self._transport.call(
            "bookai.mcp.server",
            "action_connect",
            args=[[server_id]],
        )

    async def discover_tools(
        self, server_id: int
    ) -> None:
        """Trigger tool discovery on a server."""
        await self._transport.call(
            "bookai.mcp.server",
            "action_discover_tools",
            args=[[server_id]],
        )
