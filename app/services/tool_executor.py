"""
Tool execution engine. Converts AgentToolBinding definitions to LLM function
calling format and dispatches invocations based on tool_type.

- sdk: delegates to client.tools.execute() — the SDK owns the dispatch
- mcp: delegates to MCPManager
- god_mode: generic odoo_execute via transport

BookAI does NOT know how individual SDK tools work internally.
"""

import json
import logging

from roomdoo_sdk import RoomdooClient
from roomdoo_sdk.models import AgentConfig

log = logging.getLogger("tool_executor")


class ConfirmationRequired(Exception):
    """Raised when a tool requires human confirmation before execution."""
    def __init__(self, tool_name: str, description: str, args: dict):
        self.tool_name = tool_name
        self.description = description
        self.args = args
        super().__init__(f"Tool '{tool_name}' requires confirmation")


# ── Name sanitization (LLM requires ^[a-zA-Z0-9_-]+$) ───────────────

def _to_llm_name(name: str) -> str:
    return name.replace(".", "__")


def _from_llm_name(name: str) -> str:
    return name.replace("__", ".")


# ── God mode tool definition ─────────────────────────────────────────

GOD_MODE_TOOL = {
    "type": "function",
    "function": {
        "name": "odoo_execute",
        "description": (
            "Execute any operation on Odoo. Use for reading or modifying "
            "any data in the PMS. Write operations require confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {"type": "string"},
                "method": {
                    "type": "string",
                    "enum": ["search_read", "read", "create", "write", "unlink"],
                },
                "domain": {"type": "array"},
                "fields": {"type": "array", "items": {"type": "string"}},
                "ids": {"type": "array", "items": {"type": "integer"}},
                "vals": {"type": "object"},
                "limit": {"type": "integer"},
            },
            "required": ["model_name", "method"],
        },
    },
}


class ToolExecutor:
    def __init__(self, client: RoomdooClient, mcp_manager=None, instance_id: int = 0):
        self._client = client
        self._mcp = mcp_manager
        self._instance_id = instance_id

    # ── Build LLM tools from bindings ────────────────────────────────

    def build_llm_tools(self, agent: AgentConfig) -> list[dict]:
        if agent.god_mode:
            return [GOD_MODE_TOOL]

        tools = []
        for binding in agent.tools:
            if not binding.active:
                continue
            schema = {}
            if binding.input_schema:
                try:
                    schema = (
                        json.loads(binding.input_schema)
                        if isinstance(binding.input_schema, str)
                        else binding.input_schema
                    )
                except (json.JSONDecodeError, TypeError):
                    schema = {}

            tools.append({
                "type": "function",
                "function": {
                    "name": _to_llm_name(binding.tool_name),
                    "description": binding.description or binding.tool_name,
                    "parameters": schema or {"type": "object", "properties": {}},
                },
            })
        return tools

    # ── Execute a tool call ──────────────────────────────────────────

    async def execute(
        self, tool_name: str, args: dict, agent: AgentConfig,
    ) -> dict:
        canonical = _from_llm_name(tool_name)

        binding = next(
            (t for t in agent.tools if t.tool_name == canonical), None,
        )
        if binding is None:
            return {"error": f"Tool '{canonical}' not bound to this agent"}

        if binding.requires_confirm:
            raise ConfirmationRequired(
                canonical, binding.description or canonical, args,
            )

        if binding.tool_type == "sdk":
            return await self._execute_sdk(binding.sdk_method or canonical, args)
        elif binding.tool_type == "mcp":
            return await self._execute_mcp(canonical, args)
        elif binding.tool_type == "webhook":
            return {"error": "Webhook tool execution not yet implemented"}
        elif binding.tool_type == "function":
            return {"error": "Function tool execution not yet implemented"}
        else:
            return {"error": f"Unknown tool_type: {binding.tool_type}"}

    # ── SDK: delegate to SDK's ToolRepository ────────────────────────

    async def _execute_sdk(self, sdk_method: str, args: dict) -> dict:
        try:
            return await self._client.tools.execute(sdk_method, args)
        except Exception as exc:
            log.error("SDK tool failed: %s(%s) — %s", sdk_method, args, exc)
            return {"error": str(exc)}

    # ── MCP: delegate to MCPManager ──────────────────────────────────

    async def _execute_mcp(self, tool_name: str, args: dict) -> dict:
        if not self._mcp:
            return {"error": "MCP manager not available"}
        server_id = self._mcp.find_server_for_tool(self._instance_id, tool_name)
        if server_id is None:
            return {"error": f"No MCP server found for tool '{tool_name}'"}
        return await self._mcp.call_tool(self._instance_id, server_id, tool_name, args)

    # ── God mode: generic Odoo execute ───────────────────────────────

    async def execute_god_mode(
        self, model_name: str, method: str, args: dict,
    ) -> dict:
        write_methods = ("create", "write", "unlink")
        if method in write_methods:
            raise ConfirmationRequired(
                "odoo_execute", f"{method} on {model_name}",
                {"model_name": model_name, "method": method, **args},
            )

        t = self._client._transport
        try:
            if method == "search_read":
                result = await t.search_read(
                    model_name, args.get("domain", []),
                    args.get("fields"), limit=args.get("limit", 20),
                )
                return {"records": result}
            elif method == "read":
                result = await t.read(
                    model_name, args.get("ids", []), args.get("fields"),
                )
                return {"records": result}
            elif method == "create":
                rid = await t.create(model_name, args.get("vals", {}))
                return {"id": rid}
            elif method == "write":
                await t.write(model_name, args.get("ids", []), args.get("vals", {}))
                return {"status": "ok"}
            elif method == "unlink":
                await t.unlink(model_name, args.get("ids", []))
                return {"status": "ok"}
            else:
                return {"error": f"Unknown method: {method}"}
        except Exception as exc:
            return {"error": str(exc)}
