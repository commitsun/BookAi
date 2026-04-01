from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.folio import Folio, SessionFolio
from app.models.message import Message
from app.models.session import AttentionSession, SessionStatus


async def find_active_for_conversation(
    db: AsyncSession, conversation_id: int
) -> list[AttentionSession]:
    result = await db.execute(
        select(AttentionSession).where(
            AttentionSession.conversation_id == conversation_id,
            AttentionSession.status == SessionStatus.active,
        )
    )
    return list(result.scalars().all())


async def find_sessions_with_context(
    db: AsyncSession,
    conversation_id: int,
) -> tuple[list[AttentionSession], datetime | None]:
    """
    Returns (all_sessions, conversation_last_message_at) for routing.

    - All AttentionSessions for the conversation, with session_folios +
      folio eager-loaded.
    - A synthetic `last_message_at` attribute is injected on each session
      (most recent message whose attention_session_id = session.id),
      used by pick_session().
    - conversation_last_message_at is max(Message.created_at) for the
      whole conversation, used by is_session_active() recency check.
    """
    sessions = list(
        (
            await db.execute(
                select(AttentionSession)
                .where(AttentionSession.conversation_id == conversation_id)
                .options(
                    selectinload(AttentionSession.session_folios)
                    .selectinload(SessionFolio.folio)
                )
            )
        ).scalars().all()
    )

    conv_last_msg: datetime | None = None
    if sessions:
        # Conversation-level last message timestamp (recency check)
        conv_last_msg = (
            await db.execute(
                select(func.max(Message.created_at))
                .where(Message.conversation_id == conversation_id)
            )
        ).scalar_one_or_none()

        # Per-session last message timestamp (for pick_session comparison)
        session_ids = [s.id for s in sessions]
        rows = (
            await db.execute(
                select(
                    Message.attention_session_id,
                    func.max(Message.created_at).label("last_at"),
                )
                .where(Message.attention_session_id.in_(session_ids))
                .group_by(Message.attention_session_id)
            )
        ).all()
        ts_map = {row.attention_session_id: row.last_at for row in rows}
        for s in sessions:
            # injected at runtime for pick_session
            s.last_message_at = ts_map.get(s.id)

    return sessions, conv_last_msg


async def find_or_create_unrouted(
    db: AsyncSession,
    conversation_id: int,
) -> tuple[AttentionSession, bool]:
    """
    Returns the active unrouted session (property_id IS NULL) for a
    conversation, creating one if it does not exist.

    Used when an inbound message cannot be routed to a specific property
    (multiple properties on the endpoint, none active). The session acts
    as an anchor for notes and trazability before manual assignment.
    """
    result = await db.execute(
        select(AttentionSession).where(
            AttentionSession.conversation_id == conversation_id,
            AttentionSession.property_id.is_(None),
            AttentionSession.status == SessionStatus.active,
        )
    )
    session = result.scalar_one_or_none()
    if session:
        return session, False
    session = AttentionSession(
        conversation_id=conversation_id,
        property_id=None,
        status=SessionStatus.active,
    )
    db.add(session)
    await db.flush()
    return session, True


async def close_sessions(
    db: AsyncSession,
    sessions: list[AttentionSession],
) -> None:
    """Mark a list of sessions as closed in the DB."""
    now = datetime.now(timezone.utc)
    for s in sessions:
        if s.status == SessionStatus.active:
            s.status = SessionStatus.closed
            s.closed_at = now
    await db.flush()


async def find_active_for_folio(
    db: AsyncSession,
    folio_id: int,
) -> list[AttentionSession]:
    """Return all active sessions linked to a folio via SessionFolio."""
    result = await db.execute(
        select(AttentionSession)
        .join(SessionFolio, SessionFolio.session_id == AttentionSession.id)
        .where(
            SessionFolio.folio_id == folio_id,
            AttentionSession.status == SessionStatus.active,
        )
    )
    return list(result.scalars().all())


async def get_or_create_active(
    db: AsyncSession, conversation_id: int, property_id: int
) -> tuple[AttentionSession, bool]:
    result = await db.execute(
        select(AttentionSession).where(
            AttentionSession.conversation_id == conversation_id,
            AttentionSession.property_id == property_id,
            AttentionSession.status == SessionStatus.active,
        )
    )
    session = result.scalar_one_or_none()
    if session:
        return session, False
    session = AttentionSession(
        conversation_id=conversation_id,
        property_id=property_id,
        status=SessionStatus.active,
    )
    db.add(session)
    await db.flush()
    return session, True
