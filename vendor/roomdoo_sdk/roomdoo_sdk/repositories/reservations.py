from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.folio import Reservation
from ..transports.base import Transport

_RESERVATION_FIELDS = [
    "id", "name", "folio_id", "state",
    "room_type_id", "preferred_room_id", "rooms",
    "checkin", "checkout", "arrival_hour", "departure_hour",
    "nights", "partner_id", "partner_name",
    "adults", "children", "partner_requests",
    "price_total", "price_subtotal", "price_tax",
    "price_services", "discount",
    "board_service_room_id", "overbooking",
    "cancelled_reason",
    "checkin_partner_count", "checkin_partner_pending_count",
    "sale_channel_origin_id",
    "reservation_line_ids", "service_ids",
    "checkin_partner_ids",
]


def _m2o_id(data, field):
    val = data.get(field)
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    return None


def _m2o_name(data, field):
    val = data.get(field)
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return val[1]
    return None


class ReservationRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def search(
        self,
        property_id: int | None = None,
        checkin_from: str | None = None,
        checkin_to: str | None = None,
        state: str | None = None,
        partner_name: str | None = None,
        sale_channel_id: int | None = None,
        limit: int = 20,
    ) -> list[Reservation]:
        """Search reservations with filters."""
        domain: list = []
        if property_id:
            domain.append(
                ("pms_property_id", "=", property_id)
            )
        if checkin_from:
            domain.append(("checkin", ">=", checkin_from))
        if checkin_to:
            domain.append(("checkin", "<=", checkin_to))
        if state:
            domain.append(("state", "=", state))
        if partner_name:
            domain.append(
                ("partner_name", "ilike", partner_name)
            )
        if sale_channel_id:
            domain.append(
                ("sale_channel_origin_id", "=", sale_channel_id)
            )
        records = await self._transport.search_read(
            "pms.reservation",
            domain,
            fields=_RESERVATION_FIELDS,
            limit=limit,
            order="checkin desc",
        )
        return [_build_reservation(r) for r in records]

    async def get(self, reservation_id: int) -> Reservation:
        """Get a single reservation by ID."""
        records = await self._transport.read(
            "pms.reservation",
            [reservation_id],
            fields=_RESERVATION_FIELDS,
        )
        if not records:
            raise NotFoundError(
                f"Reservation {reservation_id} not found"
            )
        return _build_reservation(records[0])

    async def confirm(self, reservation_id: int) -> None:
        """Confirm a draft reservation."""
        await self._transport.call(
            "pms.reservation",
            "action_confirm",
            args=[[reservation_id]],
        )

    async def cancel(
        self, reservation_id: int, reason: str | None = None
    ) -> None:
        """Cancel a reservation."""
        await self._transport.call(
            "pms.reservation",
            "action_cancel",
            args=[[reservation_id]],
        )
        if reason:
            await self._transport.write(
                "pms.reservation",
                [reservation_id],
                {"cancelled_reason": reason},
            )

    async def assign_room(
        self, reservation_id: int, room_id: int
    ) -> None:
        """Assign a specific room to a reservation."""
        await self._transport.write(
            "pms.reservation",
            [reservation_id],
            {"preferred_room_id": room_id},
        )

    async def checkin(self, checkin_partner_id: int) -> None:
        """Mark a guest as checked in (on board)."""
        await self._transport.call(
            "pms.checkin.partner",
            "action_on_board",
            args=[[checkin_partner_id]],
        )

    async def checkout(self, reservation_id: int) -> None:
        """Checkout a reservation."""
        await self._transport.call(
            "pms.reservation",
            "action_reservation_checkout",
            args=[[reservation_id]],
        )

    async def create_booking(
        self,
        property_id: int,
        partner_name: str,
        pricelist_id: int,
        sale_channel_id: int,
        reservations: list[dict] | None = None,
        # Legacy single-room params (backward compat)
        room_type_id: int | None = None,
        checkin: str | None = None,
        checkout: str | None = None,
        adults: int = 2,
        children: int = 0,
        partner_phone: str | None = None,
        partner_email: str | None = None,
    ) -> dict:
        """Create folio + reservations + confirm.

        Accepts either a ``reservations`` array (preferred)
        or legacy single-room params for backward
        compatibility.

        Returns dict with folio_id, folio_name,
        reservation_ids, amount_total, first_checkin,
        last_checkout.
        """
        # Normalize to reservations array
        if not reservations:
            if not room_type_id or not checkin or not checkout:
                raise ValueError(
                    "Either 'reservations' array or "
                    "room_type_id/checkin/checkout required"
                )
            reservations = [
                {
                    "room_type_id": room_type_id,
                    "checkin": checkin,
                    "checkout": checkout,
                    "adults": adults,
                    "children": children,
                }
            ]

        # 1. Create folio
        folio_vals: dict = {
            "pms_property_id": property_id,
            "partner_name": partner_name,
            "sale_channel_origin_id": sale_channel_id,
            "pricelist_id": pricelist_id,
        }
        if partner_phone:
            folio_vals["mobile"] = partner_phone
        if partner_email:
            folio_vals["email"] = partner_email

        folio_id = await self._transport.create(
            "pms.folio", folio_vals
        )

        # 2. Create reservations
        reservation_ids = []
        for res in reservations:
            res_vals = {
                "folio_id": folio_id,
                "room_type_id": res["room_type_id"],
                "checkin": res["checkin"],
                "checkout": res["checkout"],
                "adults": res.get("adults", 2),
                "children": res.get("children", 0),
            }
            rid = await self._transport.create(
                "pms.reservation", res_vals
            )
            reservation_ids.append(rid)

        # 3. Confirm folio
        await self._transport.call(
            "pms.folio",
            "action_confirm",
            args=[[folio_id]],
        )

        # 4. Read back folio details
        folio_data = await self._transport.read(
            "pms.folio",
            [folio_id],
            fields=[
                "name",
                "amount_total",
                "first_checkin",
                "last_checkout",
            ],
        )
        fd = folio_data[0] if folio_data else {}
        return {
            "folio_id": folio_id,
            "folio_name": fd.get("name"),
            "reservation_ids": reservation_ids,
            "amount_total": fd.get("amount_total"),
            "first_checkin": fd.get("first_checkin"),
            "last_checkout": fd.get("last_checkout"),
        }


