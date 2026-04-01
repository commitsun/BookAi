"""
Integration tests for the unread count feature.

Covers: get_unread_counts repo function, mark_read upsert,
and PATCH /conversations/{id}/read endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationRead
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageSender
from app.repositories import conversation_repo
from tests.conftest import GUEST_PHONE, TEST_TOKEN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_inbound(
    db: AsyncSession,
    conversation_id: int,
    endpoint_id: int,
    created_at: datetime | None = None,
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        channel_endpoint_id=endpoint_id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content="hello",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
    )
    if created_at is not None:
        msg.created_at = created_at
    db.add(msg)
    await db.flush()
    return msg


async def _insert_outbound(db: AsyncSession, conversation_id: int, endpoint_id: int) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        channel_endpoint_id=endpoint_id,
        direction=MessageDirection.outbound,
        sender=MessageSender.agent,
        content="reply",
        wa_message_type="text",
        delivery_status=DeliveryStatus.sent,
    )
    db.add(msg)
    await db.flush()
    return msg


# ---------------------------------------------------------------------------
# Tests — unread count logic
# ---------------------------------------------------------------------------


async def test_unread_count_null_cursor(
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
) -> None:
    """No ConversationRead row → all inbound messages count as unread."""
    await _insert_inbound(db, seed_conversation.id, seed_endpoint.id)
    await _insert_inbound(db, seed_conversation.id, seed_endpoint.id)

    counts = await conversation_repo.get_unread_counts(
        db, [seed_conversation.id], seed_property.id
    )
    assert counts.get(seed_conversation.id, 0) == 2


async def test_unread_count_after_mark_read(
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
) -> None:
    """After mark_read, unread count becomes 0."""
    await _insert_inbound(db, seed_conversation.id, seed_endpoint.id)

    await conversation_repo.mark_read(db, seed_conversation.id, seed_property.id)

    counts = await conversation_repo.get_unread_counts(
        db, [seed_conversation.id], seed_property.id
    )
    assert counts.get(seed_conversation.id, 0) == 0


async def test_unread_count_new_message_after_read(
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
) -> None:
    """Message received after the read cursor counts as 1 unread."""
    # Insert a message, mark as read, then insert another
    await _insert_inbound(db, seed_conversation.id, seed_endpoint.id)
    await conversation_repo.mark_read(db, seed_conversation.id, seed_property.id)

    # New message arrives after the read cursor — use explicit future timestamp
    # to avoid ties when mark_read and the new message share the same microsecond
    await _insert_inbound(
        db,
        seed_conversation.id,
        seed_endpoint.id,
        created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
    )

    counts = await conversation_repo.get_unread_counts(
        db, [seed_conversation.id], seed_property.id
    )
    assert counts.get(seed_conversation.id, 0) == 1


async def test_mark_read_upsert(
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """Two calls to mark_read → exactly one ConversationRead row."""
    await conversation_repo.mark_read(db, seed_conversation.id, seed_property.id)
    await conversation_repo.mark_read(db, seed_conversation.id, seed_property.id)

    result = await db.execute(
        select(ConversationRead).where(
            ConversationRead.conversation_id == seed_conversation.id,
            ConversationRead.property_id == seed_property.id,
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1


async def test_unread_count_outbound_ignored(
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
) -> None:
    """Outbound messages are never counted as unread."""
    await _insert_outbound(db, seed_conversation.id, seed_endpoint.id)
    await _insert_outbound(db, seed_conversation.id, seed_endpoint.id)

    counts = await conversation_repo.get_unread_counts(
        db, [seed_conversation.id], seed_property.id
    )
    assert counts.get(seed_conversation.id, 0) == 0


# ---------------------------------------------------------------------------
# Tests — PATCH /conversations/{id}/read endpoint
# ---------------------------------------------------------------------------


async def test_mark_read_endpoint_returns_204(
    client: AsyncClient,
    auth_headers: dict,
    seed_conversation,
    seed_property,
) -> None:
    response = await client.patch(
        f"/api/v1/conversations/{seed_conversation.id}/read",
        params={"property_id": seed_property.id},
        headers=auth_headers,
    )
    assert response.status_code == 204
