"""
Integration tests for the transfer endpoint and unrouted session behaviour.

Covers:
- POST /conversations/{id}/transfer: closes source session, creates dest session,
  creates notes in both, returns correct response.
- Transfer from an unrouted session (property_id=NULL).
- GET /conversations/?property_id=0 returns conversations with unrouted sessions.
- Notes appear in GET /messages with kind='note'.
- 422 when destination == currently active property.
- 404 on unknown conversation or property.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from httpx import AsyncClient

from app.models.message import (
    DeliveryStatus,
    Message,
    MessageDirection,
    MessageSender,
)
from app.models.session import AttentionSession, SessionStatus
from app.models.instance import Property


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_message(
    db: AsyncSession,
    conversation_id: int,
    endpoint_id: int,
    content: str = "hello",
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        channel_endpoint_id=endpoint_id,
        direction=MessageDirection.inbound,
        sender=MessageSender.guest,
        content=content,
        wa_message_type="text",
        delivery_status=DeliveryStatus.delivered,
    )
    db.add(msg)
    await db.flush()
    return msg


async def _make_active_session(
    db: AsyncSession,
    conversation_id: int,
    property_id: int,
) -> AttentionSession:
    s = AttentionSession(
        conversation_id=conversation_id,
        property_id=property_id,
        status=SessionStatus.active,
    )
    db.add(s)
    await db.flush()
    return s


async def _make_unrouted_session(
    db: AsyncSession,
    conversation_id: int,
) -> AttentionSession:
    s = AttentionSession(
        conversation_id=conversation_id,
        property_id=None,
        status=SessionStatus.active,
    )
    db.add(s)
    await db.flush()
    return s


# ---------------------------------------------------------------------------
# Transfer: happy path (source → dest)
# ---------------------------------------------------------------------------


async def test_transfer_closes_source_and_creates_dest(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Transfer from source property to dest property: source closed, dest active."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Dest Hotel",
        roomdoo_external_code="DEST-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    source_session = await _make_active_session(
        db, seed_conversation.id, seed_property.id
    )

    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={
            "destination_property_id": dest_prop.id,
            "note": "El huésped solicita cambio de hotel.",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["from_session_id"] == source_session.id
    assert data["destination_property_id"] == dest_prop.id
    assert data["to_session_id"] != source_session.id

    await db.refresh(source_session)
    assert source_session.status == SessionStatus.closed

    dest_result = await db.execute(
        select(AttentionSession).where(
            AttentionSession.id == data["to_session_id"]
        )
    )
    dest_session = dest_result.scalar_one()
    assert dest_session.property_id == dest_prop.id
    assert dest_session.status == SessionStatus.active


# ---------------------------------------------------------------------------
# Transfer: notes appear in timeline
# ---------------------------------------------------------------------------


async def test_transfer_notes_in_timeline(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """After transfer, GET /messages returns both notes with kind='note'."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Note Hotel",
        roomdoo_external_code="NOTE-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    await _make_active_session(db, seed_conversation.id, seed_property.id)

    await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={
            "destination_property_id": dest_prop.id,
            "note": "Traspasar por llegada anticipada.",
        },
        headers=auth_headers,
    )

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    assert response.status_code == 200
    messages = response.json()["messages"]
    notes = [m for m in messages if m["kind"] == "note"]
    assert len(notes) == 2

    contents = {n["content"] for n in notes}
    assert any("Note Hotel" in c for c in contents)
    assert any("bandeja central" not in c or "Test Hotel" in c for c in contents)

    for note in notes:
        assert note["delivery_status"] == "skipped"
        assert note["wa_message_id"] is None
        assert note["sender"] == "system"


# ---------------------------------------------------------------------------
# Transfer: notes scoped to property
# ---------------------------------------------------------------------------


async def test_transfer_notes_scoped_to_property(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Each property only sees messages (including notes) from its own sessions."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Dest Hotel",
        roomdoo_external_code="SCOPE-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    await _make_active_session(db, seed_conversation.id, seed_property.id)

    await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "Traspaso de prueba."},
        headers=auth_headers,
    )

    # Source property sees only the outbound note ("traspasada a Dest Hotel")
    src_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"property_id": seed_property.id},
        headers=auth_headers,
    )
    src_notes = [m for m in src_resp.json()["messages"] if m["kind"] == "note"]
    assert len(src_notes) == 1
    assert "Dest Hotel" in src_notes[0]["content"]

    # Dest property sees only the inbound note ("traspasada desde Test Hotel")
    dst_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )
    dst_notes = [m for m in dst_resp.json()["messages"] if m["kind"] == "note"]
    assert len(dst_notes) == 1
    assert "Test Hotel" in dst_notes[0]["content"]


# ---------------------------------------------------------------------------
# Transfer: from unrouted session
# ---------------------------------------------------------------------------


