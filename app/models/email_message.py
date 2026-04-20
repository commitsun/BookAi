"""SQLAlchemy models for email-channel messages."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class EmailMessageMetadata(Base):
    """
    Email-specific metadata for messages sent or received via an email
    ChannelEndpoint. Relation: 1:1 with messages.

    Threading fields (RFC 2822):
    - provider_message_id: the Message-ID header stored for reply threading.
    - in_reply_to: In-Reply-To header from an incoming email.
    - references: full References header chain (space-separated IDs).

    For outbound emails BookAI generates and stores provider_message_id so
    subsequent inbound replies can be threaded to the correct conversation.
    """

    __tablename__ = "email_message_metadata"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # RFC 2822 threading headers
    provider_message_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )
    in_reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Full References header — space-separated Message-IDs
    references: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Envelope
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_address: Mapped[str] = mapped_column(Text, nullable=False)
    from_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{"email": "x@example.com", "name": "X"}]
    to_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    cc_addresses: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )
    reply_to: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Content
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Provider-specific identifiers
    mailgun_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        default=func.now(), nullable=False
    )

    message: Mapped["Message"] = relationship(  # type: ignore[name-defined]
        "Message",
        foreign_keys=[message_id],
        back_populates="email_metadata",
    )
    attachments: Mapped[list["EmailAttachment"]] = relationship(
        back_populates="email_metadata", cascade="all, delete-orphan"
    )


class EmailAttachment(Base):
    """
    Attachment reference for an email message.

    Phase 1: storage_key holds a temporary Mailgun URL (TTL ~3 days).
    Phase 2: storage_key will be a permanent object-storage key (S3/R2).
    """

    __tablename__ = "email_attachments"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    email_metadata_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("email_message_metadata.id", ondelete="CASCADE"),
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        Text, nullable=False, default="application/octet-stream"
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Temporary Mailgun URL (Phase 1) or permanent storage key (Phase 2)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    inline: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    content_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        default=func.now(), nullable=False
    )

    email_metadata: Mapped["EmailMessageMetadata"] = relationship(
        back_populates="attachments"
    )
