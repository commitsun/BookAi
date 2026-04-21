import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class SessionStatus(str, enum.Enum):
    active = "active"
    closed = "closed"


class AttentionSession(Base):
    """
    The operational context of a hotel actively attending to a guest.

    An AttentionSession binds a Conversation (channel side) to a Property (hotel side)
    for a bounded period of time. It is the unit of routing: inbound messages are
    dispatched to the hotel that owns the active session for that conversation.

    A session is opened when Roomdoo explicitly sends a template (an intentional act
    of contact initiation). It is closed manually or by future business logic.

    Routing rules:
    - 0 active sessions for a conversation → message is unassigned
    - 1 active session → message is routed to that session's property
    - 2+ active sessions → ambiguous, treated as unassigned

    A session can be associated with multiple folios (N:M via SessionFolio), because
    a single stay may involve multiple reservation records, and a single folio may
    involve multiple guests each with their own session.
    """

    __tablename__ = "attention_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False
    )
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"), nullable=True
    )
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"), default=SessionStatus.active, nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Odoo agent ID pinned to this session (NULL = let AgentSelector decide)
    active_agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # AI toggle per session. Defaults to True (inherits property setting at runtime).
    # Can be manually disabled by an operator from the app.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Type of caller: external_guest | internal | roomdoo
    # Set once when the session is created via caller_identifier.
    caller_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(back_populates="attention_sessions")
    property: Mapped["Property"] = relationship(back_populates="attention_sessions")
    session_folios: Mapped[list["SessionFolio"]] = relationship(back_populates="session")
    messages: Mapped[list["Message"]] = relationship(back_populates="attention_session")
