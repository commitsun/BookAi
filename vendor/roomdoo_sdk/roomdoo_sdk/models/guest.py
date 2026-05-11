from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Guest:
    id: int
    name: str
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    street: str | None = None
    city: str | None = None
    country_id: int | None = None
    country_name: str | None = None
    vat: str | None = None
    lang: str | None = None
    comment: str | None = None
    is_agency: bool = False
    reservations_count: int = 0
    folios_count: int = 0


@dataclass
class Invoice:
    id: int
    name: str
    state: str
    move_type: str | None = None
    partner_id: int | None = None
    partner_name: str | None = None
    invoice_date: str | None = None
    amount_total: float = 0.0
    amount_residual: float = 0.0
    payment_state: str | None = None
    pms_property_id: int | None = None
    pms_property_name: str | None = None


@dataclass
class PaymentRecord:
    id: int
    name: str | None = None
    amount: float = 0.0
    date: str | None = None
    state: str | None = None
    payment_type: str | None = None
    journal_name: str | None = None
    partner_name: str | None = None
    folio_name: str | None = None


@dataclass
class OccupancyData:
    date: str
    room_type_id: int
    room_type_name: str
    total_rooms: int = 0
    occupied: int = 0
    occupancy_pct: float = 0.0


@dataclass
class RevenueSummary:
    total_revenue: float = 0.0
    total_rooms_revenue: float = 0.0
    total_services_revenue: float = 0.0
    total_folios: int = 0
    period_start: str | None = None
    period_end: str | None = None


@dataclass
class ArrivalDeparture:
    id: int
    name: str
    partner_name: str | None = None
    room_type_name: str | None = None
    room_name: str | None = None
    checkin: str | None = None
    checkout: str | None = None
    state: str | None = None
    adults: int = 0
    children: int = 0
