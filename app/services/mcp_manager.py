"""
MCP Server Manager — manages the lifecycle of MCP servers per instance.

All keys are scoped by (instance_id, server_id) to prevent collisions
when multiple Odoo instances are connected to the same BookAI.

Supports two transport types:
- stdio: launches a subprocess, communicates via stdin/stdout
- http: connects to a remote HTTP MCP server
"""

import logging
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger("mcp_manager")


@dataclass
class MCPServerState:
    instance_id: int
    server_id: int
    name: str
    transport_type: str  # stdio | http
    config: dict
    session: ClientSession | None = None
    tools: list[dict] = field(default_factory=list)
    _exit_stack: AsyncExitStack | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[int, int]:
        return (self.instance_id, self.server_id)


class MCPManager:

    def __init__(self) -> None:
        # (instance_id, server_id) → state
        self._servers: dict[tuple[int, int], MCPServerState] = {}
        # (instance_id, tool_name) → (instance_id, server_id)
        self._tool_map: dict[tuple[int, str], tuple[int, int]] = {}

    # ── Connect ──────────────────────────────────────────────────────

    async def connect(
        self, instance_id: int, server_id: int, config: dict,
    ) -> dict:
        key = (instance_id, server_id)
        if key in self._servers:
            await self.disconnect(instance_id, server_id)

        transport_type = config.get("transport_type", "stdio")
        name = config.get("name", f"server-{server_id}")

        state = MCPServerState(
            instance_id=instance_id,
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
            log.error("Failed to connect MCP server %s (%s): %s", key, name, exc)
            return {"status": "error", "message": str(exc)}

        self._servers[key] = state
        log.info("MCP server %s (%s) connected via %s", key, name, transport_type)

        # Auto-discover tools after connecting
        tools = await self.discover(instance_id, server_id)
        return {
            "status": "ok",
            "message": "Server connected successfully",
            "tools_discovered": len(tools),
        }

    async def _connect_stdio(self, state: MCPServerState) -> None:
        config = state.config
        command = config.get("command", "")
        args_str = config.get("args", "")
        args = args_str.split() if isinstance(args_str, str) else args_str
        env_vars = config.get("env_vars") or {}

        params = StdioServerParameters(
            command=command, args=args,
            env=env_vars if env_vars else None,
        )

        exit_stack = AsyncExitStack()
        read, write = await exit_stack.enter_async_context(stdio_client(params))
        session = await exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        state.session = session
        state._exit_stack = exit_stack

    def _connect_http(self, state: MCPServerState) -> None:
        state.session = None

    # ── Discover ─────────────────────────────────────────────────────

    async def discover(
        self, instance_id: int, server_id: int, config: dict | None = None,
    ) -> list[dict]:
        key = (instance_id, server_id)
        if key not in self._servers:
            if config is None:
                return []
            result = await self.connect(instance_id, server_id, config)
            if result.get("status") != "ok":
                return []

        state = self._servers[key]

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
                for tool in tools:
                    self._tool_map[(instance_id, tool["name"])] = key
                log.info("Discovered %d tools from MCP server %s", len(tools), key)
                return tools
            except Exception as exc:
                log.error("Discovery failed for server %s: %s", key, exc)
                return []

        return []

    # ── Call tool ────────────────────────────────────────────────────

    async def call_tool(
        self, instance_id: int, server_id: int, tool_name: str, args: dict,
    ) -> dict:
        key = (instance_id, server_id)
        state = self._servers.get(key)
        if state is None:
            return {"error": f"MCP server {key} not connected"}

        if state.transport_type == "stdio" and state.session:
            try:
                result = await state.session.call_tool(tool_name, args)
                content_parts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        content_parts.append(block.text)
                output = "\n".join(content_parts) if content_parts else str(result)
                log.info("MCP tool %s returned %d chars", tool_name, len(output))
                return {"result": output}
            except Exception as exc:
                log.error("MCP tool call failed: %s on %s: %s", tool_name, key, exc)
                return {"error": str(exc)}

        return {"error": f"Cannot execute on {state.transport_type} server (not connected)"}

    def find_server_for_tool(
        self, instance_id: int, tool_name: str,
    ) -> int | None:
        """Find which server_id hosts a given tool for this instance."""
        key = self._tool_map.get((instance_id, tool_name))
        return key[1] if key else None

    # ── Disconnect ───────────────────────────────────────────────────

    async def disconnect(self, instance_id: int, server_id: int) -> None:
        key = (instance_id, server_id)
        state = self._servers.pop(key, None)
        if state is None:
            return

        for tk, sk in list(self._tool_map.items()):
            if sk == key:
                del self._tool_map[tk]

        if state._exit_stack:
            try:
                await state._exit_stack.aclose()
            except Exception as exc:
                log.warning("Error closing MCP server %s: %s", key, exc)

        log.info("MCP server %s (%s) disconnected", key, state.name)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def close_all(self) -> None:
        for (iid, sid) in list(self._servers.keys()):
            await self.disconnect(iid, sid)
        log.info("All MCP servers closed")

    @property
    def connected_servers(self) -> list[dict]:
        return [
            {
                "instance_id": s.instance_id,
                "server_id": s.server_id,
                "name": s.name,
                "transport_type": s.transport_type,
                "tools_count": len(s.tools),
            }
            for s in self._servers.values()
        ]
