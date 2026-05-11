from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Property:
    id: int
    name: str
    bookai_mode: str
    tz: str
    pms_property_code: str | None = None
    external_code: str | None = None
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    country_id: int | None = None
    country_name: str | None = None
    company_id: int | None = None
    company_name: str | None = None
    # BooKAI selling
    bookai_online_selling: bool = False
    bookai_sale_channel_id: int | None = None
    # WhatsApp channel
    bookai_wa_phone_number_id: str | None = None
    bookai_wa_access_token: str | None = None
    bookai_wa_account_id: str | None = None
    bookai_wa_verify_token: str | None = None
    bookai_wa_display_number: str | None = None
    # App URL
    bookai_app_url: str | None = None
    # Escalation
    bookai_escalation_timeout: int = 30
    bookai_escalation_template_code: str | None = None
    bookai_escalation_contacts: list[dict] = field(
        default_factory=list
    )
    # PMS base
    default_arrival_hour: str | None = None
    default_departure_hour: str | None = None
    mail_information: str | None = None
    privacy_policy: str | None = None
    # pms_notifications
    arrival_instructions: str | None = None
    welcome_message: str | None = None
    parking_info: str | None = None
    checkin_time_info: str | None = None
    checkout_time_info: str | None = None
    digital_checkin_help: str | None = None
    prearrival_extra_info: str | None = None
    critical_contact_phone: str | None = None
