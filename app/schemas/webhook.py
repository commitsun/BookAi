"""
Pydantic models for the Meta WhatsApp Cloud API webhook payload.

Only fields relevant to Phase 1 (text messages + delivery status updates) are modelled.
Everything else is left as optional so unexpected payload shapes don't break parsing.
"""

from pydantic import BaseModel, Field


class WebhookMetadata(BaseModel):
    display_phone_number: str | None = None
    phone_number_id: str | None = None


class WebhookTextContent(BaseModel):
    body: str = ""


class WebhookInteractiveButtonReply(BaseModel):
    id: str | None = None
    title: str = ""


class WebhookInteractiveListReply(BaseModel):
    id: str | None = None
    title: str = ""


class WebhookInteractive(BaseModel):
    type: str | None = None
    button_reply: WebhookInteractiveButtonReply | None = None
    list_reply: WebhookInteractiveListReply | None = None


class WebhookMessage(BaseModel):
    id: str
    from_: str = Field(alias="from")  # sender phone in E.164 digits (no +)
    timestamp: str | None = None
    type: str = "text"
    text: WebhookTextContent | None = None
    interactive: WebhookInteractive | None = None
    # Audio / image / other types are not processed in Phase 1 — stored as placeholder

    model_config = {"populate_by_name": True}


class WebhookContact(BaseModel):
    wa_id: str | None = None
    profile: dict | None = None

    @property
    def display_name(self) -> str | None:
        if self.profile:
            return self.profile.get("name")
        return None


class WebhookStatus(BaseModel):
    id: str  # wa_message_id
    status: str  # delivered | read | sent | failed
    timestamp: str | None = None
    recipient_id: str | None = None
    errors: list[dict] | None = None


class WebhookValue(BaseModel):
    messaging_product: str | None = None
    metadata: WebhookMetadata | None = None
    contacts: list[WebhookContact] | None = None
    messages: list[WebhookMessage] | None = None
    statuses: list[WebhookStatus] | None = None


class WebhookChange(BaseModel):
    value: WebhookValue | None = None
    field: str | None = None


class WebhookEntry(BaseModel):
    id: str | None = None
    changes: list[WebhookChange] = []


class MetaWebhookPayload(BaseModel):
    object: str | None = None
    entry: list[WebhookEntry] = []