async def test_transfer_from_unrouted_session(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Unrouted session → transfer to property → unrouted closed, note says 'bandeja central'."""
    unrouted = await _make_unrouted_session(db, seed_conversation.id)

    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={
            "destination_property_id": seed_property.id,
            "note": "Asignar desde bandeja.",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["from_session_id"] == unrouted.id

    await db.refresh(unrouted)
    assert unrouted.status == SessionStatus.closed

    # Note in destination should mention 'bandeja central'
    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in response.json()["messages"] if m["kind"] == "note"]
    dest_note = next(
        (n for n in notes if "bandeja central" in (n["content"] or "")), None
    )
    assert dest_note is not None


# ---------------------------------------------------------------------------
# Transfer: no source session
# ---------------------------------------------------------------------------


async def test_transfer_no_source_session(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """No active session → transfer still creates dest session and dest note."""
    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={
            "destination_property_id": seed_property.id,
            "note": "Sin sesión previa.",
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["from_session_id"] is None

    messages_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in messages_resp.json()["messages"] if m["kind"] == "note"]
    assert len(notes) == 1  # only dest note, no source note


# ---------------------------------------------------------------------------
# Transfer: 422 when already on destination
# ---------------------------------------------------------------------------


async def test_transfer_same_property_raises_422(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """Destination == active session's property → 422."""
    await _make_active_session(db, seed_conversation.id, seed_property.id)

    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={
            "destination_property_id": seed_property.id,
            "note": "No debería funcionar.",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Transfer: 404 cases
# ---------------------------------------------------------------------------


async def test_transfer_unknown_conversation(
    client: AsyncClient,
    auth_headers: dict,
    seed_property,
) -> None:
    response = await client.post(
        "/api/v1/conversations/999999/transfer",
        json={"destination_property_id": seed_property.id, "note": "x"},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_transfer_unknown_property(
    client: AsyncClient,
    auth_headers: dict,
    seed_conversation,
) -> None:
    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": 999999, "note": "x"},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Unrouted sessions: property_id=0 inbox
# ---------------------------------------------------------------------------


async def test_list_unrouted_returns_conversations_with_unrouted_session(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_endpoint,
) -> None:
    """GET /conversations/?property_id=0 returns conversations with active unrouted session."""
    await _insert_message(db, seed_conversation.id, seed_endpoint.id)
    await _make_unrouted_session(db, seed_conversation.id)

    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    ids = [c["id"] for c in data["conversations"]]
    assert seed_conversation.id in ids


# ---------------------------------------------------------------------------
# GET /conversations/{id}/transfer-targets
# ---------------------------------------------------------------------------


async def test_transfer_targets_returns_all_properties_with_channel(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Returns all instance properties that have a WhatsApp channel assigned."""
    from app.models.channel import ChannelEndpoint

    other_endpoint = ChannelEndpoint(
        channel="whatsapp",
        external_code="OTHER_WA_ID",
        access_token="fake",
        account_id="fake",
        verify_token="other-verify",
        mock_mode=True,
        display_number="+34 600 000 088",
    )
    db.add(other_endpoint)
    await db.flush()

    # Property on a different WhatsApp number — should still be included
    prop2 = Property(
        instance_id=seed_instance.id,
        name="Other Channel Hotel",
        roomdoo_external_code="TARGET-002",
        channel_endpoint_id=other_endpoint.id,
    )
    # Property with no channel — should NOT be included
    prop_no_channel = Property(
        instance_id=seed_instance.id,
        name="No Channel Hotel",
        roomdoo_external_code="TARGET-003",
        channel_endpoint_id=None,
    )
    db.add(prop2)
    db.add(prop_no_channel)
    await db.flush()

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/transfer-targets",
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["conversation_id"] == seed_conversation.id
    ids = [p["id"] for p in data["properties"]]
    assert seed_property.id in ids       # same channel
    assert prop2.id in ids               # different channel, still valid
    assert prop_no_channel.id not in ids  # no channel → excluded


async def test_transfer_targets_unknown_conversation(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    response = await client.get(
        "/api/v1/conversations/999999/transfer-targets",
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_list_unrouted_excludes_assigned_conversations(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
) -> None:
    """Conversations with a property session do NOT appear in property_id=0."""
    await _make_active_session(db, seed_conversation.id, seed_property.id)

    response = await client.get(
        "/api/v1/conversations/",
        params={"property_id": 0},
        headers=auth_headers,
    )
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()["conversations"]]
    assert seed_conversation.id not in ids


# ---------------------------------------------------------------------------
# Message scoping: property_id filters ALL messages by session
# ---------------------------------------------------------------------------


async def test_messages_scoped_to_property_includes_regular_messages(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """?property_id scopes ALL messages (not just notes) to the property's sessions."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Second Hotel",
        roomdoo_external_code="SCOPE-MSG-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    src_session = await _make_active_session(db, seed_conversation.id, seed_property.id)
    # Insert a regular message scoped to source session
    src_msg = await _insert_message(
        db, seed_conversation.id, seed_endpoint.id, content="Mensaje del hotel origen"
    )
    src_msg.attention_session_id = src_session.id
    await db.flush()

    # Transfer: source closes, dest session is created with its own note
    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "Traspaso para test."},
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Source property sees its own message + its own outbound note; NOT dest note
    src_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"property_id": seed_property.id},
        headers=auth_headers,
    )
    src_msgs = src_resp.json()["messages"]
    src_contents = [m["content"] for m in src_msgs]
    assert "Mensaje del hotel origen" in src_contents  # regular message visible
    assert any("Second Hotel" in (c or "") for c in src_contents)  # outbound note visible
    assert not any("Test Hotel" in (c or "") for c in src_contents)  # dest inbound note not visible

    # Dest property does NOT see source's regular message
    dst_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )
    dst_msgs = dst_resp.json()["messages"]
    dst_contents = [m["content"] for m in dst_msgs]
    assert "Mensaje del hotel origen" not in dst_contents  # regular msg not visible to dest
    assert any("Test Hotel" in (c or "") for c in dst_contents)  # dest note mentions source


