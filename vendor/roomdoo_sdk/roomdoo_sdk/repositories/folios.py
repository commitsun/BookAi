from __future__ import annotations

from ..exceptions import NotFoundError, ValidationError
from ..models.folio import (
    CheckinPartner,
    Folio,
    FolioPayment,
    FolioService,
    Reservation,
    ReservationLine,
)
from ..models.pricelist import FolioSummary
from ..transports.base import Transport

# ---------------------------------------------------------------------------
# Field lists — only request what each dataclass needs
# ---------------------------------------------------------------------------

_FOLIO_FIELDS = [
    "id", "name", "pms_property_id", "state", "reservation_type",
    "partner_id", "partner_name", "email", "mobile",
    "date_order", "first_checkin", "last_checkout",
    "amount_total", "amount_untaxed", "amount_tax", "pending_amount",
    "pricelist_id",
    "payment_state", "invoice_status",
    "number_of_rooms", "number_of_services",
    "sale_channel_origin_id", "agency_id",
    "internal_comment", "cancelled_reason",
    "reservation_ids",
]

_RESERVATION_FIELDS = [
    "id", "name", "folio_id", "state",
    "room_type_id", "preferred_room_id", "rooms",
    "checkin", "checkout", "arrival_hour", "departure_hour", "nights",
    "partner_id", "partner_name", "adults", "children", "partner_requests",
    "price_total", "price_subtotal", "price_tax", "price_services", "discount",
    "board_service_room_id",
    "overbooking", "cancelled_reason",
    "checkin_partner_count", "checkin_partner_pending_count",
    "sale_channel_origin_id",
    "reservation_line_ids", "service_ids", "checkin_partner_ids",
]

_LINE_FIELDS = [
    "id", "reservation_id", "date", "room_id",
    "price", "discount", "cancel_discount", "price_day_total",
]

_CHECKIN_PARTNER_FIELDS = [
    "id", "reservation_id", "name", "firstname", "lastname", "lastname2",
    "document_number", "document_type",
    "email", "mobile", "birthdate_date", "gender", "nationality_id",
    "state",
]

_PAYMENT_FIELDS = [
    "id", "amount", "date", "state", "payment_type", "journal_id", "ref",
]

_SERVICE_FIELDS = [
    "id", "name", "reservation_id", "product_id", "is_board_service",
    "price_total", "price_subtotal", "price_tax", "discount",
]


# ---------------------------------------------------------------------------
# Helpers to extract Many2one (id, name) tuples from Odoo dicts
# ---------------------------------------------------------------------------

def _m2o_id(data: dict, field: str) -> int | None:
    val = data.get(field)
    if isinstance(val, (list, tuple)) and val:
        return val[0]
    return None


