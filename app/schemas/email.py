"""
Pydantic schemas for the email channel API.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EmailRecipient(BaseModel):
    email: str  # validated by email_send_service (lowercased, non-empty)
    name: str | None = None


class EmailHotelSource(BaseModel):
    external_code: str = Field(..., description="Property roomdoo_external_code")


class EmailFolioSource(BaseModel):
    code: str
    id: int | None = None
    checkin: str | None = None
    checkout: str | None = None


class EmailSource(BaseModel):
    hotel: EmailHotelSource
    origin_folio: EmailFolioSource | None = None


class EmailSendRequest(BaseModel):
    """
    Unified request body for POST /api/v1/email/send.

    Two caller modes:

    **Odoo/Roomdoo** (resolves property via hotel external_code):
      - ``source.hotel.external_code`` required
      - ``recipient.email`` required

    **App** (sends within an existing conversation):
      - ``conversation_id`` required
      - ``channel_endpoint_id`` optional (defaults to last-used endpoint)
      - ``agent_user_id`` / ``agent_display_name`` identify the operator

    Both modes require ``subject`` and at least one of ``text_body`` / ``html_body``.
    """

    # Odoo caller fields
    source: EmailSource | None = None
    recipient: EmailRecipient | None = None

    # App caller fields
    conversation_id: int | None = None
    channel_endpoint_id: int | None = None
    agent_user_id: int | None = None
    agent_display_name: str | None = None

    # Common
    subject: str = Field(..., min_length=1)
    text_body: str | None = None
    html_body: str | None = None
    idempotency_key: str | None = None


class EmailSendResponse(BaseModel):
    status: str
    message_id: int
    conversation_id: int
    provider_message_id: str | None = None
    idempotent: bool = False


# ---------------------------------------------------------------------------
# Mailgun inbound webhook payload
# ---------------------------------------------------------------------------


class MailgunInboundPayload(BaseModel):
    """
    Fields from a Mailgun inbound parsed-mail webhook.
    All fields are optional because Mailgun may omit them depending on content.
    """

    sender: str = Field(default="", alias="sender")
    recipient: str = Field(default="", alias="recipient")
    subject: str = Field(default="", alias="subject")
    body_plain: str | None = Field(default=None, alias="body-plain")
    body_html: str | None = Field(default=None, alias="body-html")
    message_id: str | None = Field(default=None, alias="Message-Id")
    in_reply_to: str | None = Field(default=None, alias="In-Reply-To")
    references: str | None = Field(default=None, alias="References")
    timestamp: str = Field(default="")
    token: str = Field(default="")
    signature: str = Field(default="")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Mailgun delivery event webhook payload
# ---------------------------------------------------------------------------


class MailgunSignature(BaseModel):
    timestamp: str
    token: str
    signature: str


class MailgunMessageHeaders(BaseModel):
    message_id: str | None = Field(default=None, alias="message-id")

    model_config = {"populate_by_name": True}


class MailgunMessageInfo(BaseModel):
    headers: MailgunMessageHeaders = Field(default_factory=MailgunMessageHeaders)


class MailgunEventData(BaseModel):
    event: str = ""
    message: MailgunMessageInfo = Field(default_factory=MailgunMessageInfo)
    recipient: str = ""
    error: str | None = None


class MailgunDeliveryEventPayload(BaseModel):
    signature: MailgunSignature
    event_data: MailgunEventData = Field(alias="event-data")

    model_config = {"populate_by_name": True}
