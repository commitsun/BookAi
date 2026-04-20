from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ChannelEndpoint(Base):
    """
    A business-side messaging endpoint — currently a WhatsApp Business phone number,
    but designed to be extended to other channels (Telegram, SMS, email, …).

    This entity is intentionally decoupled from Property because a single channel endpoint
    can be shared by multiple hotels in a small chain. Which hotel is handling a given
    conversation is determined by the AttentionSession, not by this entity.

    Credentials (access_token) live here because they belong to the endpoint, not to
    any individual hotel.
    """

    __tablename__ = "channel_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Channel type — extensible for future channels (e.g. "telegram", "email")
    channel: Mapped[str] = mapped_column(String(50), nullable=False, default="whatsapp")

    # Meta: the phone_number_id from Meta Business Platform
    external_code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Channel provider credentials for this endpoint
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Token used to verify the webhook URL during Meta registration (unique per endpoint)
    verify_token: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    # When True, skip real provider API calls and return fake IDs — for local dev/testing
    mock_mode: Mapped[bool] = mapped_column(default=False, nullable=False)

    # Human-readable identifier for display (e.g. "+34 900 000 000" for WhatsApp,
    # "Alda Hotels" sender name for email)
    display_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Provider-specific credentials and parameters (e.g. Mailgun domain + api_key for email).
    # Empty dict for WhatsApp (credentials live in access_token / account_id).
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(), onupdate=func.now(), nullable=False
    )

    properties: Mapped[list["Property"]] = relationship(back_populates="channel_endpoint")
