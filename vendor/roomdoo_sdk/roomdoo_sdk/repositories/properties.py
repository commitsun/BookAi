from __future__ import annotations

from ..exceptions import NotFoundError
from ..models.pricelist import CancelationPolicy, Pricelist
from ..models.property import Property
from ..models.room import Room, RoomType
from ..transports.base import Transport

_PROPERTY_FIELDS = [
    "id",
    "name",
    "pms_property_code",
    "external_code",
    "bookai_mode",
    "tz",
    "email",
    "phone",
    "street",
    "city",
    "country_id",
    "company_id",
    # BooKAI selling
    "bookai_online_selling",
    "bookai_sale_channel_id",
    # WhatsApp channel (v4: M2O → bookai.wa.phone)
    "bookai_wa_phone_id",
    # App URL
    "bookai_app_url",
    # Escalation
    "bookai_escalation_timeout",
    "bookai_escalation_template_id",
    "bookai_escalation_user_ids",
    # PMS base
    "default_arrival_hour",
    "default_departure_hour",
    "mail_information",
    "privacy_policy",
    # pms_notifications
    "arrival_instructions",
    "welcome_message",
    "parking_info",
    "checkin_time_info",
    "checkout_time_info",
    "digital_checkin_help",
    "prearrival_extra_info",
    "critical_contact_phone",
]


class PropertyRepository:
    def __init__(self, transport: Transport):
        self._transport = transport

    async def list(self) -> list[Property]:
        """All active properties in this Odoo instance."""
        records = await self._transport.search_read(
            "pms.property",
            [],
            fields=_PROPERTY_FIELDS,
        )
        wa_data = await _resolve_wa_data(
            self._transport, records
        )
        return [
            _build_property(r, wa_data=wa_data.get(r["id"]))
            for r in records
        ]

    async def get(self, property_id: int) -> Property:
        """A single property by ID, with escalation contacts."""
        records = await self._transport.read(
            "pms.property",
            [property_id],
            fields=_PROPERTY_FIELDS,
        )
        if not records:
            raise NotFoundError(
                f"Property {property_id} not found"
            )
        d = records[0]
        # Resolve WA phone + account data
        wa_data = await _resolve_wa_data(
            self._transport, [d]
        )
        # Resolve escalation contacts
        contacts = []
        user_ids = d.get("bookai_escalation_user_ids", [])
        if user_ids:
            users = await self._transport.read(
                "res.users",
                user_ids,
                fields=["id", "name", "mobile", "phone"],
            )
            contacts = [
                {
                    "user_id": u["id"],
                    "name": u.get("name", ""),
                    "phone": u.get("mobile")
                    or u.get("phone")
                    or "",
                }
                for u in users
                if u.get("mobile") or u.get("phone")
            ]
        prop = _build_property(
            d, contacts, wa_data=wa_data.get(property_id)
        )
        # Resolve template code
        tmpl_ref = d.get("bookai_escalation_template_id")
        if tmpl_ref:
            tmpl_id = (
                tmpl_ref[0]
                if isinstance(tmpl_ref, (list, tuple))
                else tmpl_ref
            )
            tmpl = await self._transport.read(
                "pms.notification.template",
                [tmpl_id],
                fields=["bookai_template_code"],
            )
            if tmpl:
                prop.bookai_escalation_template_code = (
                    tmpl[0].get("bookai_template_code")
                    or None
                )
        # Resolve app_url fallback to global param
        if not prop.bookai_app_url:
            params = await self._transport.call(
                "ir.config_parameter",
                "get_param",
                args=["roomdoo_app_url"],
            )
            if params:
                prop.bookai_app_url = params or None
        return prop

    async def get_room_types(
        self, property_id: int
    ) -> list[RoomType]:
        """Room types for a property with guest descriptions."""
        records = await self._transport.search_read(
            "pms.room.type",
            [("room_ids.pms_property_id", "=", property_id)],
            fields=[
                "id", "name", "default_code",
                "class_id", "overnight_room",
                "guest_display_name", "guest_short_description",
                "guest_long_description",
                "bed_configuration_text", "view_description",
                "amenities_summary",
            ],
        )
        return [_build_room_type(r) for r in records]

    async def get_rooms(
        self, property_id: int
    ) -> list[Room]:
        """All active rooms for a property."""
        records = await self._transport.search_read(
            "pms.room",
            [
                ("pms_property_id", "=", property_id),
                ("active", "=", True),
            ],
            fields=[
                "id", "name", "room_type_id", "capacity",
                "extra_beds_allowed", "ubication_id",
                "description_sale",
                "guest_visible_name", "location_hint",
                "building_label",
            ],
        )
        return [_build_room(r) for r in records]

    async def get_pricelists(
        self, property_id: int
    ) -> list[Pricelist]:
        """Pricelists linked to BooKAI channel for a property.

        In PMS, empty pms_property_ids means available for
        all properties.
        """
        records = await self._transport.search_read(
            "product.pricelist",
            [
                "|",
                ("pms_property_ids", "in", [property_id]),
                ("pms_property_ids", "=", False),
                ("pms_sale_channel_ids.name", "=", "BooKAI"),
            ],
            fields=[
                "id", "name", "cancelation_rule_id",
                "guest_rate_name", "guest_rate_description",
                "payment_terms_text",
                "cancellation_terms_text_override",
            ],
        )
        # Batch-read cancelation rules
        rule_ids = list(
            {
                r["cancelation_rule_id"][0]
                for r in records
                if r.get("cancelation_rule_id")
            }
        )
        rules: dict[int, CancelationPolicy] = {}
        if rule_ids:
            rule_records = await self._transport.read(
                "pms.cancelation.rule",
                rule_ids,
                fields=[
                    "id", "name", "days_intime",
                    "penalty_late", "apply_on_late",
                    "penalty_noshow", "apply_on_noshow",
                    "guest_policy_name", "short_policy_text",
                    "full_policy_text", "no_show_policy_text",
                    "refund_timing_text",
                ],
            )
            rules = {
                r["id"]: _build_cancelation(r)
                for r in rule_records
            }
        return [_build_pricelist(r, rules) for r in records]

    async def get_cancelation_policy(
        self, policy_id: int
    ) -> CancelationPolicy:
        """Get cancelation policy details by ID."""
        records = await self._transport.read(
            "pms.cancelation.rule",
            [policy_id],
            fields=[
                "id", "name", "days_intime",
                "penalty_late", "apply_on_late",
                "penalty_noshow", "apply_on_noshow",
                "guest_policy_name", "short_policy_text",
                "full_policy_text", "no_show_policy_text",
                "refund_timing_text",
            ],
        )
        if not records:
            raise NotFoundError(
                f"Cancelation policy {policy_id} not found"
            )
        return _build_cancelation(records[0])

    async def get_amenities(
        self, property_id: int
    ) -> list[dict]:
        """Amenities for a property (empty = all)."""
        return await self._transport.search_read(
            "pms.amenity",
            [
                "|",
                ("pms_property_ids", "in", [property_id]),
                ("pms_property_ids", "=", False),
            ],
            fields=[
                "id", "name", "pms_amenity_type_id",
                "default_code",
            ],
        )

    async def get_board_services(
        self, property_id: int
    ) -> list[dict]:
        """Board services for a property (empty = all)."""
        return await self._transport.search_read(
            "pms.board.service",
            [
                "|",
                ("pms_property_ids", "in", [property_id]),
                ("pms_property_ids", "=", False),
            ],
            fields=["id", "name", "default_code", "amount"],
        )


