from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Conversation(Base):
    """
    The logical thread between BookAI and a Contact.

    A Conversation is channel-agnostic: messages within it can arrive or be sent
    through any number of channel endpoints (WhatsApp, Telegram, etc.).
    Which channel was used for each individual message is recorded on the Message,
    not here.

    One Conversation per Contact — the guest's identity is the anchor, regardless
    of which channel they used to reach us.

    Per-channel state (e.g. the WhatsApp 24-hour window) is tracked in
    ConversationChannelState, not here.
    """

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("contact_id", name="uq_conversation_contact"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )

    contact: Mapped["Contact"] = relationship(back_populates="conversations")
    attention_sessions: Mapped[list["AttentionSession"]] = relationship(
        back_populates="conversation"
    )
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")
    channel_states: Mapped[list["ConversationChannelState"]] = relationship(
        back_populates="conversation"
    )


class ConversationRead(Base):
    """
    Tracks when a property last read a conversation.
    Used to compute unread message counts per property.
    """

    __tablename__ = "conversation_reads"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"), primary_key=True
    )
    last_read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )


class ConversationChannelState(Base):
    """
    Per-channel state for a Conversation.

    Tracks channel-specific metadata that changes over time, such as the
    WhatsApp 24-hour messaging window (last inbound message timestamp).

    A new row is created the first time a given channel_endpoint is used
    within a conversation — either when the guest first writes, or when
    an outbound message is sent through that channel.

    This design allows future channels to add their own state without
    modifying the Conversation model.
    """

    __tablename__ = "conversation_channel_states"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    channel_endpoint_id: Mapped[int] = mapped_column(
        ForeignKey("channel_endpoints.id"), primary_key=True
    )

    # Timestamp of the last inbound message through this channel.
    # Used to evaluate the WhatsApp 24-hour window (and analogues for other channels).
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="channel_states")
    channel_endpoint: Mapped["ChannelEndpoint"] = relationship()
