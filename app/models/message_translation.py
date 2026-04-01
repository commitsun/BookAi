from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class MessageTranslation(Base):
    """
    A translation of a Message's content into a specific language.

    Each row is keyed by (message_id, language) — a message can have at most
    one translation per target language, but any number of languages.

    The original content always lives on Message.content / Message.content_language.
    Translations are derived artifacts: they are created on demand (when the app
    requests a language that doesn't exist yet) and cached here for reuse.

    Who produced the translation (AI, human, external service) is intentionally
    not tracked in Phase 1. If audit is needed later, a `translated_by` column
    can be added without breaking this structure.
    """

    __tablename__ = "message_translations"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(10), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), nullable=False
    )

    message: Mapped["Message"] = relationship(back_populates="translations")
