"""
Pydantic schemas for the chatter endpoint (Flow 3: app user → BookAI → WhatsApp).
"""

from pydantic import BaseModel, ConfigDict, Field


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "conversation_id": 42,
                "content": "Buenos días María, le confirmamos que su habitación estará lista a las 15:00.",
                "channel_endpoint_id": None,
                "agent_user_id": 7,
                "agent_display_name": "Carlos Recepción",
            }
        }
    )

    conversation_id: int = Field(..., description="ID of the target Conversation", examples=[42])
    content: str = Field(..., min_length=1, description="Text to send to the guest")
    channel_endpoint_id: int | None = Field(
        default=None,
        description=(
            "Channel to use for sending. "
            "Defaults to the most recently active channel for the conversation."
        ),
        examples=[None],
    )
    agent_user_id: int | None = Field(
        default=None,
        description="Roomdoo user ID of the sender",
        examples=[7],
    )
    agent_display_name: str | None = Field(
        default=None,
        description="Display name shown alongside the message in the chat",
        examples=["Carlos Recepción"],
    )


class SendMessageResponse(BaseModel):
    status: str = Field(examples=["ok"])
    message_id: int = Field(examples=[1205])
    wa_message_id: str | None = Field(default=None, examples=["wamid.HBgLMzQ2OTkzMjM1ODM"])
    conversation_id: int = Field(examples=[42])
