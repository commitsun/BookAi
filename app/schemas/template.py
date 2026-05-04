"""
Pydantic schemas for the send-template endpoint (Flow 1: Roomdoo → BookAI → WhatsApp).
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceHotel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    odoo_id: int | None = Field(
        default=None,
        description="Property ID in Odoo — primary identifier. Pair (instance + odoo_id) is unique.",
        examples=[1],
    )
    external_code: str | None = Field(
        default=None,
        description="roomdoo_external_code of the property (legacy fallback)",
        examples=["HOTEL_BCN_01"],
    )
    name: str | None = Field(default=None, examples=["Hotel Barcelona Centro"])

    @model_validator(mode="before")
    @classmethod
    def _normalize_hotel(cls, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return data
        # Odoo sends "id" — map to "odoo_id"
        if "odoo_id" not in data and "id" in data:
            data["odoo_id"] = data["id"]
        # Treat empty external_code as None
        if not data.get("external_code"):
            data["external_code"] = None
        return data


class OriginFolio(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(
        ...,
        description=(
            "Folio code as known by the PMS (odoo_external_code). "
            "BookAI normalizes URL-unsafe characters (/, ?, #, %, &, =, space) "
            "to '_' on ingestion. The stored and returned code is always the "
            "normalized form (e.g. '206_26_026072')."
        ),
        examples=["206_26_026072"],
    )
    id: int | None = Field(default=None, description="Numeric folio ID in Odoo", examples=[1042])
    checkin: str | None = Field(default=None, description="ISO date YYYY-MM-DD", examples=["2026-04-10"])
    checkout: str | None = Field(default=None, description="ISO date YYYY-MM-DD", examples=["2026-04-14"])

    @model_validator(mode="before")
    @classmethod
    def _normalize_folio(cls, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            return data
        # Odoo sends min_checkin / max_checkout
        if "checkin" not in data and "min_checkin" in data:
            data["checkin"] = data["min_checkin"]
        if "checkout" not in data and "max_checkout" in data:
            data["checkout"] = data["max_checkout"]
        return data


class TemplateSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hotel: SourceHotel
    origin_folio: OriginFolio | None = None


class TemplateRecipient(BaseModel):
    phone: str = Field(
        ...,
        description="Destination phone (E.164 or local with country hint)",
        examples=["+34699323583"],
    )
    country: str | None = Field(
        default=None,
        description="ISO-3166-1 alpha-2 country hint for local numbers",
        examples=["ES"],
    )
    display_name: str | None = Field(default=None, examples=["María García"])

    @model_validator(mode="before")
    @classmethod
    def _strip_phone(cls, data: dict[str, Any]) -> dict[str, Any]:
        if isinstance(data, dict) and "phone" in data:
            data["phone"] = str(data["phone"]).strip()
        return data


class TemplatePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(
        ...,
        description="Internal template code stored in whatsapp_templates.code",
        examples=["welcome_checkin"],
    )
    language: str = Field(default="es", examples=["es"])
    components: list[dict] = Field(
        default_factory=list,
        description="Meta-format components array (body params, buttons, etc.)",
        examples=[[{"type": "body", "parameters": [{"type": "text", "text": "María"}]}]],
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="Named parameter values from Odoo (e.g. buyer_name → Darío)",
    )


class _Meta(BaseModel):
    """Optional metadata block sent by Odoo."""
    model_config = ConfigDict(extra="ignore")
    idempotency_key: str | None = None
    trigger: str | None = None


class SendTemplateRequest(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "source": {
                    "hotel": {"odoo_id": 1, "name": "Hotel Barcelona Centro"},
                    "origin_folio": {
                        "code": "F2500008",
                        "id": 1042,
                        "checkin": "2026-04-10",
                        "checkout": "2026-04-14",
                    },
                },
                "recipient": {
                    "phone": "+34699323583",
                    "country": "ES",
                    "display_name": "María García",
                },
                "template": {
                    "code": "welcome_checkin",
                    "language": "es",
                    "components": [
                        {"type": "body", "parameters": [{"type": "text", "text": "María"}]}
                    ],
                },
                "idempotency_key": "roomdoo-send-1042-welcome_checkin",
            }
        }
    )

    source: TemplateSource
    recipient: TemplateRecipient
    template: TemplatePayload
    meta: _Meta | None = None
    idempotency_key: str | None = Field(
        default=None,
        description="Unique key to guarantee exactly-once delivery. Roomdoo retries are safe.",
        examples=["roomdoo-send-1042-welcome_checkin"],
    )

    @model_validator(mode="after")
    def _resolve_idempotency_key(self) -> "SendTemplateRequest":
        """Accept idempotency_key from either root or meta block."""
        if self.idempotency_key is None and self.meta and self.meta.idempotency_key:
            self.idempotency_key = self.meta.idempotency_key
        return self


class SendTemplateResponse(BaseModel):
    status: str = Field(examples=["ok"])
    message_id: int = Field(examples=[1201])
    wa_message_id: str | None = Field(default=None, examples=["wamid.HBgLMzQ2OTkzMjM1ODM"])
    conversation_id: int = Field(examples=[42])
    idempotent: bool = Field(
        default=False,
        description="True when the request was already processed and the cached result is returned.",
    )
