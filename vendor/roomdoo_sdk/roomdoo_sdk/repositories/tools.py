"""
Generic tool executor for SDK tools.

BookAI calls `client.tools.execute(name, args)` without knowing
the internal implementation. The dispatch lives here in the SDK.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from ..exceptions import ToolExecutionError, ToolNotFoundError
from ..models.usage import UsageRecord
from ..transports.base import Transport

log = logging.getLogger("roomdoo_sdk.tools")


class ToolRepository:
    def __init__(self, transport: Transport, client):
        self._transport = transport
        self._client = client

    async def list_all(self) -> list[dict]:
        """List all active tools from the global catalog."""
        return await self._transport.search_read(
            "bookai.tool",
            [("active", "=", True)],
            fields=[
                "id", "name", "description", "tool_type",
                "sdk_method", "requires_confirm",
            ],
        )

    async def list_by_agent(
        self, technical_name: str
    ) -> list[dict]:
        """List tools bound to an agent."""
        agents = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=["tool_binding_ids"],
            limit=1,
        )
        if not agents or not agents[0].get(
            "tool_binding_ids"
        ):
            return []
        bindings = await self._transport.read(
            "bookai.agent.tool.binding",
            agents[0]["tool_binding_ids"],
            fields=[
                "tool_id", "description_override",
                "requires_confirm", "active",
            ],
        )
        tool_ids = [
            b["tool_id"][0]
            for b in bindings
            if b.get("tool_id")
        ]
        if not tool_ids:
            return []
        tools = await self._transport.read(
            "bookai.tool",
            tool_ids,
            fields=["id", "name", "tool_type", "description"],
        )
        tools_map = {t["id"]: t for t in tools}
        result = []
        for b in bindings:
            tid = b["tool_id"][0]
            t = tools_map.get(tid, {})
            result.append({
                "binding_id": b["id"],
                "tool_id": tid,
                "tool_name": t.get("name", ""),
                "tool_type": t.get("tool_type", ""),
                "description": (
                    b.get("description_override")
                    or t.get("description", "")
                ),
                "requires_confirm": b.get(
                    "requires_confirm", False
                ),
                "active": b.get("active", True),
            })
        return result

    async def bind_to_agent(
        self, technical_name: str, tool_name: str, **kwargs
    ) -> int:
        """Bind a tool to an agent. Returns binding ID."""
        agents = await self._transport.search_read(
            "bookai.agent",
            [("technical_name", "=", technical_name)],
            fields=["id"],
            limit=1,
        )
        if not agents:
            raise ToolNotFoundError(
                f"Agent '{technical_name}' not found"
            )
        tools = await self._transport.search_read(
            "bookai.tool",
            [("name", "=", tool_name)],
            fields=["id"],
            limit=1,
        )
        if not tools:
            raise ToolNotFoundError(
                f"Tool '{tool_name}' not found"
            )
        vals = {
            "agent_id": agents[0]["id"],
            "tool_id": tools[0]["id"],
        }
        vals.update(kwargs)
        return await self._transport.create(
            "bookai.agent.tool.binding", vals
        )

    async def unbind_from_agent(
        self, binding_id: int
    ) -> None:
        """Remove a tool binding from an agent."""
        await self._transport.unlink(
            "bookai.agent.tool.binding", [binding_id]
        )

    async def execute(self, tool_name: str, args: dict) -> dict:
        """Execute an SDK tool by name. Returns a serializable dict.

        Raises ToolNotFoundError if the tool doesn't exist.
        Raises ToolExecutionError on execution failure.
        """
        handler = _DISPATCH.get(tool_name)
        if handler is None:
            raise ToolNotFoundError(f"SDK tool '{tool_name}' not found")
        try:
            return await handler(self._client, self._transport, args)
        except (ToolNotFoundError, ToolExecutionError):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                f"Tool '{tool_name}' failed: {exc}"
            ) from exc


# ── Internal dispatch table ──────────────────────────────────────────
# Each handler: async (client, transport, args) -> dict

async def _properties_list(client, transport, args):
    results = await client.properties.list()
    return {"properties": [asdict(r) for r in results]}


async def _properties_get(client, transport, args):
    r = await client.properties.get(args["property_id"])
    return asdict(r)


async def _folios_get_folio(client, transport, args):
    r = await client.folios.get_folio(args["folio_id"])
    return asdict(r)


async def _folios_get_reservations(client, transport, args):
    results = await client.folios.get_reservations(args["folio_id"])
    return {"reservations": [asdict(r) for r in results]}


async def _folios_get_reservation_lines(client, transport, args):
    results = await client.folios.get_reservation_lines(args["reservation_id"])
    return {"lines": [asdict(r) for r in results]}


async def _folios_get_checkin_partners(client, transport, args):
    results = await client.folios.get_checkin_partners(args["folio_id"])
    return {"partners": [asdict(r) for r in results]}


async def _folios_get_payments(client, transport, args):
    results = await client.folios.get_payments(args["folio_id"])
    return {"payments": [asdict(r) for r in results]}


async def _folios_get_services(client, transport, args):
    results = await client.folios.get_services(args["folio_id"])
    return {"services": [asdict(r) for r in results]}


async def _folios_update_arrival_hour(client, transport, args):
    await client.folios.update_arrival_hour(
        args["folio_id"], args["arrival_hour"],
        args.get("reservation_ids"),
    )
    return {"status": "ok"}


async def _folios_search_by_code(client, transport, args):
    records = await transport.search_read(
        "pms.folio",
        [("name", "ilike", args["code"])],
        ["id", "name", "state", "partner_name",
         "first_checkin", "last_checkout",
         "amount_total", "pending_amount"],
        limit=5,
    )
    return {"folios": records}


async def _agents_get(client, transport, args):
    a = await client.agents.get(args["technical_name"])
    return {"name": a.name, "technical_name": a.technical_name, "description": a.description}


async def _agents_list(client, transport, args):
    results = await client.agents.list(
        active=args.get("active", True),
        caller_type=args.get("caller_type"),
    )
    return {"agents": [{"name": a.name, "technical_name": a.technical_name} for a in results]}


async def _kb_list_by_agent(client, transport, args):
    results = await client.kb.list_by_agent(args["technical_name"])
    return {
        "documents": [
            {"name": d.name, "content": d.content[:500] if d.content else None}
            for d in results
        ]
    }


async def _generic_property_search(model, field):
    async def handler(client, transport, args):
        records = await transport.search_read(
            model,
            [(field, "in", [args["property_id"]])],
            ["name"],
            limit=50,
        )
        return {"records": records}
    return handler


async def _generic_property_resource(model, field, client, transport, args, extra_fields=None):
    """Return records linked to the property OR available to all (empty field).

    In Odoo/PMS, a Many2many pms_property_ids with no values means the
    record is available for all properties — not that it belongs to none.
    """
    pid = args.get("property_id")
    domain = [
        "|",
        (field, "in", [pid]),
        (field, "=", False),
    ] if pid else []
    fields = ["name"] + (extra_fields or [])
    records = await transport.search_read(
        model, domain, fields, limit=50,
    )
    return {"records": records}


async def _properties_get_room_types(client, transport, args):
    return await _generic_property_resource(
        "pms.room.type", "pms_property_ids", client, transport, args,
        extra_fields=["default_code", "list_price"],
    )


async def _properties_get_amenities(client, transport, args):
    return await _generic_property_resource(
        "pms.amenity", "pms_property_ids", client, transport, args,
    )


async def _properties_get_board_services(client, transport, args):
    return await _generic_property_resource(
        "pms.board.service", "pms_property_ids", client, transport, args,
        extra_fields=["default_code", "list_price"],
    )


async def _properties_get_rooms(client, transport, args):
    results = await client.properties.get_rooms(args["property_id"])
    return {"rooms": [asdict(r) for r in results]}


async def _properties_get_pricelists(client, transport, args):
    results = await client.properties.get_pricelists(args["property_id"])
    return {"pricelists": [asdict(r) for r in results]}


async def _properties_get_cancelation_policy(client, transport, args):
    r = await client.properties.get_cancelation_policy(args["policy_id"])
    return asdict(r)


async def _availability_check(client, transport, args):
    results = await client.availability.check(
        args["property_id"],
        args["checkin"],
        args["checkout"],
        args["pricelist_id"],
        args.get("room_type_id"),
    )
    return {"availability": [asdict(r) for r in results]}


async def _availability_get_prices(client, transport, args):
    result = await client.availability.get_prices(
        args["property_id"],
        args["checkin"],
        args["checkout"],
        args["room_type_id"],
        args["pricelist_id"],
    )
    return asdict(result)


async def _availability_get_all_prices(client, transport, args):
    results = await client.availability.get_all_prices(
        args["property_id"],
        args["checkin"],
        args["checkout"],
        args["room_type_id"],
    )
    return {"pricelists": [asdict(r) for r in results]}


async def _folios_search_by_guest(client, transport, args):
    results = await client.folios.search_by_guest(
        email=args.get("email"),
        phone=args.get("phone"),
        name=args.get("name"),
        property_id=args.get("property_id"),
        limit=args.get("limit", 10),
    )
    return {"folios": [asdict(r) for r in results]}


async def _folios_my_folios(client, transport, args):
    results = await client.folios.my_folios(args["phone"])
    return {"folios": [asdict(r) for r in results]}


async def _folios_my_folio(client, transport, args):
    r = await client.folios.my_folio(args["phone"], args["folio_id"])
    return asdict(r)


async def _folios_my_reservations(client, transport, args):
    results = await client.folios.my_reservations(args["phone"], args["folio_id"])
    return {"reservations": [asdict(r) for r in results]}


async def _folios_my_reservation_lines(client, transport, args):
    results = await client.folios.my_reservation_lines(args["phone"], args["reservation_id"])
    return {"lines": [asdict(r) for r in results]}


async def _folios_my_services(client, transport, args):
    results = await client.folios.my_services(args["phone"], args["folio_id"])
    return {"services": [asdict(r) for r in results]}


async def _folios_my_payments(client, transport, args):
    results = await client.folios.my_payments(args["phone"], args["folio_id"])
    return {"payments": [asdict(r) for r in results]}


async def _folios_my_checkin_partners(client, transport, args):
    results = await client.folios.my_checkin_partners(args["phone"], args["folio_id"])
    return {"partners": [asdict(r) for r in results]}


async def _folios_my_update_arrival(client, transport, args):
    await client.folios.my_update_arrival(args["phone"], args["folio_id"], args["arrival_hour"])
    return {"status": "ok"}


async def _reservations_create_booking(client, transport, args):
    result = await client.reservations.create_booking(
        property_id=args["property_id"],
        partner_name=args["partner_name"],
        pricelist_id=args["pricelist_id"],
        sale_channel_id=args["sale_channel_id"],
        reservations=args.get("reservations"),
        room_type_id=args.get("room_type_id"),
        checkin=args.get("checkin"),
        checkout=args.get("checkout"),
        adults=args.get("adults", 2),
        children=args.get("children", 0),
        partner_phone=args.get("partner_phone"),
        partner_email=args.get("partner_email"),
    )
    return result


# ── Reservations — Staff ────────────────────────────────────────────


async def _reservations_search(client, transport, args):
    results = await client.reservations.search(
        property_id=args.get("property_id"),
        checkin_from=args.get("checkin_from"),
        checkin_to=args.get("checkin_to"),
        state=args.get("state"),
        partner_name=args.get("partner_name"),
    )
    return {"reservations": [asdict(r) for r in results]}


async def _reservations_get(client, transport, args):
    r = await client.reservations.get(args["reservation_id"])
    return asdict(r)


async def _reservations_confirm(client, transport, args):
    await client.reservations.confirm(args["reservation_id"])
    return {"status": "ok"}


async def _reservations_cancel(client, transport, args):
    await client.reservations.cancel(
        args["reservation_id"], args.get("reason"),
    )
    return {"status": "ok"}


async def _reservations_assign_room(client, transport, args):
    await client.reservations.assign_room(
        args["reservation_id"], args["room_id"],
    )
    return {"status": "ok"}


async def _reservations_checkin(client, transport, args):
    await client.reservations.checkin(args["checkin_partner_id"])
    return {"status": "ok"}


async def _reservations_checkout(client, transport, args):
    await client.reservations.checkout(args["reservation_id"])
    return {"status": "ok"}


# ── Guests ──────────────────────────────────────────────────────────


async def _guests_search(client, transport, args):
    results = await client.guests.search(
        name=args.get("name"),
        email=args.get("email"),
        phone=args.get("phone"),
        document_number=args.get("document_number"),
    )
    return {"guests": [asdict(r) for r in results]}


async def _guests_get(client, transport, args):
    r = await client.guests.get(args["partner_id"])
    return asdict(r)


async def _guests_get_history(client, transport, args):
    results = await client.guests.get_history(args["partner_id"])
    return {"folios": [asdict(r) for r in results]}


async def _guests_update_contact(client, transport, args):
    await client.guests.update_contact(
        args["partner_id"],
        email=args.get("email"),
        phone=args.get("phone"),
        mobile=args.get("mobile"),
    )
    return {"status": "ok"}


# ── Invoices ────────────────────────────────────────────────────────


async def _invoices_list_by_folio(client, transport, args):
    results = await client.invoices.list_by_folio(args["folio_id"])
    return {"invoices": [asdict(r) for r in results]}


async def _invoices_get(client, transport, args):
    r = await client.invoices.get(args["invoice_id"])
    return asdict(r)


async def _invoices_create_from_folio(client, transport, args):
    ids = await client.invoices.create_from_folio(args["folio_id"])
    return {"invoice_ids": ids}


async def _invoices_validate(client, transport, args):
    await client.invoices.validate(args["invoice_id"])
    return {"status": "ok"}


async def _invoices_get_pdf_url(client, transport, args):
    url = await client.invoices.get_pdf_url(args["invoice_id"])
    return {"url": url}


# ── Payments ────────────────────────────────────────────────────────


async def _payments_record(client, transport, args):
    payment_id = await client.payments.record(
        args["folio_id"], args["amount"], args["journal_id"],
    )
    return {"payment_id": payment_id}


async def _payments_list_by_property(client, transport, args):
    results = await client.payments.list_by_property(
        args["property_id"],
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
    )
    return {"payments": [asdict(r) for r in results]}


async def _payments_get_pending_by_property(client, transport, args):
    results = await client.payments.get_pending_by_property(
        args["property_id"],
    )
    return {"folios": [asdict(r) for r in results]}


# ── Reporting ───────────────────────────────────────────────────────


async def _reporting_occupancy(client, transport, args):
    results = await client.reporting.occupancy(
        args["property_id"], args["date_from"], args["date_to"],
    )
    return {"occupancy": [asdict(r) for r in results]}


async def _reporting_revenue_summary(client, transport, args):
    r = await client.reporting.revenue_summary(
        args["property_id"], args["date_from"], args["date_to"],
    )
    return asdict(r)


async def _reporting_arrivals_departures(client, transport, args):
    result = await client.reporting.arrivals_departures(
        args["property_id"], args["date"],
    )
    return {
        "arrivals": [asdict(r) for r in result["arrivals"]],
        "departures": [asdict(r) for r in result["departures"]],
    }


async def _reporting_pending_checkins(client, transport, args):
    results = await client.reporting.pending_checkins(
        args["property_id"],
    )
    return {"reservations": [asdict(r) for r in results]}


# ── Availability — Staff ────────────────────────────────────────────


async def _availability_check_real(client, transport, args):
    results = await client.availability.check_real(
        args["property_id"],
        args["checkin"],
        args["checkout"],
        args.get("room_type_id"),
    )
    return {"availability": [asdict(r) for r in results]}


# ── Revenue Management ──────────────────────────────────────────────


async def _revenue_get_availability_rules(client, transport, args):
    results = await client.revenue.get_availability_rules(
        args["property_id"],
        args["date_from"],
        args["date_to"],
        room_type_id=args.get("room_type_id"),
        availability_plan_id=args.get("availability_plan_id"),
    )
    return {"rules": results}


async def _revenue_get_pricelist_items(client, transport, args):
    results = await client.revenue.get_pricelist_items(
        args["pricelist_id"],
        args["date_from"],
        args["date_to"],
        room_type_id=args.get("room_type_id"),
    )
    return {"items": results}


async def _revenue_set_prices(client, transport, args):
    count = await client.revenue.set_prices(
        args["pricelist_id"],
        args["room_type_id"],
        args["date_from"],
        args["date_to"],
        args["price"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_close_sales(client, transport, args):
    count = await client.revenue.close_sales(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_open_sales(client, transport, args):
    count = await client.revenue.open_sales(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_close_arrivals(client, transport, args):
    count = await client.revenue.close_arrivals(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_close_departures(client, transport, args):
    count = await client.revenue.close_departures(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_set_min_stay(client, transport, args):
    count = await client.revenue.set_min_stay(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        args["min_stay"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_set_max_stay(client, transport, args):
    count = await client.revenue.set_max_stay(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        args["max_stay"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


async def _revenue_set_quota(client, transport, args):
    count = await client.revenue.set_quota(
        args["availability_plan_id"],
        args["room_type_id"],
        args["property_id"],
        args["date_from"],
        args["date_to"],
        args["quota"],
        days_of_week=args.get("days_of_week"),
    )
    return {"updated_days": count}


# ── Agents — Admin ──────────────────────────────────────────────────


async def _agents_create(client, transport, args):
    agent_id = await client.agents.create(
        technical_name=args["technical_name"],
        name=args["name"],
        description=args["description"],
        system_prompt=args["system_prompt"],
        caller_type=args.get("caller_type", "any"),
    )
    return {"agent_id": agent_id}


async def _agents_update(client, transport, args):
    vals = {k: v for k, v in args.items() if k != "technical_name"}
    await client.agents.update(args["technical_name"], **vals)
    return {"status": "ok"}


async def _agents_update_prompt(client, transport, args):
    await client.agents.update_prompt(
        args["technical_name"], args["system_prompt"],
    )
    return {"status": "ok"}


# ── KB — Admin ──────────────────────────────────────────────────────


async def _kb_create(client, transport, args):
    doc_id = await client.kb.create_document(
        name=args["name"],
        source_type=args.get("source_type", "markdown"),
        content=args.get("content"),
        doc_type=args.get("doc_type"),
    )
    return {"doc_id": doc_id}


async def _kb_update(client, transport, args):
    vals = {k: v for k, v in args.items() if k != "doc_id"}
    await client.kb.update_document(args["doc_id"], **vals)
    return {"status": "ok"}


# ── Usage ───────────────────────────────────────────────────────────


async def _usage_log(client, transport, args):
    record = UsageRecord(
        pms_property_id=args["pms_property_id"],
        agent_id=args["agent_id"],
        llm_account_id=args["llm_account_id"],
        tokens_in=args["tokens_in"],
        tokens_out=args["tokens_out"],
        model=args["model"],
        conversation_id=args["conversation_id"],
        status=args["status"],
    )
    await client.usage.log(record)
    return {"status": "ok"}


async def _usage_summary_by_agent(client, transport, args):
    results = await client.usage.summary_by_agent(
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
    )
    return {"summary": results}


async def _usage_summary_by_property(client, transport, args):
    results = await client.usage.summary_by_property(
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
    )
    return {"summary": results}


async def _usage_summary_by_model(client, transport, args):
    results = await client.usage.summary_by_model(
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
    )
    return {"summary": results}


# ── Templates ───────────────────────────────────────────────────────


async def _templates_update_status(client, transport, args):
    ok = await client.templates.update_translation_status(
        template_code=args["template_code"],
        language=args["language"],
        meta_status=args["meta_status"],
        meta_template_id=args.get("meta_template_id"),
        waba_id=args.get("waba_id"),
    )
    return {"updated": ok}


# Build dispatch table
_DISPATCH: dict[str, callable] = {
    # Properties
    "properties.list": _properties_list,
    "properties.get": _properties_get,
    "properties.get_room_types": _properties_get_room_types,
    "properties.get_rooms": _properties_get_rooms,
    "properties.get_amenities": _properties_get_amenities,
    "properties.get_board_services": _properties_get_board_services,
    "properties.get_pricelists": _properties_get_pricelists,
    "properties.get_cancelation_policy": _properties_get_cancelation_policy,
    # Availability
    "availability.check": _availability_check,
    "availability.get_prices": _availability_get_prices,
    "availability.get_all_prices": _availability_get_all_prices,
    "availability.check_real": _availability_check_real,
    # Folios — My (phone-validated)
    "folios.my_folios": _folios_my_folios,
    "folios.my_folio": _folios_my_folio,
    "folios.my_reservations": _folios_my_reservations,
    "folios.my_reservation_lines": _folios_my_reservation_lines,
    "folios.my_services": _folios_my_services,
    "folios.my_payments": _folios_my_payments,
    "folios.my_checkin_partners": _folios_my_checkin_partners,
    "folios.my_update_arrival": _folios_my_update_arrival,
    # Folios — Internal
    "folios.get_folio": _folios_get_folio,
    "folios.get_reservations": _folios_get_reservations,
    "folios.get_reservation_lines": _folios_get_reservation_lines,
    "folios.get_checkin_partners": _folios_get_checkin_partners,
    "folios.get_payments": _folios_get_payments,
    "folios.get_services": _folios_get_services,
    "folios.update_arrival_hour": _folios_update_arrival_hour,
    "folios.search_by_guest": _folios_search_by_guest,
    "folios.search_by_code": _folios_search_by_code,
    # Reservations
    "reservations.create_booking": _reservations_create_booking,
    "reservations.search": _reservations_search,
    "reservations.get": _reservations_get,
    "reservations.confirm": _reservations_confirm,
    "reservations.cancel": _reservations_cancel,
    "reservations.assign_room": _reservations_assign_room,
    "reservations.checkin": _reservations_checkin,
    "reservations.checkout": _reservations_checkout,
    # Guests
    "guests.search": _guests_search,
    "guests.get": _guests_get,
    "guests.get_history": _guests_get_history,
    "guests.update_contact": _guests_update_contact,
    # Invoices
    "invoices.list_by_folio": _invoices_list_by_folio,
    "invoices.get": _invoices_get,
    "invoices.create_from_folio": _invoices_create_from_folio,
    "invoices.validate": _invoices_validate,
    "invoices.get_pdf_url": _invoices_get_pdf_url,
    # Payments
    "payments.record": _payments_record,
    "payments.list_by_property": _payments_list_by_property,
    "payments.get_pending_by_property": _payments_get_pending_by_property,
    # Reporting
    "reporting.occupancy": _reporting_occupancy,
    "reporting.revenue_summary": _reporting_revenue_summary,
    "reporting.arrivals_departures": _reporting_arrivals_departures,
    "reporting.pending_checkins": _reporting_pending_checkins,
    # Revenue Management
    "revenue.get_availability_rules": _revenue_get_availability_rules,
    "revenue.get_pricelist_items": _revenue_get_pricelist_items,
    "revenue.set_prices": _revenue_set_prices,
    "revenue.close_sales": _revenue_close_sales,
    "revenue.open_sales": _revenue_open_sales,
    "revenue.close_arrivals": _revenue_close_arrivals,
    "revenue.close_departures": _revenue_close_departures,
    "revenue.set_min_stay": _revenue_set_min_stay,
    "revenue.set_max_stay": _revenue_set_max_stay,
    "revenue.set_quota": _revenue_set_quota,
    # Agents
    "agents.get": _agents_get,
    "agents.list": _agents_list,
    "agents.create": _agents_create,
    "agents.update": _agents_update,
    "agents.update_prompt": _agents_update_prompt,
    # KB
    "kb.list_by_agent": _kb_list_by_agent,
    "kb.create": _kb_create,
    "kb.update": _kb_update,
    # Usage
    "usage.log": _usage_log,
    "usage.summary_by_agent": _usage_summary_by_agent,
    "usage.summary_by_property": _usage_summary_by_property,
    "usage.summary_by_model": _usage_summary_by_model,
    # Templates
    "templates.update_status": _templates_update_status,
}
