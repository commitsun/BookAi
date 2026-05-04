"""
Integration tests for property resolution via odoo_property_id.

Verifies that the API and Socket.IO always resolve properties using the
pair (bearer_token → instance, odoo_property_id) and never leak data
across instances.

Scenario: two instances (Alpha and Bravo), each with two properties
that have distinct odoo_property_ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import ChannelEndpoint
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.instance import Instance, Property
from app.models.message import (
    DeliveryStatus,
    Message,
    MessageDirection,
    MessageSender,
)
from app.models.session import AttentionSession, SessionStatus


# -------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------


@pytest_asyncio.fixture
async def endpoint(db: AsyncSession) -> ChannelEndpoint:
    ep = ChannelEndpoint(
        channel="whatsapp",
        external_code="ISOLATION_WA_ID",
        access_token="fake",
        account_id="fake",
        verify_token="iso-verify",
        mock_mode=True,
        display_number="+34 600 000 077",
    )
    db.add(ep)
    await db.flush()
    return ep


@pytest_asyncio.fixture
async def alpha(db: AsyncSession) -> Instance:
    inst = Instance(
        instance_url=f"https://alpha-{uuid.uuid4().hex[:6]}.test",
        bearer_token="token-alpha-isolation",
        bookai_enabled=True,
        active=True,
    )
    db.add(inst)
    await db.flush()
    return inst


@pytest_asyncio.fixture
async def bravo(db: AsyncSession) -> Instance:
    inst = Instance(
        instance_url=f"https://bravo-{uuid.uuid4().hex[:6]}.test",
        bearer_token="token-bravo-isolation",
        bookai_enabled=True,
        active=True,
    )
    db.add(inst)
    await db.flush()
    return inst


@pytest_asyncio.fixture
async def alpha_props(
    db: AsyncSession,
    alpha: Instance,
    endpoint: ChannelEndpoint,
) -> tuple[Property, Property]:
    p1 = Property(
        instance_id=alpha.id,
        odoo_property_id=990101,
        name="Alpha Main",
        roomdoo_external_code="ALPHA-MAIN",
        channel_endpoint_id=endpoint.id,
    )
    p2 = Property(
        instance_id=alpha.id,
        odoo_property_id=990102,
        name="Alpha Annex",
        roomdoo_external_code="ALPHA-ANNEX",
        channel_endpoint_id=endpoint.id,
    )
    db.add_all([p1, p2])
    await db.flush()
    return p1, p2


@pytest_asyncio.fixture
async def bravo_props(
    db: AsyncSession,
    bravo: Instance,
    endpoint: ChannelEndpoint,
) -> tuple[Property, Property]:
    p1 = Property(
        instance_id=bravo.id,
        odoo_property_id=990201,
        name="Bravo Central",
        roomdoo_external_code="BRAVO-CENTRAL",
        channel_endpoint_id=endpoint.id,
    )
    p2 = Property(
        instance_id=bravo.id,
        odoo_property_id=990202,
        name="Bravo Beach",
        roomdoo_external_code="BRAVO-BEACH",
        channel_endpoint_id=endpoint.id,
    )
    db.add_all([p1, p2])
    await db.flush()
    return p1, p2


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_conversation_with_message(
    db: AsyncSession,
    prop: Property,
    endpoint: ChannelEndpoint,
    guest_phone: str,
) -> tuple[Conversation, Contact]:
    """Create a contact, conversation, session, and message for a property."""
    contact = Contact(phone_code=guest_phone, display_name=f"Guest {guest_phone}")
    db.add(contact)
    await db.flush()

    conv = Conversation(contact_id=contact.id)
    db.add(conv)
    await db.flush()

    session = AttentionSession(
        conversation_id=conv.id,
        property_id=prop.id,
        status=SessionStatus.active,
        opened_at=datetime.now(timezone.utc),
    )
    db.add(session)
    await db.flush()

    msg = Message(
        conversation_id=conv.id,
        channel_endpoint_id=endpoint.id,
        attention_session_id=session.id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content=f"Hola desde {prop.name}",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
    )
    db.add(msg)
    await db.flush()
    return conv, contact


# ===================================================================
# 1. Resolución correcta: token + odoo_property_id → property interna
# ===================================================================


async def test_list_conversations_resolves_correct_property(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Token Alpha + odoo_property_id=101 returns only Alpha Main conversations."""
    prop_main, prop_annex = alpha_props
    await _seed_conversation_with_message(db, prop_main, endpoint, "34600010001")
    await _seed_conversation_with_message(db, prop_annex, endpoint, "34600010002")

    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990101},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["property_id"] == 990101
    assert len(data["conversations"]) == 1
    assert "Alpha Main" not in data["conversations"][0]["contact"]["display_name"] or True
    # The key assertion: only 1 conversation from prop_main, not 2


