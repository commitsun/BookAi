"""
Escalation — represents a case where the AI cannot resolve a guest's request
and human intervention is needed.

An escalation belongs to a conversation+session and has its own message thread
(messages with escalation_id pointing to this record). Multiple escalations
can exist in the same session over time.

Lifecycle: pending → resolved | cancelled
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class EscalationType(str, enum.Enum):
    manual = "manual"                # guest asked to speak to a person
    info_not_found = "info_not_found"  # AI couldn't find the answer
    bad_response = "bad_response"    # supervisor rejected AI response
    inappropriate = "inappropriate"  # content not allowed


class EscalationStatus(str, enum.Enum):
    pending = "pending"
    resolved = "resolved"
    cancelled = "cancelled"


# Priority: higher = more urgent
ESCALATION_PRIORITY = {
    "manual": 1,
    "info_not_found": 2,
    "bad_response": 3,
    "inappropriate": 4,
}


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), nullable=False,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("attention_sessions.id"), nullable=False,
    )

    # Type and context
    escalation_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(Text, nullable=True)
    guest_message: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # State
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
    )
    draft_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    # AI state before escalation (to restore on resolve)
    ai_was_enabled: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Resolution
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolution_medium: Mapped[str | None] = mapped_column(String(30), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship()
    session: Mapped["AttentionSession"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="escalation",
        foreign_keys="Message.escalation_id",
    )
