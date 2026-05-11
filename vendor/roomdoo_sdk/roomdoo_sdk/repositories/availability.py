from __future__ import annotations

from ..models.pricelist import (
    AvailabilityResult,
    NightPrice,
    PriceBreakdown,
)
from ..transports.base import Transport


class AvailabilityRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def check(
        self,
        property_id: int,
        checkin: str,
        checkout: str,
        pricelist_id: int,
        room_type_id: int | None = None,
    ) -> list[AvailabilityResult]:
        """Check sellable availability for a date range.

        Uses pms.property computed field free_room_ids with
        context (checkin, checkout, pricelist_id) which
        applies availability plan restrictions, quotas, etc.
        """
        # Get all overnight room types for this property
        room_types = await self._transport.search_read(
            "pms.room.type",
            [("room_ids.pms_property_id", "=", property_id)],
            fields=["id", "name", "overnight_room"],
        )
        overnight_types = [
            rt for rt in room_types
            if rt.get("overnight_room", True)
        ]
        if room_type_id:
            overnight_types = [
                rt for rt in overnight_types
                if rt["id"] == room_type_id
            ]

        results = []
        for rt in overnight_types:
            ctx = {
                "checkin": checkin,
                "checkout": checkout,
                "room_type_id": rt["id"],
            }
            if pricelist_id:
                ctx["pricelist_id"] = pricelist_id
            else:
                ctx["real_avail"] = True

            # Read computed field with context
            data = await self._transport.call(
                "pms.property",
                "read",
                args=[[property_id], ["availability"]],
                kwargs={"context": ctx},
            )
            avail = 0
            if data and isinstance(data, list):
                avail = data[0].get("availability", 0)

            results.append(
                AvailabilityResult(
                    room_type_id=rt["id"],
                    room_type_name=rt.get("name", ""),
                    available_rooms=avail,
                )
            )

        return results

    async def check_real(
        self,
        property_id: int,
        checkin: str,
        checkout: str,
        room_type_id: int | None = None,
    ) -> list[AvailabilityResult]:
        """Check real availability (ignoring pricelist
        restrictions). Useful for staff tools."""
        room_types = await self._transport.search_read(
            "pms.room.type",
            [("room_ids.pms_property_id", "=", property_id)],
            fields=["id", "name", "overnight_room"],
        )
        overnight_types = [
            rt for rt in room_types
            if rt.get("overnight_room", True)
        ]
        if room_type_id:
            overnight_types = [
                rt for rt in overnight_types
                if rt["id"] == room_type_id
            ]
        results = []
        for rt in overnight_types:
            ctx = {
                "checkin": checkin,
                "checkout": checkout,
                "room_type_id": rt["id"],
                "real_avail": True,
            }
            data = await self._transport.call(
                "pms.property",
                "read",
                args=[[property_id], ["availability"]],
                kwargs={"context": ctx},
            )
            avail = 0
            if data and isinstance(data, list):
                avail = data[0].get("availability", 0)
            results.append(
                AvailabilityResult(
                    room_type_id=rt["id"],
                    room_type_name=rt.get("name", ""),
                    available_rooms=avail,
                )
            )
        return results

    async def get_prices(
        self,
        property_id: int,
        checkin: str,
        checkout: str,
        room_type_id: int,
        pricelist_id: int,
    ) -> PriceBreakdown:
        """Get nightly prices for a room type and pricelist.

        Calls pms.property.get_bookai_prices which replicates
        the pms_price_service logic with tax adjustment.
        """
        # Get names for the response
        rt_records = await self._transport.read(
            "pms.room.type",
            [room_type_id],
            fields=["name"],
        )
        rt_name = (
            rt_records[0].get("name", "")
            if rt_records
            else ""
        )
        pl_records = await self._transport.read(
            "product.pricelist",
            [pricelist_id],
            fields=["name"],
        )
        pl_name = (
            pl_records[0].get("name", "")
            if pl_records
            else ""
        )

        # Call Odoo helper that handles pricing correctly
        data = await self._transport.call(
            "pms.property",
            "get_bookai_prices",
            args=[
                property_id,
                pricelist_id,
                room_type_id,
                checkin,
                checkout,
            ],
        )

        nights = []
        total = 0.0
        for entry in data or []:
            p = entry.get("price", 0.0)
            nights.append(
                NightPrice(
                    date=entry.get("date", ""),
                    price=p,
                )
            )
            total += p

        return PriceBreakdown(
            room_type_id=room_type_id,
            room_type_name=rt_name,
            pricelist_id=pricelist_id,
            pricelist_name=pl_name,
            nights=nights,
            total=round(total, 2),
        )

    async def get_all_prices(
        self,
        property_id: int,
        checkin: str,
        checkout: str,
        room_type_id: int,
    ) -> list[PriceBreakdown]:
        """Prices for ALL BooKAI pricelists for a room type.

        Returns one PriceBreakdown per pricelist, each with
        cancelation_rule_id so the agent can look up the
        policy if needed.
        """
        rt_records = await self._transport.read(
            "pms.room.type",
            [room_type_id],
            fields=["name"],
        )
        rt_name = (
            rt_records[0].get("name", "")
            if rt_records
            else ""
        )

        data = await self._transport.call(
            "pms.property",
            "get_bookai_all_prices",
            args=[
                property_id,
                room_type_id,
                checkin,
                checkout,
            ],
        )

        results = []
        for entry in data or []:
            nights = [
                NightPrice(
                    date=n.get("date", ""),
                    price=n.get("price", 0.0),
                )
                for n in entry.get("nights", [])
            ]
            cancel_id = entry.get("cancelation_rule_id")
            results.append(
                PriceBreakdown(
                    room_type_id=room_type_id,
                    room_type_name=rt_name,
                    pricelist_id=entry.get(
                        "pricelist_id", 0
                    ),
                    pricelist_name=entry.get(
                        "pricelist_name", ""
                    ),
                    nights=nights,
                    total=entry.get("total", 0.0),
                    guest_rate_name=entry.get(
                        "guest_rate_name"
                    )
                    or None,
                    cancelation_rule_id=(
                        cancel_id if cancel_id else None
                    ),
                    cancelation_policy_name=entry.get(
                        "cancelation_policy_name"
                    )
                    or None,
                )
            )
        return results