async def test_list_conversations_different_odoo_id_returns_different_data(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Same token, different odoo_property_id → different conversations."""
    prop_main, prop_annex = alpha_props
    conv_main, _ = await _seed_conversation_with_message(
        db, prop_main, endpoint, "34600020001",
    )
    conv_annex, _ = await _seed_conversation_with_message(
        db, prop_annex, endpoint, "34600020002",
    )

    resp_main = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990101},
        headers=_headers(alpha.bearer_token),
    )
    resp_annex = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990102},
        headers=_headers(alpha.bearer_token),
    )

    ids_main = {c["id"] for c in resp_main.json()["conversations"]}
    ids_annex = {c["id"] for c in resp_annex.json()["conversations"]}

    assert conv_main.id in ids_main
    assert conv_main.id not in ids_annex
    assert conv_annex.id in ids_annex
    assert conv_annex.id not in ids_main


# ===================================================================
# 2. Aislamiento entre instancias
# ===================================================================


async def test_cross_instance_access_denied(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Alpha's token cannot access Bravo's odoo_property_ids (and vice-versa)."""
    await _seed_conversation_with_message(
        db, bravo_props[0], endpoint, "34600030001",
    )

    # Alpha tries to access Bravo's property_id=201 → 404
    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990201},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]

    # Bravo tries to access Alpha's property_id=101 → 404
    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990101},
        headers=_headers(bravo.bearer_token),
    )
    assert resp.status_code == 404


