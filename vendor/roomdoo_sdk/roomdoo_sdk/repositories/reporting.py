from __future__ import annotations

from collections import defaultdict

from ..models.guest import (
    ArrivalDeparture,
    OccupancyData,
    RevenueSummary,
)
from ..transports.base import Transport


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


class ReportingRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def occupancy(
        self,
        property_id: int,
        date_from: str,
        date_to: str,
    ) -> list[OccupancyData]:
        """Occupancy by room type for a date range."""
        # Get total rooms per type
        rooms = await self._transport.search_read(
            "pms.room",
            [
                ("pms_property_id", "=", property_id),
                ("active", "=", True),
            ],
            fields=["room_type_id"],
        )
        total_by_type: dict[int, int] = defaultdict(int)
        type_names: dict[int, str] = {}
        for r in rooms:
            rt = r.get("room_type_id")
            if rt and isinstance(rt, (list, tuple)):
                total_by_type[rt[0]] += 1
                type_names[rt[0]] = rt[1]

        # Get availability (occupied = total - real_avail)
        avail_records = await self._transport.search_read(
            "pms.availability",
            [
                ("pms_property_id", "=", property_id),
                ("date", ">=", date_from),
                ("date", "<", date_to),
            ],
            fields=["date", "room_type_id", "real_avail"],
        )
        results = []
        for r in avail_records:
            rt = r.get("room_type_id")
            if not rt:
                continue
            rt_id = rt[0] if isinstance(rt, (list, tuple)) else rt
            rt_name = type_names.get(rt_id, "")
            total = total_by_type.get(rt_id, 0)
            avail = r.get("real_avail", 0)
            occupied = max(0, total - avail)
            pct = (
                round(occupied / total * 100, 1)
                if total > 0
                else 0.0
            )
            results.append(
                OccupancyData(
                    date=r.get("date", ""),
                    room_type_id=rt_id,
                    room_type_name=rt_name,
                    total_rooms=total,
                    occupied=occupied,
                    occupancy_pct=pct,
                )
            )
        return results

    async def revenue_summary(
        self,
        property_id: int,
        date_from: str,
        date_to: str,
    ) -> RevenueSummary:
        """Revenue summary for a property and period."""
        folios = await self._transport.search_read(
            "pms.folio",
            [
                ("pms_property_id", "=", property_id),
                ("state", "not in", ["cancel", "draft"]),
                ("first_checkin", ">=", date_from),
                ("first_checkin", "<", date_to),
            ],
            fields=[
                "amount_total", "reservation_ids",
                "service_ids",
            ],
        )
        total = sum(f.get("amount_total", 0) for f in folios)
        # Approximate split: read reservation totals
        res_ids = []
        for f in folios:
            res_ids.extend(f.get("reservation_ids", []))
        rooms_rev = 0.0
        if res_ids:
            res_records = await self._transport.read(
                "pms.reservation",
                res_ids,
                fields=["price_total", "price_services"],
            )
            for r in res_records:
                rooms_rev += r.get("price_total", 0) - r.get(
                    "price_services", 0
                )
        return RevenueSummary(
            total_revenue=total,
            total_rooms_revenue=rooms_rev,
            total_services_revenue=total - rooms_rev,
            total_folios=len(folios),
            period_start=date_from,
            period_end=date_to,
        )

    async def arrivals_departures(
        self, property_id: int, date: str
    ) -> dict[str, list[ArrivalDeparture]]:
        """Arrivals and departures for a specific date."""
        arrivals = await self._transport.search_read(
            "pms.reservation",
            [
                ("pms_property_id", "=", property_id),
                ("checkin", "=", date),
                ("state", "not in", ["cancel"]),
            ],
            fields=[
                "id", "name", "partner_name",
                "room_type_id", "preferred_room_id",
                "checkin", "checkout", "state",
                "adults", "children",
            ],
        )
        departures = await self._transport.search_read(
            "pms.reservation",
            [
                ("pms_property_id", "=", property_id),
                ("checkout", "=", date),
                ("state", "not in", ["cancel"]),
            ],
            fields=[
                "id", "name", "partner_name",
                "room_type_id", "preferred_room_id",
                "checkin", "checkout", "state",
                "adults", "children",
            ],
        )
        return {
            "arrivals": [_build_ad(r) for r in arrivals],
            "departures": [_build_ad(r) for r in departures],
        }

    async def pending_checkins(
        self, property_id: int, limit: int = 50
    ) -> list[ArrivalDeparture]:
        """Reservations pending check-in."""
        records = await self._transport.search_read(
            "pms.reservation",
            [
                ("pms_property_id", "=", property_id),
                ("state", "in", [
                    "confirm", "arrival_delayed"
                ]),
            ],
            fields=[
                "id", "name", "partner_name",
                "room_type_id", "preferred_room_id",
                "checkin", "checkout", "state",
                "adults", "children",
            ],
            limit=limit,
            order="checkin asc",
        )
        return [_build_ad(r) for r in records]


def _build_ad(d: dict) -> ArrivalDeparture:
    return ArrivalDeparture(
        id=d["id"],
        name=d.get("name", ""),
        partner_name=d.get("partner_name") or None,
        room_type_name=_m2o_name(d, "room_type_id"),
        room_name=_m2o_name(d, "preferred_room_id"),
        checkin=d.get("checkin") or None,
        checkout=d.get("checkout") or None,
        state=d.get("state") or None,
        adults=d.get("adults", 0),
        children=d.get("children", 0),
    )
