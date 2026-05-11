from __future__ import annotations

import logging
from datetime import date, timedelta

from ..transports.base import Transport

_logger = logging.getLogger(__name__)

# Day-of-week mapping (Python weekday → name)
_DOW = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}

_RULE_FIELDS = [
    "id",
    "availability_plan_id",
    "room_type_id",
    "date",
    "pms_property_id",
    "min_stay",
    "min_stay_arrival",
    "max_stay",
    "max_stay_arrival",
    "closed",
    "closed_arrival",
    "closed_departure",
    "quota",
    "max_avail",
    "plan_avail",
    "real_avail",
]

_ITEM_FIELDS = [
    "id",
    "pricelist_id",
    "product_tmpl_id",
    "date_start_consumption",
    "date_end_consumption",
    "fixed_price",
    "compute_price",
    "min_quantity",
    "pms_property_ids",
]


def _date_range(date_from: str, date_to: str):
    """Yield dates from date_from to date_to (exclusive)."""
    d = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while d <= end:
        yield d
        d += timedelta(days=1)


class RevenueRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_availability_rules(
        self,
        property_id: int,
        date_from: str,
        date_to: str,
        room_type_id: int | None = None,
        availability_plan_id: int | None = None,
    ) -> list[dict]:
        """Get availability plan rules for a date range.

        Returns list of rules with min/max stay, closed
        flags, quotas, and real availability.
        """
        domain: list = [
            ("pms_property_id", "=", property_id),
            ("date", ">=", date_from),
            ("date", "<=", date_to),
        ]
        if room_type_id:
            domain.append(("room_type_id", "=", room_type_id))
        if availability_plan_id:
            domain.append(
                (
                    "availability_plan_id",
                    "=",
                    availability_plan_id,
                )
            )
        return await self._transport.search_read(
            "pms.availability.plan.rule",
            domain,
            fields=_RULE_FIELDS,
            order="date asc",
        )

    async def get_pricelist_items(
        self,
        pricelist_id: int,
        date_from: str,
        date_to: str,
        room_type_id: int | None = None,
    ) -> list[dict]:
        """Get pricelist items (daily rates) for a date range."""
        domain: list = [
            ("pricelist_id", "=", pricelist_id),
            ("date_start_consumption", "<=", date_to),
            ("date_end_consumption", ">=", date_from),
        ]
        if room_type_id:
            # Room type product template
            domain.append(
                ("product_tmpl_id.pms_room_type_ids", "=", room_type_id)
            )
        return await self._transport.search_read(
            "product.pricelist.item",
            domain,
            fields=_ITEM_FIELDS,
            order="date_start_consumption asc",
        )

    # ------------------------------------------------------------------
    # Write operations — Prices
    # ------------------------------------------------------------------

    async def set_prices(
        self,
        pricelist_id: int,
        room_type_id: int,
        date_from: str,
        date_to: str,
        price: float,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Set price for a room type in a pricelist for a date range.

        days_of_week: optional filter, e.g. ["mon", "fri", "sat"].
        If None, applies to all days.

        Creates or updates pricelist items. Returns count of
        items created/updated.
        """
        # Get room type product
        rt_data = await self._transport.search_read(
            "pms.room.type",
            [("id", "=", room_type_id)],
            fields=["product_id"],
            limit=1,
        )
        if not rt_data:
            raise ValueError(f"Room type {room_type_id} not found")
        product_id = (
            rt_data[0]["product_id"][0]
            if isinstance(rt_data[0]["product_id"], (list, tuple))
            else rt_data[0]["product_id"]
        )

        count = 0
        for d in _date_range(date_from, date_to):
            if days_of_week:
                dow = _DOW.get(d.weekday())
                if dow not in days_of_week:
                    continue

            d_str = d.isoformat()
            # Search existing item
            existing = await self._transport.search_read(
                "product.pricelist.item",
                [
                    ("pricelist_id", "=", pricelist_id),
                    ("product_id", "=", product_id),
                    ("date_start_consumption", "=", d_str),
                    ("date_end_consumption", "=", d_str),
                ],
                fields=["id"],
                limit=1,
            )
            if existing:
                await self._transport.write(
                    "product.pricelist.item",
                    [existing[0]["id"]],
                    {"fixed_price": price},
                )
            else:
                await self._transport.create(
                    "product.pricelist.item",
                    {
                        "pricelist_id": pricelist_id,
                        "applied_on": "0_product_variant",
                        "product_id": product_id,
                        "date_start_consumption": d_str,
                        "date_end_consumption": d_str,
                        "compute_price": "fixed",
                        "fixed_price": price,
                    },
                )
            count += 1

        _logger.info(
            "Set price %.2f for %d days (pricelist=%d, rt=%d)",
            price,
            count,
            pricelist_id,
            room_type_id,
        )
        return count

    # ------------------------------------------------------------------
    # Write operations — Availability rules
    # ------------------------------------------------------------------

    async def set_availability_rule(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        vals: dict,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Create or update availability plan rules for a date range.

        vals: dict with any of: min_stay, max_stay, min_stay_arrival,
        max_stay_arrival, closed, closed_arrival, closed_departure,
        quota, max_avail.

        Returns count of rules created/updated.
        """
        count = 0
        for d in _date_range(date_from, date_to):
            if days_of_week:
                dow = _DOW.get(d.weekday())
                if dow not in days_of_week:
                    continue

            d_str = d.isoformat()
            existing = await self._transport.search_read(
                "pms.availability.plan.rule",
                [
                    ("availability_plan_id", "=", availability_plan_id),
                    ("room_type_id", "=", room_type_id),
                    ("pms_property_id", "=", property_id),
                    ("date", "=", d_str),
                ],
                fields=["id"],
                limit=1,
            )
            if existing:
                await self._transport.write(
                    "pms.availability.plan.rule",
                    [existing[0]["id"]],
                    vals,
                )
            else:
                create_vals = {
                    "availability_plan_id": availability_plan_id,
                    "room_type_id": room_type_id,
                    "pms_property_id": property_id,
                    "date": d_str,
                    **vals,
                }
                await self._transport.create(
                    "pms.availability.plan.rule",
                    create_vals,
                )
            count += 1

        _logger.info(
            "Set availability rule for %d days "
            "(plan=%d, rt=%d, prop=%d, vals=%s)",
            count,
            availability_plan_id,
            room_type_id,
            property_id,
            vals,
        )
        return count

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    async def close_sales(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Stop-sell: close all sales for dates."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"closed": True},
            days_of_week,
        )

    async def open_sales(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Reopen previously closed sales."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"closed": False},
            days_of_week,
        )

    async def close_arrivals(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Close arrivals only for dates."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"closed_arrival": True},
            days_of_week,
        )

    async def close_departures(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Close departures only for dates."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"closed_departure": True},
            days_of_week,
        )

    async def set_min_stay(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        min_stay: int,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Set minimum stay requirement."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"min_stay": min_stay},
            days_of_week,
        )

    async def set_max_stay(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        max_stay: int,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Set maximum stay limit."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"max_stay": max_stay},
            days_of_week,
        )

    async def set_quota(
        self,
        availability_plan_id: int,
        room_type_id: int,
        property_id: int,
        date_from: str,
        date_to: str,
        quota: int,
        days_of_week: list[str] | None = None,
    ) -> int:
        """Set sales quota (-1 for unlimited)."""
        return await self.set_availability_rule(
            availability_plan_id,
            room_type_id,
            property_id,
            date_from,
            date_to,
            {"quota": quota},
            days_of_week,
        )