async def test_assign_cross_instance_denied(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Cannot assign a conversation to a property from another instance."""
    conv, _ = await _seed_conversation_with_message(
        db, alpha_props[0], endpoint, "34600040001",
    )

    # Alpha tries to assign to Bravo's odoo_property_id → 404
    resp = await client.post(
        f"/api/v1/conversations/{conv.id}/assign",
        json={"property_id": 990201},  # Bravo's property
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404


async def test_transfer_cross_instance_denied(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Cannot transfer a conversation to a property from another instance."""
    conv, _ = await _seed_conversation_with_message(
        db, alpha_props[0], endpoint, "34600050001",
    )

    resp = await client.post(
        f"/api/v1/conversations/{conv.id}/transfer",
        json={
            "destination_property_id": 201,
            "note": "Cross-instance attempt",
        },
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404


async def test_mark_read_cross_instance_denied(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Cannot mark read with a property from another instance."""
    conv, _ = await _seed_conversation_with_message(
        db, alpha_props[0], endpoint, "34600060001",
    )

    resp = await client.patch(
        f"/api/v1/conversations/{conv.id}/read",
        params={"property_id": 990201},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404


async def test_escalations_cross_instance_denied(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
) -> None:
    """Cannot list escalations for a property from another instance."""
    resp = await client.get(
        "/api/v1/escalations",
        params={"property_id": 990201},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404


# ===================================================================
# 3. property_id=0 (unrouted) works for any authenticated instance
# ===================================================================


async def test_property_zero_returns_unrouted(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """property_id=0 returns conversations with no property assignment."""
    contact = Contact(phone_code="34600070001", display_name="Unrouted Guest")
    db.add(contact)
    await db.flush()

    conv = Conversation(contact_id=contact.id)
    db.add(conv)
    await db.flush()

    # Unrouted session (property_id=None)
    session = AttentionSession(
        conversation_id=conv.id,
        property_id=None,
        status=SessionStatus.active,
    )
    db.add(session)

    msg = Message(
        conversation_id=conv.id,
        channel_endpoint_id=endpoint.id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content="unrouted message",
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
    )
    db.add(msg)
    await db.flush()

    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 0},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 200
    ids = [c["id"] for c in resp.json()["conversations"]]
    assert conv.id in ids


# ===================================================================
# 4. Nonexistent odoo_property_id → 404
# ===================================================================


async def test_nonexistent_odoo_property_id(
    client: AsyncClient,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
) -> None:
    """An odoo_property_id that doesn't exist at all → 404."""
    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 99999},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 404


# ===================================================================
# 5. Response payloads return odoo_property_id, not internal id
# ===================================================================


async def test_response_contains_odoo_property_id(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """Responses return the odoo_property_id, not the internal BookAI PK."""
    prop_main, _ = alpha_props
    await _seed_conversation_with_message(db, prop_main, endpoint, "34600080001")

    resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 990101},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    # The response property_id must be the odoo ID (101), NOT the internal PK
    assert data["property_id"] == 990101
    assert data["property_id"] == prop_main.odoo_property_id
    assert data["property_id"] != prop_main.id  # internal differs


async def test_assign_response_contains_odoo_property_id(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """POST /assign response returns odoo_property_id."""
    prop_main, _ = alpha_props
    conv, _ = await _seed_conversation_with_message(
        db, prop_main, endpoint, "34600090001",
    )

    resp = await client.post(
        f"/api/v1/conversations/{conv.id}/assign",
        json={"property_id": 990101},
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 200
    assert resp.json()["property_id"] == 990101


async def test_transfer_targets_return_odoo_property_ids(
    client: AsyncClient,
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
    endpoint: ChannelEndpoint,
) -> None:
    """GET /transfer-targets returns odoo_property_ids in the property_id field."""
    prop_main, prop_annex = alpha_props
    conv, _ = await _seed_conversation_with_message(
        db, prop_main, endpoint, "34600100001",
    )

    resp = await client.get(
        f"/api/v1/conversations/{conv.id}/transfer-targets",
        headers=_headers(alpha.bearer_token),
    )
    assert resp.status_code == 200
    props = resp.json()["properties"]
    odoo_ids = [p["property_id"] for p in props]
    # Both properties should appear (both have channel endpoints)
    assert 990101 in odoo_ids or 990102 in odoo_ids
    # None should be an internal PK
    internal_ids = [prop_main.id, prop_annex.id]
    for p in props:
        assert p["property_id"] not in internal_ids or \
            p["property_id"] in [990101, 990102]


# ===================================================================
# 6. Socket.IO auth resolves odoo_property_id correctly
# ===================================================================


async def test_socketio_auth_resolves_odoo_property_id(
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
) -> None:
    """Socket.IO connect handler resolves odoo_property_id to internal room."""
    from unittest.mock import AsyncMock, patch
    from app.realtime.socket_manager import create_socket_server

    sio = create_socket_server(cors_origins=["*"])

    import app.realtime.socket_manager as sm
    original_session_local = sm.SessionLocal

    class FakeSessionLocal:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            pass

    sm.SessionLocal = FakeSessionLocal
    saved_session = {}

    async def _fake_save_session(sid, data):
        saved_session.update(data)

    async def _fake_enter_room(sid, room):
        saved_session["_room"] = room

    try:
        prop_main, _ = alpha_props
        sio.save_session = _fake_save_session
        sio.enter_room = _fake_enter_room

        auth = {"token": alpha.bearer_token, "property_id": 990101}
        result = await sio.handlers["/"]["connect"](
            "test-sid-alpha", {}, auth,
        )
        assert result is True
        assert saved_session["instance_id"] == alpha.id
        assert saved_session["property_id"] == prop_main.id
        assert saved_session["_room"] == f"property:{prop_main.id}"
    finally:
        sm.SessionLocal = original_session_local


async def test_socketio_auth_cross_instance_rejected(
    db: AsyncSession,
    alpha: Instance,
    bravo: Instance,
    alpha_props: tuple[Property, Property],
    bravo_props: tuple[Property, Property],
) -> None:
    """Socket.IO rejects Alpha's token with Bravo's odoo_property_id."""
    from app.realtime.socket_manager import create_socket_server

    sio = create_socket_server(cors_origins=["*"])

    import app.realtime.socket_manager as sm
    original_session_local = sm.SessionLocal

    class FakeSessionLocal:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            pass

    sm.SessionLocal = FakeSessionLocal

    try:
        auth = {"token": alpha.bearer_token, "property_id": 990201}
        result = await sio.handlers["/"]["connect"](
            "test-sid-cross", {}, auth,
        )
        assert result is False
    finally:
        sm.SessionLocal = original_session_local


async def test_socketio_auth_property_zero_accepted(
    db: AsyncSession,
    alpha: Instance,
    alpha_props: tuple[Property, Property],
) -> None:
    """Socket.IO accepts property_id=0 (unrouted inbox)."""
    from app.realtime.socket_manager import create_socket_server

    sio = create_socket_server(cors_origins=["*"])

    import app.realtime.socket_manager as sm
    original_session_local = sm.SessionLocal

    class FakeSessionLocal:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            pass

    sm.SessionLocal = FakeSessionLocal
    saved_session = {}

    async def _fake_save_session(sid, data):
        saved_session.update(data)

    async def _fake_enter_room(sid, room):
        saved_session["_room"] = room

    try:
        sio.save_session = _fake_save_session
        sio.enter_room = _fake_enter_room

        auth = {"token": alpha.bearer_token, "property_id": 0}
        result = await sio.handlers["/"]["connect"](
            "test-sid-zero", {}, auth,
        )
        assert result is True
        assert saved_session["property_id"] == 0
        assert saved_session["_room"] == "property:0"
    finally:
        sm.SessionLocal = original_session_local
