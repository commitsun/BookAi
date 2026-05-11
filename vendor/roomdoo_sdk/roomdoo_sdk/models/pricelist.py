from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CancelationPolicy:
    id: int
    name: str
    days_intime: int = 0
    penalty_late: int = 0
    apply_on_late: str | None = None
    penalty_noshow: int = 0
    apply_on_noshow: str | None = None
    # pms_notifications
    guest_policy_name: str | None = None
    short_policy_text: str | None = None
    full_policy_text: str | None = None
    no_show_policy_text: str | None = None
    refund_timing_text: str | None = None


@dataclass
class Pricelist:
    id: int
    name: str
    # pms_notifications
    guest_rate_name: str | None = None
    guest_rate_description: str | None = None
    payment_terms_text: str | None = None
    cancellation_terms_text_override: str | None = None
    cancelation_policy: CancelationPolicy | None = None


@dataclass
class NightPrice:
    date: str
    price: float


@dataclass
class PriceBreakdown:
    room_type_id: int
    room_type_name: str
    pricelist_id: int
    pricelist_name: str
    nights: list[NightPrice]
    total: float = 0.0
    guest_rate_name: str | None = None
    cancelation_rule_id: int | None = None
    cancelation_policy_name: str | None = None


@dataclass
class AvailabilityResult:
    room_type_id: int
    room_type_name: str
    available_rooms: int
    restricted: bool = False
    restriction_reason: str | None = None


@dataclass
class FolioSummary:
    id: int
    name: str
    state: str
    partner_name: str | None = None
    first_checkin: str | None = None
    last_checkout: str | None = None
    amount_total: float = 0.0
    pending_amount: float = 0.0