async def _resolve_wa_data(
    transport: Transport,
    records: list[dict],
) -> dict[int, dict]:
    """Batch-resolve WA phone + account data for property records.

    Reads ``bookai.wa.phone`` and ``bookai.wa.account`` linked via the
    ``bookai_wa_phone_id`` M2O (pms_bookai ≥ 4.0).

    Returns ``{property_id: {bookai_wa_phone_number_id, …}}``.
    """
    phone_to_props: dict[int, list[int]] = {}
    for r in records:
        ref = r.get("bookai_wa_phone_id")
        if isinstance(ref, (list, tuple)) and ref:
            phone_to_props.setdefault(ref[0], []).append(
                r["id"]
            )

    if not phone_to_props:
        return {}

    phones = await transport.read(
        "bookai.wa.phone",
        list(phone_to_props),
        fields=[
            "id",
            "phone_number_id",
            "display_number",
            "wa_account_id",
        ],
    )

    account_ids = list(
        {
            p["wa_account_id"][0]
            for p in phones
            if isinstance(
                p.get("wa_account_id"), (list, tuple)
            )
        }
    )
    accounts: dict[int, dict] = {}
    if account_ids:
        recs = await transport.read(
            "bookai.wa.account",
            account_ids,
            fields=[
                "id",
                "waba_id",
                "access_token",
                "verify_token",
            ],
        )
        accounts = {a["id"]: a for a in recs}

    result: dict[int, dict] = {}
    for phone in phones:
        acc_ref = phone.get("wa_account_id")
        acc_id = (
            acc_ref[0]
            if isinstance(acc_ref, (list, tuple))
            else None
        )
        acc = accounts.get(acc_id, {}) if acc_id else {}

        wa = {
            "bookai_wa_phone_number_id": (
                phone.get("phone_number_id") or None
            ),
            "bookai_wa_display_number": (
                phone.get("display_number") or None
            ),
            "bookai_wa_account_id": (
                acc.get("waba_id") or None
            ),
            "bookai_wa_access_token": (
                acc.get("access_token") or None
            ),
            "bookai_wa_verify_token": (
                acc.get("verify_token") or None
            ),
        }
        for prop_id in phone_to_props.get(
            phone["id"], []
        ):
            result[prop_id] = wa

    return result


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


def _build_room_type(d: dict) -> RoomType:
    return RoomType(
        id=d["id"],
        name=d.get("name", ""),
        default_code=d.get("default_code") or None,
        class_id=_m2o_id(d, "class_id"),
        class_name=_m2o_name(d, "class_id"),
        overnight=d.get("overnight_room", True),
        guest_display_name=(
            d.get("guest_display_name") or None
        ),
        guest_short_description=(
            d.get("guest_short_description") or None
        ),
        guest_long_description=(
            d.get("guest_long_description") or None
        ),
        bed_configuration_text=(
            d.get("bed_configuration_text") or None
        ),
        view_description=d.get("view_description") or None,
        amenities_summary=d.get("amenities_summary") or None,
    )


