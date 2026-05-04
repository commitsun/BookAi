"""
Integration tests for the conversations REST API.

Covers: GET /conversations/, GET /conversations/search,
GET /conversations/{id}/messages, PATCH /conversations/{id}/read,
and 401 on missing token.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import ConversationChannelState
from app.models.folio import Folio, FolioStatus, SessionFolio
from app.models.message import DeliveryStatus, Message, MessageDirection, MessageSender
from app.models.message_translation import MessageTranslation
from app.models.session import AttentionSession, SessionStatus
from tests.conftest import GUEST_PHONE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_message(
    db: AsyncSession,
    conversation_id: int,
    endpoint_id: int,
    direction: MessageDirection = MessageDirection.inbound,
    sender: MessageSender = MessageSender.guest,
    content: str = "hello",
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        channel_endpoint_id=endpoint_id,
        direction=direction,
        sender=sender,
        content=content,
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
    )
    db.add(msg)
    await db.flush()
    return msg


# ---------------------------------------------------------------------------
# GET /conversations/
# ---------------------------------------------------------------------------


async def test_list_conversations_for_property(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_endpoint,
    seed_attention_session,
) -> None:
    """Inbox includes the seeded conversation for property_id."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)

    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["property_id"] == seed_property.odoo_property_id
    ids = [c["id"] for c in data["conversations"]]
    assert seed_conversation.id in ids


