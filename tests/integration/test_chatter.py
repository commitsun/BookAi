"""
Integration tests for Flow 3: POST /api/v1/chatter/send-message.

The channel_endpoint has mock_mode=True, so no real Meta calls are made.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationChannelState
from app.models.message import Message, MessageDirection, MessageSender
from tests.conftest import GUEST_PHONE, TEST_PHONE_NUMBER_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send_body(
    conversation_id: int,
    content: str = "Hello guest",
    channel_endpoint_id: int | None = None,
) -> dict:
    body: dict = {
        "conversation_id": conversation_id,
        "content": content,
        "agent_user_id": 42,
        "agent_display_name": "Test Agent",
    }
    if channel_endpoint_id is not None:
        body["channel_endpoint_id"] = channel_endpoint_id
    return body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_send_message_within_window(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
    seed_attention_session,
    seed_channel_state_open,
) -> None:
    """Window open (last_inbound_at=1h ago) → 200, message persisted as outbound."""
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(seed_conversation.id),
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["conversation_id"] == seed_conversation.id

    result = await db.execute(
        select(Message).where(Message.id == data["message_id"])
    )
    msg = result.scalar_one()
    assert msg.direction == MessageDirection.outbound
    assert msg.sender == MessageSender.agent
    assert msg.content == "Hello guest"


async def test_send_message_window_closed(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
) -> None:
    """No channel state (window never opened) → 422."""
    # No ConversationChannelState → last_inbound_at is None → window closed
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(seed_conversation.id),
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_send_message_mock_mode_returns_fake_id(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
    seed_attention_session,
    seed_channel_state_open,
) -> None:
    """mock_mode=True → wa_message_id starts with 'wamid.mock.'."""
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(seed_conversation.id),
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["wa_message_id"].startswith("wamid.mock.")


async def test_send_message_explicit_channel(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
    seed_attention_session,
    seed_channel_state_open,
) -> None:
    """Explicit channel_endpoint_id → accepted without default-channel lookup."""
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(seed_conversation.id, channel_endpoint_id=seed_endpoint.id),
        headers=auth_headers,
    )
    assert response.status_code == 200


async def test_send_message_conversation_not_found(
    client: AsyncClient,
    auth_headers: dict,
    seed_instance,
) -> None:
    """Non-existent conversation_id → 404."""
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(conversation_id=999999),
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_send_message_default_channel(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
    seed_property,
    seed_attention_session,
    seed_channel_state_open,
) -> None:
    """Without channel_endpoint_id, defaults to most recently used channel."""
    # seed_channel_state_open already sets the channel state for seed_endpoint
    response = await client.post(
        "/api/v1/chatter/send-message",
        json=_send_body(seed_conversation.id),  # no explicit channel
        headers=auth_headers,
    )
    assert response.status_code == 200
    result = await db.execute(
        select(Message).where(Message.id == response.json()["message_id"])
    )
    msg = result.scalar_one()
    assert msg.channel_endpoint_id == seed_endpoint.id
