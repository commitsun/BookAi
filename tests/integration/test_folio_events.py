"""
Integration tests for POST /api/v1/folios/{code}/events.

Covers:
- Event creates note (kind=note) in active sessions linked to the folio.
- template_code is stored for future lazy translation.
- No active sessions → notes_created=0, 200 OK.
- Various event types: folio_cancelled, payment_registered, precheckin_completed,
  status_changed, folio_modified.dates_changed.
- 404 on unknown folio.
- 401 on missing token.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.folio import Folio, SessionFolio
from app.models.session import AttentionSession, SessionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_folio(db: AsyncSession, code: str = "TEST-FOLIO-001") -> Folio:
    folio = Folio(odoo_external_code=code)
    db.add(folio)
    await db.flush()
    return folio


async def _link_folio_to_session(
    db: AsyncSession, session_id: int, folio_id: int
) -> None:
    db.add(SessionFolio(session_id=session_id, folio_id=folio_id))
    await db.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_folio_created_event_creates_note(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    """folio_created event → note with kind=note and correct template_code."""
    folio = await _make_folio(db)
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["folio_code"] == folio.odoo_external_code
    assert data["event_type"] == "folio_created"
    assert data["notes_created"] == 1

    # Verify note appears in messages
    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    messages = msgs_resp.json()["messages"]
    notes = [m for m in messages if m["kind"] == "note"]
    assert len(notes) == 1
    assert notes[0]["template_code"] == "folio_created"
    assert notes[0]["sender"] == "system"
    assert notes[0]["delivery_status"] == "skipped"


async def test_no_active_session_returns_zero(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
) -> None:
    """Folio with no active sessions → notes_created=0, 200 OK."""
    folio = await _make_folio(db, code="FOLIO-NO-SESSION")

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["notes_created"] == 0


async def test_folio_cancelled_event(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    folio = await _make_folio(db, code="FOLIO-CANCEL-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={"event_type": "folio_cancelled", "data": {}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["notes_created"] == 1

    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in msgs_resp.json()["messages"] if m["kind"] == "note"]
    assert any("cancelad" in (n["content"] or "").lower() for n in notes)


async def test_payment_registered_event(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    folio = await _make_folio(db, code="FOLIO-PAYMENT-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={
            "event_type": "payment_registered",
            "data": {"amount": "250.00", "currency": "EUR"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["notes_created"] == 1

    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in msgs_resp.json()["messages"] if m["kind"] == "note"]
    assert any("250.00" in (n["content"] or "") and "EUR" in (n["content"] or "") for n in notes)


async def test_precheckin_completed_event(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    folio = await _make_folio(db, code="FOLIO-PRECHECKIN-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={
            "event_type": "precheckin_completed",
            "data": {"guest_name": "Ana García", "room_number": "302"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 200

    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in msgs_resp.json()["messages"] if m["kind"] == "note"]
    content = " ".join(n["content"] or "" for n in notes)
    assert "Ana García" in content
    assert "302" in content


async def test_status_changed_event(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    folio = await _make_folio(db, code="FOLIO-STATUS-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={
            "event_type": "status_changed",
            "data": {"new_status": "onboard"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 200

    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in msgs_resp.json()["messages"] if m["kind"] == "note"]
    assert any("onboard" in (n["content"] or "") for n in notes)


async def test_folio_modified_dates_changed(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    folio = await _make_folio(db, code="FOLIO-DATES-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    response = await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={
            "event_type": "folio_modified",
            "data": {
                "modification_type": "dates_changed",
                "checkin_date": "2026-04-01",
                "checkout_date": "2026-04-05",
            },
        },
        headers=auth_headers,
    )
    assert response.status_code == 200

    msgs_resp = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        headers=auth_headers,
    )
    notes = [m for m in msgs_resp.json()["messages"] if m["kind"] == "note"]
    assert any("2026-04-01" in (n["content"] or "") for n in notes)
    assert any(n["template_code"] == "folio_modified.dates_changed" for n in notes)


async def test_note_translation_generated_lazily(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    """GET /messages?language=gl on a note renders from template and caches the translation."""
    folio = await _make_folio(db, code="FOLIO-LAZY-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )

    # First request — triggers lazy generation
    r1 = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"language": "gl"},
        headers=auth_headers,
    )
    assert r1.status_code == 200
    notes = [m for m in r1.json()["messages"] if m["kind"] == "note"]
    assert len(notes) == 1
    assert notes[0]["is_translated"] is True
    assert notes[0]["content_language"] == "gl"
    assert notes[0]["content"] != ""

    # Second request — served from MessageTranslation cache
    r2 = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"language": "gl"},
        headers=auth_headers,
    )
    note2 = next(m for m in r2.json()["messages"] if m["kind"] == "note")
    assert note2["content"] == notes[0]["content"]


async def test_note_unsupported_language_returns_original(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    """Requesting a note in an unsupported language returns the original, is_translated=False."""
    folio = await _make_folio(db, code="FOLIO-DE-001")
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    await client.post(
        f"/api/v1/folios/{folio.odoo_external_code}/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )

    response = await client.get(
        f"/api/v1/conversations/{seed_conversation.id}/messages",
        params={"language": "de"},
        headers=auth_headers,
    )
    notes = [m for m in response.json()["messages"] if m["kind"] == "note"]
    assert notes[0]["is_translated"] is False
    assert notes[0]["content_language"] == "es"


async def test_unknown_folio_returns_404(
    client: AsyncClient,
    auth_headers: dict,
) -> None:
    response = await client.post(
        "/api/v1/folios/NONEXISTENT-FOLIO/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_unauthorized_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/folios/ANY-FOLIO/events",
        json={"event_type": "folio_created", "data": {}},
    )
    assert response.status_code == 401


async def test_slash_in_code_normalized_in_url(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_conversation,
    seed_attention_session,
) -> None:
    """Folio codes with '/' in the URL path are stored and matched as-is."""
    raw_code = "206/26/TEST"
    folio = await _make_folio(db, code=raw_code)
    await _link_folio_to_session(db, seed_attention_session.id, folio.id)

    # Call the API with the raw code containing slashes in the URL path
    response = await client.post(
        f"/api/v1/folios/{raw_code}/events",
        json={"event_type": "folio_created", "data": {}},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["folio_code"] == raw_code
    assert data["notes_created"] == 1
