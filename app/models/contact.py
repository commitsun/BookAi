from datetime import datetime

from sqlalchemy import String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Contact(Base):
    """
    The identity of a guest on the channel.

    A Contact is channel-agnostic in concept, but in Phase 1 the unique identifier
    is the phone number (E.164 digits only, no '+', no spaces).
    A Contact does not belong to any hotel — it is a global channel identity.

    Example: phone_code = "34699323583" for +34 699 323 583
    """

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)

    # E.164 digits only for WhatsApp; "email:<address>" synthetic key for email-only contacts
    phone_code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # Email address — used as identity for the email channel (Phase 1: one email per contact)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(), onupdate=func.now(), nullable=False
    )

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="contact")
