from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Folio:
    id: int
    name: str
    pms_property_id: int
    pms_property_name: str
    state: str
    reservation_type: str | None = None
    # Partner
    partner_id: int | None = None
    partner_name: str | None = None
    email: str | None = None
    mobile: str | None = None
    # Dates
    date_order: str | None = None
    first_checkin: str | None = None
    last_checkout: str | None = None
    # Amounts
    amount_total: float = 0.0
    amount_untaxed: float = 0.0
    amount_tax: float = 0.0
    pending_amount: float = 0.0
    # Pricelist
    pricelist_id: int | None = None
    pricelist_name: str | None = None
    # Status
    payment_state: str | None = None
    invoice_status: str | None = None
    # Counts
    number_of_rooms: int = 0
    number_of_services: int = 0
    # Channel
    sale_channel_origin_id: int | None = None
    sale_channel_origin_name: str | None = None
    agency_id: int | None = None
    agency_name: str | None = None
    # Notes
    internal_comment: str | None = None
    cancelled_reason: str | None = None
    # Linked IDs (for further queries)
    reservation_ids: list[int] = field(default_factory=list)


@dataclass
class Reservation:
    id: int
    name: str
    folio_id: int
    state: str
    # Room
    room_type_id: int | None = None
    room_type_name: str | None = None
    preferred_room_id: int | None = None
    preferred_room_name: str | None = None
    rooms: str | None = None
    # Dates
    checkin: str | None = None
    checkout: str | None = None
    arrival_hour: str | None = None
    departure_hour: str | None = None
    nights: int = 0
    # Guests
    partner_id: int | None = None
    partner_name: str | None = None
    adults: int = 0
    children: int = 0
    partner_requests: str | None = None
    # Pricing
    price_total: float = 0.0
    price_subtotal: float = 0.0
    price_tax: float = 0.0
    price_services: float = 0.0
    discount: float = 0.0
    # Board service
    board_service_room_id: int | None = None
    board_service_room_name: str | None = None
    # Status
    overbooking: bool = False
    cancelled_reason: str | None = None
    # Checkin data
    checkin_partner_count: int = 0
    checkin_partner_pending_count: int = 0
    # Channel
    sale_channel_origin_id: int | None = None
    sale_channel_origin_name: str | None = None
    # Linked IDs
    reservation_line_ids: list[int] = field(default_factory=list)
    service_ids: list[int] = field(default_factory=list)
    checkin_partner_ids: list[int] = field(default_factory=list)


@dataclass
class ReservationLine:
    id: int
    reservation_id: int
    date: str
    room_id: int | None = None
    room_name: str | None = None
    price: float = 0.0
    discount: float = 0.0
    cancel_discount: float = 0.0
    price_day_total: float = 0.0


@dataclass
class CheckinPartner:
    id: int
    reservation_id: int
    name: str | None = None
    firstname: str | None = None
    lastname: str | None = None
    lastname2: str | None = None
    document_number: str | None = None
    document_type: str | None = None
    email: str | None = None
    mobile: str | None = None
    birthdate_date: str | None = None
    gender: str | None = None
    nationality_id: int | None = None
    nationality_name: str | None = None
    state: str | None = None


@dataclass
class FolioPayment:
    id: int
    amount: float = 0.0
    date: str | None = None
    state: str | None = None
    payment_type: str | None = None
    journal_name: str | None = None
    ref: str | None = None


@dataclass
class FolioService:
    id: int
    name: str
    reservation_id: int | None = None
    product_id: int | None = None
    product_name: str | None = None
    is_board_service: bool = False
    price_total: float = 0.0
    price_subtotal: float = 0.0
    price_tax: float = 0.0
    discount: float = 0.0
