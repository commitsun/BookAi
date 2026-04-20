"""
Converts AgentTool definitions to LLM function calling format and executes
tool invocations via the roomdoo-sdk.

The tool registry maps tool names (from Odoo's bookai.agent.tool) to SDK
methods. God mode agents get a generic odoo_execute tool instead.
"""

import json
import logging
from dataclasses import asdict

from roomdoo_sdk import RoomdooClient
from roomdoo_sdk.models import AgentConfig, AgentTool

log = logging.getLogger("tool_executor")


class ConfirmationRequired(Exception):
    """Raised when a tool requires human confirmation before execution."""
    def __init__(self, tool_name: str, description: str, args: dict):
        self.tool_name = tool_name
        self.description = description
        self.args = args
        super().__init__(f"Tool '{tool_name}' requires confirmation")


# ── Tool registry: maps tool names to SDK calls ─────────────────────

# Each entry: (description, parameters_schema)
# The actual execution is in _execute_tool()
TOOL_CATALOG: dict[str, dict] = {
    "folios.get_folio": {
        "description": "Get folio details by ID (dates, amounts, guest, status)",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer", "description": "Folio ID in the PMS"},
            },
            "required": ["folio_id"],
        },
    },
    "folios.get_reservations": {
        "description": "Get all reservations for a folio (rooms, dates, guests, prices)",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer", "description": "Folio ID"},
            },
            "required": ["folio_id"],
        },
    },
    "folios.get_reservation_lines": {
        "description": "Get daily price breakdown for a reservation",
        "parameters": {
            "type": "object",
            "properties": {
                "reservation_id": {"type": "integer", "description": "Reservation ID"},
            },
            "required": ["reservation_id"],
        },
    },
    "folios.get_checkin_partners": {
        "description": "Get guest checkin data for a folio (names, documents, status)",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer", "description": "Folio ID"},
            },
            "required": ["folio_id"],
        },
    },
    "folios.get_payments": {
        "description": "Get payment records for a folio",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer", "description": "Folio ID"},
            },
            "required": ["folio_id"],
        },
    },
    "folios.get_services": {
        "description": "Get extra services for a folio (board, products)",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer", "description": "Folio ID"},
            },
            "required": ["folio_id"],
        },
    },
    "folios.update_arrival_hour": {
        "description": "Update the arrival time for a folio's reservations",
        "parameters": {
            "type": "object",
            "properties": {
                "folio_id": {"type": "integer"},
                "arrival_hour": {"type": "string", "description": "HH:MM format"},
                "reservation_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "Optional: specific reservation IDs. If empty, updates all.",
                },
            },
            "required": ["folio_id", "arrival_hour"],
        },
    },
    "properties.list": {
        "description": "List all properties (hotels) in the system",
        "parameters": {"type": "object", "properties": {}},
    },
    "properties.get": {
        "description": "Get details of a specific property",
        "parameters": {
            "type": "object",
            "properties": {
                "property_id": {"type": "integer"},
            },
            "required": ["property_id"],
        },
    },
}

# God mode tool definition
GOD_MODE_TOOL = {
    "type": "function",
    "function": {
        "name": "odoo_execute",
        "description": (
            "Execute any operation on Odoo. Use for reading or modifying "
            "any data in the PMS. Write operations (create/write/unlink) "
            "require human confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string",
                    "description": "Odoo model (e.g. pms.reservation, pms.folio)",
                },
                "method": {
                    "type": "string",
                    "enum": ["search_read", "read", "create", "write", "unlink"],
                },
                "domain": {
                    "type": "array",
                    "description": "Search domain for search_read",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fields to read",
                },
                "ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Record IDs for read/write/unlink",
                },
                "vals": {
                    "type": "object",
                    "description": "Values for create/write",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max records for search_read",
                },
            },
            "required": ["model_name", "method"],
        },
    },
}


