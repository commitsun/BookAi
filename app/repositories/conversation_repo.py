from datetime import datetime, timezone

from sqlalchemy import case, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.conversation import Conversation, ConversationChannelState, ConversationRead
from app.models.message import Message, MessageDirection, MessageKind
from app.models.session import AttentionSession, SessionStatus


async def list_for_property(
    db: AsyncSession,
    property_id: int,
    limit: int = 50,
    conversation_type: str = "guest",
) -> list[Conversation]:
    """
    Return conversations for a property, ordered by priority groups then by
    last message time (most recent first) within each group.

    Priority groups:
      0 — Escalated: conversations with at least one pending escalation.
      1 — AI off + unread: AI disabled and unread inbound messages exist.
      2 — Everything else.

    Uses correlated subqueries so the inbox reflects actual state.
    """
    from app.models.escalation import Escalation

    # Exclude auto-generated folio-event notes (template_code IS NOT NULL)
    # from ordering so they don't bump conversations in the inbox.
    last_msg_at = (
        select(func.max(Message.created_at))
        .where(
            Message.conversation_id == Conversation.id,
            ~(
                (Message.kind == MessageKind.note)
                & Message.template_code.isnot(None)
            ),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )

    # Group 0: pending escalation for this property's session
    has_escalation = (
        exists(
            select(literal(1))
            .select_from(Escalation)
            .join(AttentionSession, AttentionSession.id == Escalation.session_id)
            .where(
                Escalation.conversation_id == Conversation.id,
                Escalation.status == "pending",
                AttentionSession.property_id == property_id,
            )
        )
    )

    # Group 1: AI disabled + has unread inbound messages
    ai_off = (
        exists(
            select(literal(1))
            .select_from(AttentionSession)
            .where(
                AttentionSession.conversation_id == Conversation.id,
                AttentionSession.property_id == property_id,
                AttentionSession.status == SessionStatus.active,
                AttentionSession.ai_enabled == False,  # noqa: E712
            )
        )
    )

    read_at_subq = (
        select(ConversationRead.last_read_at)
        .where(
            ConversationRead.conversation_id == Conversation.id,
            ConversationRead.property_id == property_id,
        )
        .correlate(Conversation)
        .scalar_subquery()
    )

    has_unread = (
        exists(
            select(literal(1))
            .select_from(Message)
            .where(
                Message.conversation_id == Conversation.id,
                Message.direction == MessageDirection.inbound,
                (read_at_subq == None) |  # noqa: E711
                (Message.created_at > read_at_subq),
            )
        )
    )

    priority = case(
        (has_escalation, 0),
        (ai_off & has_unread, 1),
        else_=2,
    )

    stmt = (
        select(Conversation)
        .where(
            Conversation.id.in_(
                select(AttentionSession.conversation_id)
                .where(AttentionSession.property_id == property_id)
                .distinct()
            ),
            Conversation.conversation_type == conversation_type,
        )
        .options(selectinload(Conversation.contact))
        .order_by(priority, last_msg_at.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_unrouted(
    db: AsyncSession,
    limit: int = 50,
) -> list[Conversation]:
    """
    Conversations sitting in the admin inbox (property:0).

    These have an active AttentionSession with property_id IS NULL —
    created automatically when an inbound message cannot be routed to
    a specific property (multiple properties on the endpoint, none active).
    """
    last_msg_at = (
        select(func.max(Message.created_at))
        .where(
            Message.conversation_id == Conversation.id,
            ~(
                (Message.kind == MessageKind.note)
                & Message.template_code.isnot(None)
            ),
        )
        .correlate(Conversation)
        .scalar_subquery()
    )
    stmt = (
        select(Conversation)
        .where(
            Conversation.id.in_(
                select(AttentionSession.conversation_id)
                .where(
                    AttentionSession.property_id.is_(None),
                    AttentionSession.status == SessionStatus.active,
                )
                .distinct()
            )
        )
        .options(selectinload(Conversation.contact))
        .order_by(last_msg_at.desc().nullslast())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_or_create(
    db: AsyncSession,
    contact_id: int,
) -> tuple[Conversation, bool]:
    """Get or create a guest conversation for a contact."""
    result = await db.execute(
        select(Conversation).where(
            Conversation.contact_id == contact_id,
            Conversation.conversation_type == "guest",
        )
    )
    conv = result.scalar_one_or_none()
    if conv:
        return conv, False
    conv = Conversation(contact_id=contact_id, conversation_type="guest")
    db.add(conv)
    await db.flush()
    return conv, True


async def find_by_id(
    db: AsyncSession, conversation_id: int
) -> Conversation | None:
    result = await db.execute(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .options(
            selectinload(Conversation.contact),
            selectinload(Conversation.channel_states).selectinload(
                ConversationChannelState.channel_endpoint
            ),
        )
    )
    return result.scalar_one_or_none()


async def get_or_create_channel_state(
    db: AsyncSession,
    conversation_id: int,
    channel_endpoint_id: int,
) -> tuple[ConversationChannelState, bool]:
    result = await db.execute(
        select(ConversationChannelState).where(
            ConversationChannelState.conversation_id == conversation_id,
            ConversationChannelState.channel_endpoint_id == channel_endpoint_id,
        )
    )
    state = result.scalar_one_or_none()
    if state:
        return state, False
    state = ConversationChannelState(
        conversation_id=conversation_id,
        channel_endpoint_id=channel_endpoint_id,
    )
    db.add(state)
    await db.flush()
    return state, True


async def find_channel_state(
    db: AsyncSession,
    conversation_id: int,
    channel_endpoint_id: int,
) -> ConversationChannelState | None:
    result = await db.execute(
        select(ConversationChannelState).where(
            ConversationChannelState.conversation_id == conversation_id,
            ConversationChannelState.channel_endpoint_id == channel_endpoint_id,
        )
    )
    return result.scalar_one_or_none()


async def mark_read(
    db: AsyncSession,
    conversation_id: int,
    property_id: int,
) -> None:
    """Upsert the read cursor for a property on a conversation."""
    result = await db.execute(
        select(ConversationRead).where(
            ConversationRead.conversation_id == conversation_id,
            ConversationRead.property_id == property_id,
        )
    )
    read = result.scalar_one_or_none()
    if read:
        read.last_read_at = datetime.now(timezone.utc)
    else:
        db.add(ConversationRead(
            conversation_id=conversation_id,
            property_id=property_id,
            last_read_at=datetime.now(timezone.utc),
        ))


async def get_unread_counts(
    db: AsyncSession,
    conversation_ids: list[int],
    property_id: int,
) -> dict[int, int]:
    """
    Returns {conversation_id: unread_count} for the given property.
    Unread = inbound messages created after the property's last_read_at cursor.
    Conversations with no read cursor count all inbound messages as unread.
    """
    if not conversation_ids:
        return {}

    read_subq = (
        select(ConversationRead.conversation_id, ConversationRead.last_read_at)
        .where(
            ConversationRead.conversation_id.in_(conversation_ids),
            ConversationRead.property_id == property_id,
        )
        .subquery()
    )
    stmt = (
        select(
            Message.conversation_id,
            func.count(Message.id).label("unread"),
        )
        .outerjoin(read_subq, read_subq.c.conversation_id == Message.conversation_id)
        .where(
            Message.conversation_id.in_(conversation_ids),
            Message.direction == MessageDirection.inbound,
            (read_subq.c.last_read_at == None) |  # noqa: E711
            (Message.created_at > read_subq.c.last_read_at),
        )
        .group_by(Message.conversation_id)
    )
    result = await db.execute(stmt)
    return {row.conversation_id: row.unread for row in result.all()}


async def get_needs_attention(
    db: AsyncSession,
    conversation_ids: list[int],
    property_id: int,
) -> dict[int, bool]:
    """
    Returns {conversation_id: True} for conversations that have at least one
    unread transfer note belonging to this property's sessions.

    Transfer notes = kind=note AND template_code IN ('transfer.outgoing', 'transfer.incoming').
    "Unread" = created after the property's last_read_at cursor (or all if
    the cursor has never been set).

    Cleared by the same PATCH /conversations/{id}/read call used for messages.
    """
    if not conversation_ids:
        return {}

    read_subq = (
        select(ConversationRead.conversation_id, ConversationRead.last_read_at)
        .where(
            ConversationRead.conversation_id.in_(conversation_ids),
            ConversationRead.property_id == property_id,
        )
        .subquery()
    )
    # Only active sessions: closed sessions (e.g. source after transfer) are ignored.
    property_session_sq = (
        select(AttentionSession.id)
        .where(
            AttentionSession.property_id == property_id,
            AttentionSession.status == SessionStatus.active,
        )
        .subquery()
    )
    stmt = (
        select(Message.conversation_id)
        .outerjoin(read_subq, read_subq.c.conversation_id == Message.conversation_id)
        .where(
            Message.conversation_id.in_(conversation_ids),
            Message.kind == MessageKind.note,
            Message.template_code.in_(["transfer.outgoing", "transfer.incoming"]),
            Message.attention_session_id.in_(select(property_session_sq.c.id)),
            (read_subq.c.last_read_at == None)  # noqa: E711
            | (Message.created_at > read_subq.c.last_read_at),
        )
        .distinct()
    )
    result = await db.execute(stmt)
    flagged = {row.conversation_id for row in result.all()}
    return {cid: cid in flagged for cid in conversation_ids}


async def update_channel_last_inbound(
    db: AsyncSession, state: ConversationChannelState
) -> None:
    state.last_inbound_at = datetime.now(timezone.utc)


async def find_default_channel_endpoint_id(
    db: AsyncSession, conversation_id: int
) -> int | None:
    """
    Returns the channel_endpoint_id most recently used (by last_inbound_at) in
    this conversation. Falls back to the most recently created channel state if
    no inbound message exists yet. Returns None if no channel has been used.
    """
    result = await db.execute(
        select(ConversationChannelState)
        .where(ConversationChannelState.conversation_id == conversation_id)
        .order_by(
            ConversationChannelState.last_inbound_at.desc().nullslast(),
        )
        .limit(1)
    )
    state = result.scalar_one_or_none()
    return state.channel_endpoint_id if state else None