def _build_reservation(d: dict) -> Reservation:
    return Reservation(
        id=d["id"],
        name=d.get("name", ""),
        folio_id=(
            _m2o_id(d, "folio_id") or d.get("folio_id", 0)
        ),
        state=d.get("state", ""),
        room_type_id=_m2o_id(d, "room_type_id"),
        room_type_name=_m2o_name(d, "room_type_id"),
        preferred_room_id=_m2o_id(d, "preferred_room_id"),
        preferred_room_name=_m2o_name(
            d, "preferred_room_id"
        ),
        rooms=d.get("rooms") or None,
        checkin=d.get("checkin") or None,
        checkout=d.get("checkout") or None,
        arrival_hour=d.get("arrival_hour") or None,
        departure_hour=d.get("departure_hour") or None,
        nights=d.get("nights", 0),
        partner_id=_m2o_id(d, "partner_id"),
        partner_name=d.get("partner_name") or None,
        adults=d.get("adults", 0),
        children=d.get("children", 0),
        partner_requests=d.get("partner_requests") or None,
        price_total=d.get("price_total", 0.0),
        price_subtotal=d.get("price_subtotal", 0.0),
        price_tax=d.get("price_tax", 0.0),
        price_services=d.get("price_services", 0.0),
        discount=d.get("discount", 0.0),
        board_service_room_id=_m2o_id(
            d, "board_service_room_id"
        ),
        board_service_room_name=_m2o_name(
            d, "board_service_room_id"
        ),
        overbooking=d.get("overbooking", False),
        cancelled_reason=d.get("cancelled_reason") or None,
        checkin_partner_count=d.get(
            "checkin_partner_count", 0
        ),
        checkin_partner_pending_count=d.get(
            "checkin_partner_pending_count", 0
        ),
        sale_channel_origin_id=_m2o_id(
            d, "sale_channel_origin_id"
        ),
        sale_channel_origin_name=_m2o_name(
            d, "sale_channel_origin_id"
        ),
        reservation_line_ids=d.get(
            "reservation_line_ids", []
        ),
        service_ids=d.get("service_ids", []),
        checkin_partner_ids=d.get(
            "checkin_partner_ids", []
        ),
    )