class ToolExecutor:
    def __init__(self, client: RoomdooClient):
        self._client = client

    def build_llm_tools(self, agent: AgentConfig) -> list[dict]:
        """Convert agent's tools to LLM function calling format."""
        if agent.god_mode:
            return [GOD_MODE_TOOL]

        tools = []
        for tool in agent.tools:
            if not tool.active:
                continue
            catalog_entry = TOOL_CATALOG.get(tool.name)
            if catalog_entry:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or catalog_entry["description"],
                        "parameters": catalog_entry["parameters"],
                    },
                })
            else:
                log.warning("Tool '%s' not found in catalog, skipping", tool.name)
        return tools

    async def execute(
        self, tool_name: str, args: dict, agent: AgentConfig,
    ) -> dict:
        """Execute a named tool via SDK. Returns serializable result."""
        # Check requires_confirm
        tool_def = next(
            (t for t in agent.tools if t.name == tool_name), None,
        )
        if tool_def and tool_def.requires_confirm:
            raise ConfirmationRequired(
                tool_name,
                tool_def.description or tool_name,
                args,
            )

        return await self._dispatch(tool_name, args)

    async def execute_god_mode(
        self, model_name: str, method: str, args: dict,
    ) -> dict:
        """Execute a generic Odoo operation (god mode)."""
        write_methods = ("create", "write", "unlink")
        if method in write_methods:
            raise ConfirmationRequired(
                "odoo_execute",
                f"{method} on {model_name}",
                {"model_name": model_name, "method": method, **args},
            )
        return await self._dispatch_god(model_name, method, args)

    async def _dispatch(self, tool_name: str, args: dict) -> dict:
        """Route a tool call to the appropriate SDK method."""
        try:
            if tool_name == "folios.get_folio":
                result = await self._client.folios.get_folio(args["folio_id"])
                return asdict(result)
            elif tool_name == "folios.get_reservations":
                results = await self._client.folios.get_reservations(args["folio_id"])
                return {"reservations": [asdict(r) for r in results]}
            elif tool_name == "folios.get_reservation_lines":
                results = await self._client.folios.get_reservation_lines(args["reservation_id"])
                return {"lines": [asdict(r) for r in results]}
            elif tool_name == "folios.get_checkin_partners":
                results = await self._client.folios.get_checkin_partners(args["folio_id"])
                return {"partners": [asdict(r) for r in results]}
            elif tool_name == "folios.get_payments":
                results = await self._client.folios.get_payments(args["folio_id"])
                return {"payments": [asdict(r) for r in results]}
            elif tool_name == "folios.get_services":
                results = await self._client.folios.get_services(args["folio_id"])
                return {"services": [asdict(r) for r in results]}
            elif tool_name == "folios.update_arrival_hour":
                await self._client.folios.update_arrival_hour(
                    args["folio_id"], args["arrival_hour"],
                    args.get("reservation_ids"),
                )
                return {"status": "ok"}
            elif tool_name == "properties.list":
                results = await self._client.properties.list()
                return {"properties": [asdict(r) for r in results]}
            elif tool_name == "properties.get":
                result = await self._client.properties.get(args["property_id"])
                return asdict(result)
            else:
                return {"error": f"Unknown tool: {tool_name}"}
        except Exception as exc:
            log.error("Tool execution failed: %s(%s) — %s", tool_name, args, exc)
            return {"error": str(exc)}

    async def _dispatch_god(
        self, model_name: str, method: str, args: dict,
    ) -> dict:
        """Execute a generic Odoo operation."""
        t = self._client._transport
        try:
            if method == "search_read":
                result = await t.search_read(
                    model_name,
                    args.get("domain", []),
                    args.get("fields"),
                    limit=args.get("limit", 20),
                )
                return {"records": result}
            elif method == "read":
                result = await t.read(
                    model_name, args.get("ids", []), args.get("fields"),
                )
                return {"records": result}
            elif method == "create":
                record_id = await t.create(model_name, args.get("vals", {}))
                return {"id": record_id}
            elif method == "write":
                await t.write(model_name, args.get("ids", []), args.get("vals", {}))
                return {"status": "ok"}
            elif method == "unlink":
                await t.unlink(model_name, args.get("ids", []))
                return {"status": "ok"}
            else:
                return {"error": f"Unknown method: {method}"}
        except Exception as exc:
            log.error("God mode failed: %s.%s — %s", model_name, method, exc)
            return {"error": str(exc)}
