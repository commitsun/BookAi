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

GOD_MODE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "odoo_list_models",
            "description": (
                "Search for Odoo models by name. Use this FIRST to find the correct "
                "model name before querying. Example: search 'payment' to find "
                "account.payment, search 'folio' to find pms.folio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Partial model name to search (e.g. 'folio', 'payment', 'reservation')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "odoo_get_fields",
            "description": (
                "Get field definitions for an Odoo model. Use this to discover "
                "available fields, their types, and relations before querying."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {"type": "string", "description": "Full model name (e.g. 'pms.folio', 'account.payment')"},
                },
                "required": ["model_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "odoo_search_read",
            "description": (
                "Search and read records from any Odoo model. For reading data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {"type": "string"},
                    "domain": {"type": "string", "description": "JSON search domain, e.g. '[[\"name\",\"=\",\"F2500008\"]]'. Use '[]' for no filter."},
                    "fields": {"type": "array", "items": {"type": "string"}, "description": "Fields to return. Empty = all fields."},
                    "limit": {"type": "integer", "description": "Max records (default 20)"},
                },
                "required": ["model_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "odoo_write",
            "description": (
                "Create, update or delete records in Odoo. ALWAYS requires confirmation. "
                "Describe what you will do before calling this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "model_name": {"type": "string"},
                    "method": {"type": "string", "enum": ["create", "write", "unlink"]},
                    "ids": {"type": "array", "items": {"type": "integer"}, "description": "Record IDs (for write/unlink)"},
                    "vals": {"type": "object", "description": "Values (for create/write)"},
                },
                "required": ["model_name", "method"],
            },
        },
    },
]


class ToolExecutor:
    def __init__(self, client: RoomdooClient, mcp_manager=None, instance_id: int = 0):
        self._client = client
        self._mcp = mcp_manager
        self._instance_id = instance_id

    # ── Build LLM tools from bindings ────────────────────────────────

    def build_llm_tools(self, agent: AgentConfig) -> list[dict]:
        if agent.god_mode:
            # God mode: introspection tools + all SDK tools + all agent tools
            tools = list(GOD_MODE_TOOLS)
            # Add regular agent tools too (SDK/MCP bindings)
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
        self, tool_name: str, args: dict,
    ) -> dict:
        """Execute a god mode tool (introspection or write operations)."""
        t = self._client._transport

        try:
            if tool_name == "odoo_list_models":
                query = args.get("query", "")
                result = await t.search_read(
                    "ir.model",
                    [("model", "ilike", query)],
                    ["model", "name"],
                    limit=20,
                )
                return {"models": [{"model": r["model"], "name": r["name"]} for r in result]}

            elif tool_name == "odoo_get_fields":
                model_name = args.get("model_name", "")
                fields = await t.call(model_name, "fields_get", args=[[]])
                # Simplify: return name, type, string, relation
                summary = {}
                for fname, fdata in fields.items():
                    if fname.startswith("__"):
                        continue
                    summary[fname] = {
                        "type": fdata.get("type"),
                        "string": fdata.get("string"),
                    }
                    if fdata.get("relation"):
                        summary[fname]["relation"] = fdata["relation"]
                return {"model": model_name, "fields": summary}

            elif tool_name == "odoo_search_read":
                model_name = args.get("model_name", "")
                domain = args.get("domain", "[]")
                if isinstance(domain, str):
                    try:
                        domain = json.loads(domain)
                    except json.JSONDecodeError:
                        domain = []
                fields = args.get("fields") or None
                limit = args.get("limit", 20)
                result = await t.search_read(model_name, domain, fields, limit=limit)
                return {"records": result, "count": len(result)}

            elif tool_name == "odoo_write":
                method = args.get("method", "write")
                model_name = args.get("model_name", "")
                raise ConfirmationRequired(
                    "odoo_write",
                    f"{method} on {model_name}",
                    args,
                )

            else:
                return {"error": f"Unknown god mode tool: {tool_name}"}

        except ConfirmationRequired:
            raise
        except Exception as exc:
            return {"error": str(exc)}
