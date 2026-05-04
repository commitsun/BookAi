from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folio import Folio, SessionFolio


async def find_by_code(
    db: AsyncSession, odoo_external_code: str
) -> Folio | None:
    result = await db.execute(
        select(Folio).where(
            Folio.odoo_external_code == odoo_external_code
        )
    )
    return result.scalar_one_or_none()


async def update_cache(db: AsyncSession, folio: Folio, data: dict) -> None:
    """Partial update from a push payload. Only keys present in data are written."""
    allowed = {
        "status",
        "checkin_date",
        "checkout_date",
        "pending_payment_amount",
        "pending_payment_currency",
    }
    for key, value in data.items():
        if key in allowed:
            setattr(folio, key, value)
    folio.synced_at = datetime.now(timezone.utc)


async def get_or_create(
    db: AsyncSession,
    odoo_external_code: str,
    odoo_folio_id: int | None = None,
    checkin_date: date | None = None,
    checkout_date: date | None = None,
) -> tuple[Folio, bool]:
    result = await db.execute(
        select(Folio).where(Folio.odoo_external_code == odoo_external_code)
    )
    folio = result.scalar_one_or_none()
    if folio:
        return folio, False
    folio = Folio(
        odoo_external_code=odoo_external_code,
        odoo_folio_id=odoo_folio_id,
        checkin_date=checkin_date,
        checkout_date=checkout_date,
    )
    db.add(folio)
    await db.flush()
    return folio, True


async def attach_to_session(
    db: AsyncSession, session_id: int, folio_id: int
) -> None:
    existing = await db.execute(
        select(SessionFolio).where(
            SessionFolio.session_id == session_id,
            SessionFolio.folio_id == folio_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(SessionFolio(session_id=session_id, folio_id=folio_id))
        await db.flush()
