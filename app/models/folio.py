import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class FolioStatus(str, enum.Enum):
    draft = "draft"
    confirm = "confirm"
    onboard = "onboard"
    done = "done"
    cancel = "cancel"


class Folio(Base):
    """
    A reservation (folio) from the PMS.

    A Folio is a PMS-side concept. It does not belong to a Contact directly — a group
    reservation can have multiple guests (Contacts), each with their own Conversation,
    all referencing the same Folio through their respective AttentionSessions.

    The relationship to AttentionSession is N:M via SessionFolio, because:
    - One AttentionSession can cover multiple folios (e.g. multiple reservations in the same property).
    - One Folio can appear in multiple AttentionSessions (e.g. multiple guests of
      the same group, each with their own conversation and session).

    Dynamic fields (status, pending_payment_*) are cached from Roomdoo via
    PATCH /api/v1/folios/{odoo_external_code} and kept up to date by push.
    """

    __tablename__ = "folios"

    id: Mapped[int] = mapped_column(primary_key=True)

    # The folio code as known by the PMS, e.g. "206/26/026072"
    odoo_external_code: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # The numeric ID of the folio in Odoo — useful for future SDK calls
    odoo_folio_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    checkin_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    checkout_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Dynamic fields — pushed by Roomdoo whenever the reservation changes
    status: Mapped[FolioStatus | None] = mapped_column(
        Enum(FolioStatus, name="folio_status"), nullable=True
    )
    pending_payment_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    pending_payment_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    session_folios: Mapped[list["SessionFolio"]] = relationship(back_populates="folio")


class SessionFolio(Base):
    """
    Junction table linking AttentionSessions to Folios (N:M).

    Records when and why a folio was attached to a session — typically at the moment
    Roomdoo sends the template that opens the session.
    """

    __tablename__ = "session_folios"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("attention_sessions.id"), primary_key=True
    )
    folio_id: Mapped[int] = mapped_column(ForeignKey("folios.id"), primary_key=True)
    attached_at: Mapped[datetime] = mapped_column(default=func.now(), nullable=False)

    session: Mapped["AttentionSession"] = relationship(back_populates="session_folios")
    folio: Mapped["Folio"] = relationship(back_populates="session_folios")
