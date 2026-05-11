from __future__ import annotations

from ..models.guest import PaymentRecord
from ..models.pricelist import FolioSummary
from ..transports.base import Transport

_PAYMENT_FIELDS = [
    "id", "name", "amount", "date", "state",
    "payment_type", "journal_id", "partner_id",
]


def _m2o_name(data, field):
    val = data.get(field)
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return val[1]
    return None


class PaymentRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def record(
        self,
        folio_id: int,
        amount: float,
        journal_id: int,
    ) -> int:
        """Record a payment for a folio. Returns payment ID."""
        return await self._transport.call(
            "pms.folio",
            "action_pay",
            args=[[folio_id]],
            kwargs={
                "amount": amount,
                "journal_id": journal_id,
            },
        )

    async def list_by_property(
        self,
        property_id: int,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 50,
    ) -> list[PaymentRecord]:
        """Payments for a property in a period."""
        domain: list = [
            ("pms_property_id", "=", property_id),
        ]
        if date_from:
            domain.append(("date", ">=", date_from))
        if date_to:
            domain.append(("date", "<=", date_to))
        records = await self._transport.search_read(
            "account.payment",
            domain,
            fields=_PAYMENT_FIELDS,
            limit=limit,
            order="date desc",
        )
        return [_build_payment(r) for r in records]

    async def get_pending_by_property(
        self, property_id: int, limit: int = 50
    ) -> list[FolioSummary]:
        """Folios with pending balance for a property."""
        records = await self._transport.search_read(
            "pms.folio",
            [
                ("pms_property_id", "=", property_id),
                ("pending_amount", ">", 0),
                ("state", "not in", ["cancel"]),
            ],
            fields=[
                "id", "name", "state", "partner_name",
                "first_checkin", "last_checkout",
                "amount_total", "pending_amount",
            ],
            limit=limit,
            order="pending_amount desc",
        )
        return [
            FolioSummary(
                id=r["id"],
                name=r.get("name", ""),
                state=r.get("state", ""),
                partner_name=r.get("partner_name") or None,
                first_checkin=(
                    r.get("first_checkin") or None
                ),
                last_checkout=(
                    r.get("last_checkout") or None
                ),
                amount_total=r.get("amount_total", 0.0),
                pending_amount=r.get("pending_amount", 0.0),
            )
            for r in records
        ]


def _build_payment(d: dict) -> PaymentRecord:
    return PaymentRecord(
        id=d["id"],
        name=d.get("name") or None,
        amount=d.get("amount", 0.0),
        date=d.get("date") or None,
        state=d.get("state") or None,
        payment_type=d.get("payment_type") or None,
        journal_name=_m2o_name(d, "journal_id"),
        partner_name=_m2o_name(d, "partner_id"),
    )