async def test_list_conversations_unrouted(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """property_id=0 returns conversations with an active unrouted session."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)
    # Create an unrouted session (property_id=NULL) to appear in admin inbox
    unrouted = AttentionSession(
        conversation_id=seed_conversation.id,
        property_id=None,
        status=SessionStatus.active,
    )
    db.add(unrouted)
    await db.flush()

    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["conversations"]]
    assert seed_conversation.id in ids


async def test_list_conversations_includes_unread_count(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_endpoint,
    seed_attention_session,
) -> None:
    """Each ConversationListItem has an unread_count field."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)

    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert response.status_code == 200
    items = response.json()["conversations"]
    assert any(c["id"] == seed_conversation.id for c in items)
    for item in items:
        assert "unread_count" in item


# ---------------------------------------------------------------------------
# GET /conversations/search
# ---------------------------------------------------------------------------


async def test_search_by_name(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_endpoint,
    seed_attention_session,
    seed_contact,
) -> None:
    """?q=Test matches guest display_name 'Test Guest'."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)

    response = await client.get(
        "/api/v1/conversations/search",
        params={"property_id": seed_property.odoo_property_id, "q": "Test"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["conversations"]]
    assert seed_conversation.id in ids


async def test_search_requires_param(
    client: AsyncClient,
    auth_headers: dict,
    seed_property,
) -> None:
    """Neither q nor status → 400."""
    response = await client.get(
        "/api/v1/conversations/search",
        params={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert response.status_code == 400


async def test_search_by_folio_code(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_endpoint,
    seed_attention_session,
) -> None:
    """?q=FOLIO-TEST matches conversation with that folio code."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)

    folio = Folio(
        odoo_external_code="FOLIO-TEST-001",
        status=FolioStatus.confirm,
    )
    db.add(folio)
    await db.flush()
    db.add(SessionFolio(session_id=seed_attention_session.id, folio_id=folio.id))
    await db.flush()

    response = await client.get(
        "/api/v1/conversations/search",
        params={"property_id": seed_property.odoo_property_id, "q": "FOLIO-TEST-001"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["conversations"]]
    assert seed_conversation.id in ids


async def test_search_by_status(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_endpoint,
    seed_attention_session,
) -> None:
    """?status=confirm returns only conversations with that folio status."""
    await _make_message(db, seed_conversation.id, seed_endpoint.id)

    folio = Folio(
        odoo_external_code="FOLIO-STATUS-001",
        status=FolioStatus.confirm,
    )
    db.add(folio)
    await db.flush()
    db.add(SessionFolio(session_id=seed_attention_session.id, folio_id=folio.id))
    await db.flush()

    response = await client.get(
        "/api/v1/conversations/search",
        params={"property_id": seed_property.odoo_property_id, "status": "confirm"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["conversations"]]
    assert seed_conversation.id in ids


# ---------------------------------------------------------------------------
# GET /conversations/{id}/messages
# ---------------------------------------------------------------------------


async def test_messages_returns_history(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """Returns message list in ascending order."""
    m1 = await _make_message(db, seed_conversation.id, seed_endpoint.id, content="first")
    m2 = await _make_message(db, seed_conversation.id, seed_endpoint.id, content="second")

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == seed_conversation.id
    ids = [m["id"] for m in data["messages"]]
    assert m1.id in ids and m2.id in ids
    # ascending order
    assert ids.index(m1.id) < ids.index(m2.id)


async def test_messages_pagination_before_id(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """before_id cursor excludes messages at or after that id."""
    m1 = await _make_message(db, seed_conversation.id, seed_endpoint.id, content="old")
    m2 = await _make_message(db, seed_conversation.id, seed_endpoint.id, content="new")

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"before_id": m2.id},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()["messages"]]
    assert m1.id in ids
    assert m2.id not in ids


async def test_messages_language_translated(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """?language=es with cached translation → is_translated=True."""
    msg = await _make_message(
        db, seed_conversation.id, seed_endpoint.id, content="hello"
    )
    msg.content_language = "en"
    await db.flush()

    translation = MessageTranslation(
        message_id=msg.id,
        language="es",
        content="hola",
    )
    db.add(translation)
    await db.flush()

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"language": "es"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    messages = response.json()["messages"]
    translated = next(m for m in messages if m["id"] == msg.id)
    assert translated["is_translated"] is True
    assert translated["content"] == "hola"


async def test_messages_language_not_cached(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """?language=fr without cached translation → original content, is_translated=False."""
    msg = await _make_message(
        db, seed_conversation.id, seed_endpoint.id, content="hello"
    )
    msg.content_language = "en"
    await db.flush()

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"language": "fr"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    messages = response.json()["messages"]
    found = next(m for m in messages if m["id"] == msg.id)
    assert found["is_translated"] is False
    assert found["content"] == "hello"


# ---------------------------------------------------------------------------
# PATCH /conversations/{id}/read
# ---------------------------------------------------------------------------


async def test_mark_read_returns_204(
    client: AsyncClient,
    auth_headers: dict,
    seed_conversation,
    seed_property,
) -> None:
    response = await client.patch(
        f"/api/v1/conversations/{seed_conversation.id}/read",
        params={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# POST /conversations/{id}/assign
# ---------------------------------------------------------------------------


async def test_assign_creates_session(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """Unassigned conversation + valid property → 200, AttentionSession created."""
    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/assign",
        json={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == seed_conversation.id
    assert data["property_id"] == seed_property.odoo_property_id
    assert data["created"] is True

    from sqlalchemy import select as sa_select
    result = await db.execute(
        sa_select(AttentionSession).where(
            AttentionSession.conversation_id == seed_conversation.id,
            AttentionSession.property_id == seed_property.id,
        )
    )
    assert result.scalar_one_or_none() is not None


async def test_assign_idempotent(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """Assigning the same property twice → second call returns created=False."""
    r1 = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/assign",
        json={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["created"] is True

    r2 = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/assign",
        json={"property_id": seed_property.odoo_property_id},
        headers=auth_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["attention_session_id"] == r1.json()["attention_session_id"]


async def test_assign_unknown_property(
    client: AsyncClient,
    auth_headers: dict,
    seed_conversation,
) -> None:
    """Property not in this instance → 404."""
    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/assign",
        json={"property_id": 999999},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_assign_conversation_not_found(
    client: AsyncClient,
    auth_headers: dict,
    seed_instance,
) -> None:
    """Non-existent conversation → 404."""
    response = await client.post(
        "/api/v1/conversations/999999/assign",
        json={"property_id": 1},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_unauthorized_returns_401(client: AsyncClient) -> None:
    """Missing Bearer token → 401."""
    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 1},
    )
    assert response.status_code == 401
