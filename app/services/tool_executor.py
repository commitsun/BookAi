"""
Tool execution engine. Converts AgentToolBinding definitions to LLM function
calling format and dispatches invocations based on tool_type.

Supported tool types:
- sdk: Execute via roomdoo-sdk methods (folios.get_folio, etc.)
- mcp: Execute via MCP server (future)
- webhook: POST to an external URL (future)
- function: Internal BookAI function (future)

Tool names are sanitized for LLM compatibility (dots → double underscores).
The input_schema comes from the binding, not from a hardcoded catalog.
"""

import json
import logging
from dataclasses import asdict

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
        self._mcp = mcp_manager  # MCPManager | None
        self._instance_id = instance_id

    # ── Build LLM tools from bindings ────────────────────────────────

    def build_llm_tools(self, agent: AgentConfig) -> list[dict]:
        """Convert agent's tool bindings to LLM function calling format."""
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
        """Execute a tool call. Dispatches based on tool_type."""
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

    # ── God mode ─────────────────────────────────────────────────────

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

    # ── MCP tool dispatch ────────────────────────────────────────────

    async def _execute_mcp(self, tool_name: str, args: dict) -> dict:
        """Execute a tool via MCP server."""
        if not self._mcp:
            return {"error": "MCP manager not available"}
        server_id = self._mcp.find_server_for_tool(self._instance_id, tool_name)
        if server_id is None:
            return {"error": f"No MCP server found for tool '{tool_name}'"}
        return await self._mcp.call_tool(self._instance_id, server_id, tool_name, args)

    # ── SDK tool dispatch ────────────────────────────────────────────

    async def _execute_sdk(self, sdk_method: str, args: dict) -> dict:
        """Route an SDK tool call to the appropriate client method."""
        try:
            # folios.*
            if sdk_method == "folios.search_by_code":
                records = await self._client._transport.search_read(
                    "pms.folio",
                    [("name", "ilike", args["code"])],
                    ["id", "name", "state", "partner_name",
                     "first_checkin", "last_checkout",
                     "amount_total", "pending_amount"],
                    limit=5,
                )
                return {"folios": records}
            elif sdk_method == "folios.get_folio":
                r = await self._client.folios.get_folio(args["folio_id"])
                return asdict(r)
            elif sdk_method == "folios.get_reservations":
                rs = await self._client.folios.get_reservations(args["folio_id"])
                return {"reservations": [asdict(r) for r in rs]}
            elif sdk_method == "folios.get_reservation_lines":
                rs = await self._client.folios.get_reservation_lines(args["reservation_id"])
                return {"lines": [asdict(r) for r in rs]}
            elif sdk_method == "folios.get_checkin_partners":
                rs = await self._client.folios.get_checkin_partners(args["folio_id"])
                return {"partners": [asdict(r) for r in rs]}
            elif sdk_method == "folios.get_payments":
                rs = await self._client.folios.get_payments(args["folio_id"])
                return {"payments": [asdict(r) for r in rs]}
            elif sdk_method == "folios.get_services":
                rs = await self._client.folios.get_services(args["folio_id"])
                return {"services": [asdict(r) for r in rs]}
            elif sdk_method == "folios.update_arrival_hour":
                await self._client.folios.update_arrival_hour(
                    args["folio_id"], args["arrival_hour"],
                    args.get("reservation_ids"),
                )
                return {"status": "ok"}
            # properties.*
            elif sdk_method == "properties.list":
                rs = await self._client.properties.list()
                return {"properties": [asdict(r) for r in rs]}
            elif sdk_method == "properties.get":
                r = await self._client.properties.get(args["property_id"])
                return asdict(r)
            # Generic search for property sub-resources
            elif sdk_method in (
                "properties.get_amenities",
                "properties.get_board_services",
                "properties.get_room_types",
            ):
                return await self._property_sub_resource(sdk_method, args)
            # agents/kb (for god-mode or meta-agents)
            elif sdk_method == "agents.get":
                r = await self._client.agents.get(args["technical_name"])
                return {"name": r.name, "description": r.description}
            elif sdk_method == "agents.list":
                rs = await self._client.agents.list(
                    active=args.get("active", True),
                    caller_type=args.get("caller_type"),
                )
                return {"agents": [{"name": a.name, "technical_name": a.technical_name} for a in rs]}
            elif sdk_method == "kb.list_by_agent":
                rs = await self._client.kb.list_by_agent(args["technical_name"])
                return {"documents": [{"name": d.name, "content": d.content[:500] if d.content else None} for d in rs]}
            else:
                return {"error": f"Unknown SDK method: {sdk_method}"}
        except Exception as exc:
            log.error("SDK tool failed: %s(%s) — %s", sdk_method, args, exc)
            return {"error": str(exc)}

    async def _property_sub_resource(self, method: str, args: dict) -> dict:
        model_map = {
            "properties.get_amenities": ("pms.amenity", "pms_property_ids"),
            "properties.get_board_services": ("pms.board.service", "pms_property_ids"),
            "properties.get_room_types": ("pms.room.type", "pms_property_ids"),
        }
        model, field = model_map[method]
        try:
            records = await self._client._transport.search_read(
                model,
                [(field, "in", [args["property_id"]])],
                ["name"],
                limit=50,
            )
            return {"records": records}
        except Exception as exc:
            return {"error": str(exc)}