def _m2o_name(data: dict, field: str) -> str | None:
    val = data.get(field)
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return val[1]
    return None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class FolioRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def get_folio(self, folio_id: int) -> Folio:
        """Basic folio data: partner, dates, amounts, state."""
        records = await self._transport.read(
            "pms.folio", [folio_id], fields=_FOLIO_FIELDS
        )
        if not records:
            raise NotFoundError(f"Folio {folio_id} not found")
        return _build_folio(records[0])

    async def get_reservations(self, folio_id: int) -> list[Reservation]:
        """All reservations for a folio: rooms, guests, prices, dates."""
        records = await self._transport.search_read(
            "pms.reservation",
            [("folio_id", "=", folio_id)],
            fields=_RESERVATION_FIELDS,
        )
        return [_build_reservation(r) for r in records]

    async def get_reservation_lines(
        self, reservation_id: int
    ) -> list[ReservationLine]:
        """Daily price breakdown for a reservation."""
        records = await self._transport.search_read(
            "pms.reservation.line",
            [("reservation_id", "=", reservation_id)],
            fields=_LINE_FIELDS,
            order="date asc",
        )
        return [_build_line(r) for r in records]

    async def get_checkin_partners(
        self, folio_id: int
    ) -> list[CheckinPartner]:
        """Guest checkin data for all reservations in a folio."""
        records = await self._transport.search_read(
            "pms.checkin.partner",
            [("folio_id", "=", folio_id)],
            fields=_CHECKIN_PARTNER_FIELDS,
        )
        return [_build_checkin_partner(r) for r in records]

    async def get_payments(self, folio_id: int) -> list[FolioPayment]:
        """Payments linked to a folio."""
        # Read payment_ids from folio
        folios = await self._transport.read(
            "pms.folio", [folio_id], fields=["payment_ids"]
        )
        if not folios or not folios[0].get("payment_ids"):
            return []
        payment_ids = folios[0]["payment_ids"]
        records = await self._transport.read(
            "account.payment", payment_ids, fields=_PAYMENT_FIELDS
        )
        return [_build_payment(r) for r in records]

    async def get_services(self, folio_id: int) -> list[FolioService]:
        """Services linked to a folio."""
        records = await self._transport.search_read(
            "pms.service",
            [("folio_id", "=", folio_id)],
            fields=_SERVICE_FIELDS,
        )
        return [_build_service(r) for r in records]

    async def search_by_guest(
        self,
        email: str | None = None,
        phone: str | None = None,
        name: str | None = None,
        property_id: int | None = None,
        limit: int = 10,
    ) -> list[FolioSummary]:
        """Search folios by guest email, phone or name."""
        domain: list = []
        if email:
            domain.append(("email", "ilike", email))
        if phone:
            domain.append(("mobile", "ilike", phone))
        if name:
            domain.append(("partner_name", "ilike", name))
        if property_id:
            domain.append(
                ("pms_property_id", "=", property_id)
            )
        if not domain:
            return []
        records = await self._transport.search_read(
            "pms.folio",
            domain,
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

    # -----------------------------------------------------------------------
    # Write methods
    # -----------------------------------------------------------------------

    async def update_arrival_hour(
        self,
        folio_id: int,
        arrival_hour: str,
        reservation_ids: list[int] | None = None,
    ) -> None:
        """Update arrival hour for reservations in a folio.

        Args:
            folio_id: The folio ID.
            arrival_hour: Time in HH:MM format (e.g. "14:30").
            reservation_ids: Specific reservation IDs to update.
                If None, updates all reservations in the folio.
        """
        if not _is_valid_time(arrival_hour):
            raise ValidationError(
                f"Invalid arrival_hour '{arrival_hour}'. Expected HH:MM format."
            )
        if reservation_ids is None:
            folios = await self._transport.read(
                "pms.folio", [folio_id], fields=["reservation_ids"]
            )
            if not folios:
                raise NotFoundError(f"Folio {folio_id} not found")
            reservation_ids = folios[0].get("reservation_ids", [])
        if not reservation_ids:
            return
        await self._transport.write(
            "pms.reservation",
            reservation_ids,
            {"arrival_hour": arrival_hour},
        )

    # -----------------------------------------------------------------------
    # my_* methods (phone-validated, for external guests)
    # -----------------------------------------------------------------------

    async def _validate_phone_folio(
        self, phone: str, folio_id: int
    ) -> dict:
        """Validate that phone owns the folio.

        Matches last 9 digits of phone against folio.mobile.
        Returns folio data or raises NotFoundError.
        """
        suffix = phone[-9:] if len(phone) >= 9 else phone
        folios = await self._transport.read(
            "pms.folio",
            [folio_id],
            fields=["id", "mobile", "partner_id"],
        )
        if not folios:
            raise NotFoundError(
                f"Folio {folio_id} not found"
            )
        folio = folios[0]
        folio_mobile = folio.get("mobile") or ""
        if suffix in folio_mobile:
            return folio
        # Fallback: check partner mobile
        partner = folio.get("partner_id")
        if partner:
            pid = (
                partner[0]
                if isinstance(partner, (list, tuple))
                else partner
            )
            partners = await self._transport.read(
                "res.partner",
                [pid],
                fields=["mobile", "phone"],
            )
            if partners:
                p = partners[0]
                p_mobile = p.get("mobile") or ""
                p_phone = p.get("phone") or ""
                if suffix in p_mobile or suffix in p_phone:
                    return folio
        raise NotFoundError(
            "Phone does not match this booking"
        )

    async def _validate_phone_reservation(
        self, phone: str, reservation_id: int
    ) -> dict:
        """Validate phone owns the reservation's folio."""
        res = await self._transport.read(
            "pms.reservation",
            [reservation_id],
            fields=["folio_id"],
        )
        if not res:
            raise NotFoundError(
                f"Reservation {reservation_id} not found"
            )
        folio_ref = res[0].get("folio_id")
        fid = (
            folio_ref[0]
            if isinstance(folio_ref, (list, tuple))
            else folio_ref
        )
        return await self._validate_phone_folio(phone, fid)

    async def my_folios(
        self, phone: str, limit: int = 10
    ) -> list[FolioSummary]:
        """List folios belonging to this phone."""
        suffix = phone[-9:] if len(phone) >= 9 else phone
        records = await self._transport.search_read(
            "pms.folio",
            [
                "|",
                ("mobile", "ilike", f"%{suffix}%"),
                (
                    "partner_id.mobile",
                    "ilike",
                    f"%{suffix}%",
                ),
            ],
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
                pending_amount=r.get(
                    "pending_amount", 0.0
                ),
            )
            for r in records
        ]

    async def my_folio(
        self, phone: str, folio_id: int
    ) -> Folio:
        """Get folio detail, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        return await self.get_folio(folio_id)

    async def my_reservations(
        self, phone: str, folio_id: int
    ) -> list[Reservation]:
        """Get reservations, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        return await self.get_reservations(folio_id)

    async def my_reservation_lines(
        self, phone: str, reservation_id: int
    ) -> list[ReservationLine]:
        """Get reservation lines, validated by phone."""
        await self._validate_phone_reservation(
            phone, reservation_id
        )
        return await self.get_reservation_lines(
            reservation_id
        )

    async def my_services(
        self, phone: str, folio_id: int
    ) -> list[FolioService]:
        """Get services, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        return await self.get_services(folio_id)

    async def my_payments(
        self, phone: str, folio_id: int
    ) -> list[FolioPayment]:
        """Get payments, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        return await self.get_payments(folio_id)

    async def my_checkin_partners(
        self, phone: str, folio_id: int
    ) -> list[CheckinPartner]:
        """Get checkin partners, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        return await self.get_checkin_partners(folio_id)

    async def my_update_arrival(
        self, phone: str, folio_id: int, arrival_hour: str
    ) -> None:
        """Update arrival hour, validated by phone."""
        await self._validate_phone_folio(phone, folio_id)
        await self.update_arrival_hour(folio_id, arrival_hour)


def _is_valid_time(value: str) -> bool:
    if not value or len(value) != 5 or value[2] != ":":
        return False
    try:
        h, m = int(value[:2]), int(value[3:])
        return 0 <= h <= 23 and 0 <= m <= 59
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _build_folio(d: dict) -> Folio:
    return Folio(
        id=d["id"],
        name=d.get("name", ""),
        pms_property_id=_m2o_id(d, "pms_property_id") or 0,
        pms_property_name=_m2o_name(d, "pms_property_id") or "",
        state=d.get("state", ""),
        reservation_type=d.get("reservation_type") or None,
        partner_id=_m2o_id(d, "partner_id"),
        partner_name=d.get("partner_name") or None,
        email=d.get("email") or None,
        mobile=d.get("mobile") or None,
        date_order=d.get("date_order") or None,
        first_checkin=d.get("first_checkin") or None,
        last_checkout=d.get("last_checkout") or None,
        amount_total=d.get("amount_total", 0.0),
        amount_untaxed=d.get("amount_untaxed", 0.0),
        amount_tax=d.get("amount_tax", 0.0),
        pending_amount=d.get("pending_amount", 0.0),
        pricelist_id=_m2o_id(d, "pricelist_id"),
        pricelist_name=_m2o_name(d, "pricelist_id"),
        payment_state=d.get("payment_state") or None,
        invoice_status=d.get("invoice_status") or None,
        number_of_rooms=d.get("number_of_rooms", 0),
        number_of_services=d.get("number_of_services", 0),
        sale_channel_origin_id=_m2o_id(d, "sale_channel_origin_id"),
        sale_channel_origin_name=_m2o_name(d, "sale_channel_origin_id"),
        agency_id=_m2o_id(d, "agency_id"),
        agency_name=_m2o_name(d, "agency_id"),
        internal_comment=d.get("internal_comment") or None,
        cancelled_reason=d.get("cancelled_reason") or None,
        reservation_ids=d.get("reservation_ids", []),
    )


def _build_reservation(d: dict) -> Reservation:
    return Reservation(
        id=d["id"],
        name=d.get("name", ""),
        folio_id=_m2o_id(d, "folio_id") or d.get("folio_id", 0),
        state=d.get("state", ""),
        room_type_id=_m2o_id(d, "room_type_id"),
        room_type_name=_m2o_name(d, "room_type_id"),
        preferred_room_id=_m2o_id(d, "preferred_room_id"),
        preferred_room_name=_m2o_name(d, "preferred_room_id"),
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
        board_service_room_id=_m2o_id(d, "board_service_room_id"),
        board_service_room_name=_m2o_name(d, "board_service_room_id"),
        overbooking=d.get("overbooking", False),
        cancelled_reason=d.get("cancelled_reason") or None,
        checkin_partner_count=d.get("checkin_partner_count", 0),
        checkin_partner_pending_count=d.get("checkin_partner_pending_count", 0),
        sale_channel_origin_id=_m2o_id(d, "sale_channel_origin_id"),
        sale_channel_origin_name=_m2o_name(d, "sale_channel_origin_id"),
        reservation_line_ids=d.get("reservation_line_ids", []),
        service_ids=d.get("service_ids", []),
        checkin_partner_ids=d.get("checkin_partner_ids", []),
    )


def _build_line(d: dict) -> ReservationLine:
    return ReservationLine(
        id=d["id"],
        reservation_id=_m2o_id(d, "reservation_id") or d.get("reservation_id", 0),
        date=d.get("date", ""),
        room_id=_m2o_id(d, "room_id"),
        room_name=_m2o_name(d, "room_id"),
        price=d.get("price", 0.0),
        discount=d.get("discount", 0.0),
        cancel_discount=d.get("cancel_discount", 0.0),
        price_day_total=d.get("price_day_total", 0.0),
    )


def _build_checkin_partner(d: dict) -> CheckinPartner:
    return CheckinPartner(
        id=d["id"],
        reservation_id=_m2o_id(d, "reservation_id") or d.get("reservation_id", 0),
        name=d.get("name") or None,
        firstname=d.get("firstname") or None,
        lastname=d.get("lastname") or None,
        lastname2=d.get("lastname2") or None,
        document_number=d.get("document_number") or None,
        document_type=d.get("document_type") or None,
        email=d.get("email") or None,
        mobile=d.get("mobile") or None,
        birthdate_date=d.get("birthdate_date") or None,
        gender=d.get("gender") or None,
        nationality_id=_m2o_id(d, "nationality_id"),
        nationality_name=_m2o_name(d, "nationality_id"),
        state=d.get("state") or None,
    )


def _build_payment(d: dict) -> FolioPayment:
    return FolioPayment(
        id=d["id"],
        amount=d.get("amount", 0.0),
        date=d.get("date") or None,
        state=d.get("state") or None,
        payment_type=d.get("payment_type") or None,
        journal_name=_m2o_name(d, "journal_id"),
        ref=d.get("ref") or None,
    )


def _build_service(d: dict) -> FolioService:
    return FolioService(
        id=d["id"],
        name=d.get("name", ""),
        reservation_id=_m2o_id(d, "reservation_id"),
        product_id=_m2o_id(d, "product_id"),
        product_name=_m2o_name(d, "product_id"),
        is_board_service=d.get("is_board_service", False),
        price_total=d.get("price_total", 0.0),
        price_subtotal=d.get("price_subtotal", 0.0),
        price_tax=d.get("price_tax", 0.0),
        discount=d.get("discount", 0.0),
    )