# ---------------------------------------------------------------------------
# needs_attention flag in conversation inbox
# ---------------------------------------------------------------------------


async def test_needs_attention_set_after_transfer(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """After transfer, dest inbox shows needs_attention=True; src shows False."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Attention Hotel",
        roomdoo_external_code="ATTN-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    await _make_active_session(db, seed_conversation.id, seed_property.id)
    await _insert_message(db, seed_conversation.id, seed_endpoint.id)

    await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "Test atención."},
        headers=auth_headers,
    )

    # Destination inbox: needs_attention=True
    dst_resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )
    dst_convs = dst_resp.json()["conversations"]
    dst_item = next(c for c in dst_convs if c["id"] == seed_conversation.id)
    assert dst_item["needs_attention"] is True

    # Source inbox: needs_attention=False (session closed, no pending transfer note)
    src_resp = await client.get(
        "/api/v1/conversations/",
        params={"property_id": seed_property.id},
        headers=auth_headers,
    )
    src_convs = src_resp.json()["conversations"]
    src_item = next(c for c in src_convs if c["id"] == seed_conversation.id)
    assert src_item["needs_attention"] is False


async def test_needs_attention_cleared_after_read(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """PATCH /read clears needs_attention for the destination property."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Read Hotel",
        roomdoo_external_code="READ-001",
        channel_endpoint_id=seed_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    await _make_active_session(db, seed_conversation.id, seed_property.id)
    await _insert_message(db, seed_conversation.id, seed_endpoint.id)

    await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "Test lectura."},
        headers=auth_headers,
    )

    # Confirm flag is set before read
    pre = await client.get(
        "/api/v1/conversations/",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )
    pre_item = next(
        c for c in pre.json()["conversations"]
        if c["id"] == seed_conversation.id
    )
    assert pre_item["needs_attention"] is True

    # Mark as read
    await client.patch(
        f"/api/v1/conversations/{seed_conversation.id}/read",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )

    # Flag should be cleared
    post = await client.get(
        "/api/v1/conversations/",
        params={"property_id": dest_prop.id},
        headers=auth_headers,
    )
    post_item = next(
        c for c in post.json()["conversations"]
        if c["id"] == seed_conversation.id
    )
    assert post_item["needs_attention"] is False


# ---------------------------------------------------------------------------
# Transfer: source session closed only when same WhatsApp channel
# ---------------------------------------------------------------------------


async def test_transfer_same_channel_closes_source(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Source and dest share the same endpoint → source session is closed."""
    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Same WA Hotel",
        roomdoo_external_code="SAMEWA-001",
        channel_endpoint_id=seed_endpoint.id,  # same endpoint as seed_property
    )
    db.add(dest_prop)
    await db.flush()

    source_session = await _make_active_session(
        db, seed_conversation.id, seed_property.id
    )

    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "mismo canal"},
        headers=auth_headers,
    )
    assert response.status_code == 200

    await db.refresh(source_session)
    assert source_session.status == SessionStatus.closed


async def test_transfer_different_channel_keeps_source_active(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_property,
    seed_instance,
    seed_endpoint,
) -> None:
    """Source and dest have different endpoints → source session stays active."""
    from app.models.channel import ChannelEndpoint

    other_endpoint = ChannelEndpoint(
        channel="whatsapp",
        external_code="OTHER_WA_DIFF",
        access_token="fake",
        account_id="fake",
        verify_token="diff-verify",
        mock_mode=True,
        display_number="+34 600 000 099",
    )
    db.add(other_endpoint)
    await db.flush()

    dest_prop = Property(
        instance_id=seed_instance.id,
        name="Diff WA Hotel",
        roomdoo_external_code="DIFFWA-001",
        channel_endpoint_id=other_endpoint.id,
    )
    db.add(dest_prop)
    await db.flush()

    source_session = await _make_active_session(
        db, seed_conversation.id, seed_property.id
    )

    response = await client.post(
        f"/api/v1/conversations/{seed_conversation.id}/transfer",
        json={"destination_property_id": dest_prop.id, "note": "canal distinto"},
        headers=auth_headers,
    )
    assert response.status_code == 200

    await db.refresh(source_session)
    assert source_session.status == SessionStatus.active  # kept open
