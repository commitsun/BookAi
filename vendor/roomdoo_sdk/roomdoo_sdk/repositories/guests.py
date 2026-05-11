from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.guest import Guest
from ..models.pricelist import FolioSummary
from ..transports.base import Transport

_GUEST_FIELDS = [
    "id", "name", "email", "phone", "mobile",
    "street", "city", "country_id", "vat", "lang",
    "comment", "is_agency",
    "reservations_count", "folios_count",
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


class GuestRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def search(
        self,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        document_number: str | None = None,
        limit: int = 20,
    ) -> list[Guest]:
        """Search guests/partners."""
        domain: list = [("is_agency", "=", False)]
        if name:
            domain.append(("name", "ilike", name))
        if email:
            domain.append(("email", "ilike", email))
        if phone:
            domain.append(
                "|",
            )
            domain.append(("phone", "ilike", phone))
            domain.append(("mobile", "ilike", phone))
        if document_number:
            domain.append(
                (
                    "pms_checkin_partner_ids.document_number",
                    "ilike",
                    document_number,
                )
            )
        records = await self._transport.search_read(
            "res.partner",
            domain,
            fields=_GUEST_FIELDS,
            limit=limit,
            order="name",
        )
        return [_build_guest(r) for r in records]

    async def get(self, partner_id: int) -> Guest:
        """Get guest details."""
        records = await self._transport.read(
            "res.partner",
            [partner_id],
            fields=_GUEST_FIELDS,
        )
        if not records:
            raise NotFoundError(
                f"Guest {partner_id} not found"
            )
        return _build_guest(records[0])

    async def get_history(
        self, partner_id: int, limit: int = 10
    ) -> list[FolioSummary]:
        """Get folio history for a guest."""
        records = await self._transport.search_read(
            "pms.folio",
            [("partner_id", "=", partner_id)],
            fields=[
                "id", "name", "state", "partner_name",
                "first_checkin", "last_checkout",
                "amount_total", "pending_amount",
            ],
            limit=limit,
            order="first_checkin desc",
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

    async def update_contact(
        self,
        partner_id: int,
        email: str | None = None,
        phone: str | None = None,
        mobile: str | None = None,
    ) -> None:
        """Update guest contact info."""
        vals = {}
        if email is not None:
            vals["email"] = email
        if phone is not None:
            vals["phone"] = phone
        if mobile is not None:
            vals["mobile"] = mobile
        if vals:
            await self._transport.write(
                "res.partner", [partner_id], vals
            )


def _build_guest(d: dict) -> Guest:
    return Guest(
        id=d["id"],
        name=d.get("name", ""),
        email=d.get("email") or None,
        phone=d.get("phone") or None,
        mobile=d.get("mobile") or None,
        street=d.get("street") or None,
        city=d.get("city") or None,
        country_id=_m2o_id(d, "country_id"),
        country_name=_m2o_name(d, "country_id"),
        vat=d.get("vat") or None,
        lang=d.get("lang") or None,
        comment=d.get("comment") or None,
        is_agency=d.get("is_agency", False),
        reservations_count=d.get("reservations_count", 0),
        folios_count=d.get("folios_count", 0),
    )
