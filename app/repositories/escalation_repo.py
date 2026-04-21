"""
Repository for escalation CRUD operations.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.escalation import Escalation, ESCALATION_PRIORITY


async def create(
    db: AsyncSession,
    conversation_id: int,
    session_id: int,
    escalation_type: str,
    reason: str,
    guest_message: str,
    ai_was_enabled: bool,
    context: str | None = None,
) -> Escalation:
    esc = Escalation(
        conversation_id=conversation_id,
        session_id=session_id,
        escalation_type=escalation_type,
        reason=reason,
        context=context,
        guest_message=guest_message,
        priority=ESCALATION_PRIORITY.get(escalation_type, 1),
        ai_was_enabled=ai_was_enabled,
    )
    db.add(esc)
    await db.flush()
    return esc


async def find_pending_for_session(
    db: AsyncSession, session_id: int,
) -> list[Escalation]:
    result = await db.execute(
        select(Escalation).where(
            Escalation.session_id == session_id,
            Escalation.status == "pending",
        ).order_by(Escalation.created_at.desc())
    )
    return list(result.scalars().all())


async def find_pending_for_conversation(
    db: AsyncSession, conversation_id: int,
) -> list[Escalation]:
    result = await db.execute(
        select(Escalation).where(
            Escalation.conversation_id == conversation_id,
            Escalation.status == "pending",
        ).order_by(Escalation.created_at.desc())
    )
    return list(result.scalars().all())


async def find_by_id(
    db: AsyncSession, escalation_id: int,
) -> Escalation | None:
    return await db.get(Escalation, escalation_id)


async def find_by_id_with_messages(
    db: AsyncSession, escalation_id: int,
) -> Escalation | None:
    result = await db.execute(
        select(Escalation)
        .options(selectinload(Escalation.messages))
        .where(Escalation.id == escalation_id)
    )
    return result.scalar_one_or_none()


async def resolve(
    db: AsyncSession,
    escalation: Escalation,
    resolved_by: str | None = None,
    resolution_medium: str | None = None,
    resolution_notes: str | None = None,
) -> None:
    escalation.status = "resolved"
    escalation.resolved_at = datetime.now(timezone.utc)
    if resolved_by:
        escalation.resolved_by = resolved_by
    if resolution_medium:
        escalation.resolution_medium = resolution_medium
    if resolution_notes:
        escalation.resolution_notes = resolution_notes


async def list_for_property(
    db: AsyncSession,
    property_id: int,
    status: str | None = None,
    limit: int = 50,
) -> list[Escalation]:
    from app.models.session import AttentionSession
    query = (
        select(Escalation)
        .join(AttentionSession, Escalation.session_id == AttentionSession.id)
        .where(AttentionSession.property_id == property_id)
    )
    if status:
        query = query.where(Escalation.status == status)
    query = query.order_by(Escalation.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return list(result.scalars().all())
