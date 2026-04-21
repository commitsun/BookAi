"""
MCP Server Manager — manages the lifecycle of MCP servers.

Supports two transport types:
- stdio: launches a subprocess, communicates via stdin/stdout
- http: connects to a remote HTTP MCP server

Servers are connected on demand (via /connect endpoint called by Odoo)
and kept alive until explicitly disconnected or BookAI shuts down.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("mcp_manager")


@dataclass
class MCPServerState:
    server_id: int
    name: str
    transport_type: str  # stdio | http
    config: dict
    session: ClientSession | None = None
    tools: list[dict] = field(default_factory=list)
    _exit_stack: AsyncExitStack | None = field(default=None, repr=False)


class MCPManager:

    def __init__(self) -> None:
        self._servers: dict[int, MCPServerState] = {}
        # Reverse map: tool_name → server_id (built during discover)
        self._tool_map: dict[str, int] = {}

    # ── Connect ──────────────────────────────────────────────────────

    async def connect(self, server_id: int, config: dict) -> dict:
        """Connect to an MCP server. Config varies by transport_type."""
        # Disconnect existing if reconnecting
        if server_id in self._servers:
            await self.disconnect(server_id)

        transport_type = config.get("transport_type", "stdio")
        name = config.get("name", f"server-{server_id}")

        state = MCPServerState(
            server_id=server_id,
            name=name,
            transport_type=transport_type,
            config=config,
        )

        try:
            if transport_type == "stdio":
                await self._connect_stdio(state)
            elif transport_type == "http":
                self._connect_http(state)
            else:
                return {"status": "error", "message": f"Unknown transport: {transport_type}"}
        except Exception as exc:
            log.error("Failed to connect MCP server %d (%s): %s", server_id, name, exc)
            return {"status": "error", "message": str(exc)}

        self._servers[server_id] = state
        log.info("MCP server %d (%s) connected via %s", server_id, name, transport_type)
        return {"status": "ok", "message": "Server connected successfully"}

    async def _connect_stdio(self, state: MCPServerState) -> None:
        """Launch a stdio MCP server as a subprocess."""
        config = state.config
        command = config.get("command", "")
        args_str = config.get("args", "")
        args = args_str.split() if isinstance(args_str, str) else args_str
        env_vars = config.get("env_vars") or {}

        params = StdioServerParameters(
            command=command,
            args=args,
            env=env_vars if env_vars else None,
        )

        exit_stack = AsyncExitStack()
        read, write = await exit_stack.enter_async_context(
            stdio_client(params)
        )
        session = await exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()

        state.session = session
        state._exit_stack = exit_stack

    def _connect_http(self, state: MCPServerState) -> None:
        """Store config for HTTP MCP server (no persistent connection)."""
        # HTTP servers don't need a persistent connection — we just store
        # the config and make requests on demand
        state.session = None
        log.info("HTTP MCP server %d config stored", state.server_id)

    # ── Discover ─────────────────────────────────────────────────────

    async def discover(self, server_id: int, config: dict | None = None) -> list[dict]:
        """Discover tools from an MCP server. Connects first if needed."""
        if server_id not in self._servers:
            if config is None:
                return []
            result = await self.connect(server_id, config)
            if result.get("status") != "ok":
                return []

        state = self._servers[server_id]

        if state.transport_type == "stdio" and state.session:
            try:
                result = await state.session.list_tools()
                tools = [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema if hasattr(t, "inputSchema") else {},
                    }
                    for t in result.tools
                ]
                state.tools = tools
                # Update reverse map
                for tool in tools:
                    self._tool_map[tool["name"]] = server_id
                log.info(
                    "Discovered %d tools from MCP server %d (%s)",
                    len(tools), server_id, state.name,
                )
                return tools
            except Exception as exc:
                log.error("Discovery failed for server %d: %s", server_id, exc)
                return []

        elif state.transport_type == "http":
            # HTTP discovery not implemented yet
            log.warning("HTTP MCP discovery not yet implemented")
            return []

        return []

    # ── Call tool ────────────────────────────────────────────────────

    async def call_tool(
        self, server_id: int, tool_name: str, args: dict,
    ) -> dict:
        """Execute a tool on an MCP server."""
        state = self._servers.get(server_id)
        if state is None:
            return {"error": f"MCP server {server_id} not connected"}

        if state.transport_type == "stdio" and state.session:
            try:
                result = await state.session.call_tool(tool_name, args)
                # Extract text content from result
                content_parts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        content_parts.append(block.text)
                return {"result": "\n".join(content_parts) if content_parts else str(result)}
            except Exception as exc:
                log.error("MCP tool call failed: %s on server %d: %s", tool_name, server_id, exc)
                return {"error": str(exc)}

        return {"error": f"Cannot execute on {state.transport_type} server (not connected)"}

    def find_server_for_tool(self, tool_name: str) -> int | None:
        """Find which server hosts a given tool."""
        return self._tool_map.get(tool_name)

    # ── Disconnect ───────────────────────────────────────────────────

    async def disconnect(self, server_id: int) -> None:
        """Disconnect and clean up an MCP server."""
        state = self._servers.pop(server_id, None)
        if state is None:
            return

        # Clean tool map
        for tool_name, sid in list(self._tool_map.items()):
            if sid == server_id:
                del self._tool_map[tool_name]

        if state._exit_stack:
            try:
                await state._exit_stack.aclose()
            except Exception as exc:
                log.warning("Error closing MCP server %d: %s", server_id, exc)

        log.info("MCP server %d (%s) disconnected", server_id, state.name)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def close_all(self) -> None:
        """Disconnect all MCP servers (called on app shutdown)."""
        for server_id in list(self._servers.keys()):
            await self.disconnect(server_id)
        log.info("All MCP servers closed")

    @property
    def connected_servers(self) -> list[dict]:
        return [
            {
                "server_id": s.server_id,
                "name": s.name,
                "transport_type": s.transport_type,
                "tools_count": len(s.tools),
            }
            for s in self._servers.values()
        ]