def _build_room(d: dict) -> Room:
    return Room(
        id=d["id"],
        name=d.get("name", ""),
        room_type_id=_m2o_id(d, "room_type_id"),
        room_type_name=_m2o_name(d, "room_type_id"),
        capacity=d.get("capacity", 0),
        extra_beds_allowed=d.get("extra_beds_allowed", 0),
        ubication_name=_m2o_name(d, "ubication_id"),
        description_sale=d.get("description_sale") or None,
        guest_visible_name=(
            d.get("guest_visible_name") or None
        ),
        location_hint=d.get("location_hint") or None,
        building_label=d.get("building_label") or None,
    )


def _build_cancelation(d: dict) -> CancelationPolicy:
    return CancelationPolicy(
        id=d["id"],
        name=d.get("name", ""),
        days_intime=d.get("days_intime", 0),
        penalty_late=d.get("penalty_late", 0),
        apply_on_late=d.get("apply_on_late") or None,
        penalty_noshow=d.get("penalty_noshow", 0),
        apply_on_noshow=d.get("apply_on_noshow") or None,
        guest_policy_name=(
            d.get("guest_policy_name") or None
        ),
        short_policy_text=(
            d.get("short_policy_text") or None
        ),
        full_policy_text=(
            d.get("full_policy_text") or None
        ),
        no_show_policy_text=(
            d.get("no_show_policy_text") or None
        ),
        refund_timing_text=(
            d.get("refund_timing_text") or None
        ),
    )


def _build_pricelist(
    d: dict, rules: dict[int, CancelationPolicy]
) -> Pricelist:
    rule_ref = d.get("cancelation_rule_id")
    rule_id = (
        rule_ref[0] if isinstance(rule_ref, (list, tuple)) else None
    )
    return Pricelist(
        id=d["id"],
        name=d.get("name", ""),
        guest_rate_name=d.get("guest_rate_name") or None,
        guest_rate_description=(
            d.get("guest_rate_description") or None
        ),
        payment_terms_text=(
            d.get("payment_terms_text") or None
        ),
        cancellation_terms_text_override=(
            d.get("cancellation_terms_text_override") or None
        ),
        cancelation_policy=rules.get(rule_id) if rule_id else None,
    )


def _build_property(
    d: dict,
    contacts: list[dict] | None = None,
    wa_data: dict | None = None,
) -> Property:
    wa = wa_data or {}
    return Property(
        id=d["id"],
        name=d.get("name", ""),
        pms_property_code=d.get("pms_property_code") or None,
        external_code=d.get("external_code") or None,
        bookai_mode=d.get("bookai_mode", "disabled"),
        tz=d.get("tz", "UTC"),
        email=d.get("email") or None,
        phone=d.get("phone") or None,
        street=d.get("street") or None,
        city=d.get("city") or None,
        country_id=_m2o_id(d, "country_id"),
        country_name=_m2o_name(d, "country_id"),
        company_id=_m2o_id(d, "company_id"),
        company_name=_m2o_name(d, "company_id"),
        bookai_online_selling=d.get(
            "bookai_online_selling", False
        ),
        bookai_sale_channel_id=_m2o_id(
            d, "bookai_sale_channel_id"
        ),
        bookai_wa_phone_number_id=(
            wa.get("bookai_wa_phone_number_id")
        ),
        bookai_wa_access_token=(
            wa.get("bookai_wa_access_token")
        ),
        bookai_wa_account_id=(
            wa.get("bookai_wa_account_id")
        ),
        bookai_wa_verify_token=(
            wa.get("bookai_wa_verify_token")
        ),
        bookai_wa_display_number=(
            wa.get("bookai_wa_display_number")
        ),
        bookai_app_url=d.get("bookai_app_url") or None,
        bookai_escalation_timeout=d.get(
            "bookai_escalation_timeout", 30
        ),
        bookai_escalation_template_code=None,  # resolved in get()
        bookai_escalation_contacts=contacts or [],
        default_arrival_hour=d.get("default_arrival_hour") or None,
        default_departure_hour=(
            d.get("default_departure_hour") or None
        ),
        mail_information=d.get("mail_information") or None,
        privacy_policy=d.get("privacy_policy") or None,
        arrival_instructions=(
            d.get("arrival_instructions") or None
        ),
        welcome_message=d.get("welcome_message") or None,
        parking_info=d.get("parking_info") or None,
        checkin_time_info=d.get("checkin_time_info") or None,
        checkout_time_info=d.get("checkout_time_info") or None,
        digital_checkin_help=(
            d.get("digital_checkin_help") or None
        ),
        prearrival_extra_info=(
            d.get("prearrival_extra_info") or None
        ),
        critical_contact_phone=(
            d.get("critical_contact_phone") or None
        ),
    )
