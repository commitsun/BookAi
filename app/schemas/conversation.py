"""
Pydantic schemas for conversation-level endpoints.
"""

from typing import Literal

from pydantic import BaseModel, Field


class EmailMetadataOut(BaseModel):
    subject: str
    from_address: str
    from_name: str | None
    has_attachments: bool = False


class ContactSummary(BaseModel):
    id: int
    phone_code: str
    display_name: str | None


class LastMessageSummary(BaseModel):
    id: int
    direction: Literal["inbound", "outbound"]
    sender: Literal["guest", "agent", "system", "ai"]
    content: str | None
    created_at: str


class ConversationListItem(BaseModel):
    id: int
    created_at: str
    updated_at: str | None
    contact: ContactSummary
    last_message: LastMessageSummary | None
    unread_count: int = Field(
        default=0,
        description=(
            "Inbound messages received after this property's last "
            "PATCH /conversations/{id}/read call. 0 means fully read."
        ),
    )
    needs_attention: bool = Field(
        default=False,
        description=(
            "True when there is at least one unread transfer note for this "
            "property (a conversation was transferred here and not yet opened). "
            "Cleared by the same PATCH /conversations/{id}/read call. "
            "When True, show a 'needs attention' indicator instead of (or in "
            "addition to) the numeric unread badge."
        ),
    )


class ConversationsListResponse(BaseModel):
    property_id: int
    conversations: list[ConversationListItem]


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    channel_endpoint_id: int | None
    # Channel that carried this message (e.g. "whatsapp", "email")
    channel: str | None = None
    kind: Literal["message", "note"] = "message"
    direction: Literal["inbound", "outbound"]
    sender: Literal["guest", "agent", "system", "ai"]
    content: str | None = Field(
        description=(
            "Text in the requested language (original or translated)."
        )
    )
    content_language: str | None = Field(
        description="BCP-47 tag of the language that `content` is in."
    )
    is_translated: bool = Field(
        description=(
            "True when `content` is a cached translation; "
            "False when it is the original."
        )
    )
    agent_user_id: int | None
    agent_display_name: str | None
    wa_message_id: str | None
    wa_message_type: str
    delivery_status: Literal[
        "pending", "sent", "delivered", "read",
        "failed", "skipped", "accepted", "bounced",
    ]
    routing_status: Literal["routed", "unassigned", "ambiguous"] | None
    template_code: str | None
    created_at: str
    # Only populated when channel == "email"
    email_metadata: EmailMetadataOut | None = None


class MessagesResponse(BaseModel):
    conversation_id: int
    language: str | None = Field(
        description=(
            "The language requested by the client "
            "(null = original languages)."
        )
    )
    messages: list[MessageOut]


class TransferTargetProperty(BaseModel):
    id: int
    name: str
    roomdoo_external_code: str


class TransferTargetsResponse(BaseModel):
    conversation_id: int
    properties: list[TransferTargetProperty]


class TransferConversationRequest(BaseModel):
    destination_property_id: int = Field(
        ..., description="Property to transfer this conversation to"
    )
    note: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Summary note written by the agent explaining the transfer",
    )


class TransferConversationResponse(BaseModel):
    conversation_id: int
    from_session_id: int | None = Field(
        description="Source session closed (null if there was no active session)"
    )
    to_session_id: int
    destination_property_id: int


class AssignConversationRequest(BaseModel):
    property_id: int = Field(..., description="Property to assign this conversation to")


class AssignConversationResponse(BaseModel):
    conversation_id: int
    property_id: int
    attention_session_id: int
    created: bool = Field(description="True if a new session was created, False if one already existed")
