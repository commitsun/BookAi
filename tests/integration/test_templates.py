"""
Integration tests for Flow 1: POST /api/v1/whatsapp/send-template.

The channel_endpoint has mock_mode=True, so no real Meta calls are made.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.session import AttentionSession
from app.models.template import (
    TemplateTranslationProperty,
    WhatsAppTemplate,
    WhatsAppTemplateTranslation,
)


# ---------------------------------------------------------------------------
# Fixtures — template seed data
# ---------------------------------------------------------------------------


async def _seed_template(db: AsyncSession, prop_id: int) -> WhatsAppTemplate:
    """Insert a WhatsAppTemplate + Spanish translation linked to prop_id."""
    tmpl = WhatsAppTemplate(code="test_welcome")
    db.add(tmpl)
    await db.flush()

    translation = WhatsAppTemplateTranslation(
        template_id=tmpl.id,
        whatsapp_name="test_welcome_es",
        language="es",
        components=[],
        active=True,
    )
    db.add(translation)
    await db.flush()

    db.add(TemplateTranslationProperty(translation_id=translation.id, property_id=prop_id))
    await db.flush()

    return tmpl


def _template_request(
    hotel_code: str,
    phone: str,
    template_code: str = "test_welcome",
    idempotency_key: str | None = None,
) -> dict:
    body: dict = {
        "source": {
            "hotel": {"external_code": hotel_code},
        },
        "recipient": {
            "phone": phone,
            "country": "ES",
            "display_name": "Test Guest",
        },
        "template": {
            "code": template_code,
            "language": "es",
            "components": [],
        },
    }
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    return body


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_send_template_creates_session(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_property,
    seed_endpoint,
) -> None:
    """POST /send-template → AttentionSession created for the property."""
    await _seed_template(db, seed_property.id)

    response = await client.post(
        "/api/v1/whatsapp/send-template",
        json=_template_request(
            hotel_code=seed_property.roomdoo_external_code,
            phone="+34699000099",
        ),
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"

    result = await db.execute(
        select(AttentionSession).where(
            AttentionSession.conversation_id == data["conversation_id"],
            AttentionSession.property_id == seed_property.id,
        )
    )
    session = result.scalar_one()
    assert session is not None


async def test_send_template_idempotent(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_property,
    seed_endpoint,
) -> None:
    """Same idempotency_key twice → 2x OK, only 1 message in DB."""
    await _seed_template(db, seed_property.id)
    key = "idempotency-test-key-001"

    r1 = await client.post(
        "/api/v1/whatsapp/send-template",
        json=_template_request(
            hotel_code=seed_property.roomdoo_external_code,
            phone="+34699000098",
            idempotency_key=key,
        ),
        headers=auth_headers,
    )
    assert r1.status_code == 200
    assert r1.json()["idempotent"] is False

    r2 = await client.post(
        "/api/v1/whatsapp/send-template",
        json=_template_request(
            hotel_code=seed_property.roomdoo_external_code,
            phone="+34699000098",
            idempotency_key=key,
        ),
        headers=auth_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["idempotent"] is True
    assert r2.json()["message_id"] == r1.json()["message_id"]

    result = await db.execute(
        select(Message).where(Message.idempotency_key == key)
    )
    messages = result.scalars().all()
    assert len(messages) == 1


async def test_send_template_unknown_hotel(
    client: AsyncClient,
    auth_headers: dict,
    seed_instance,
) -> None:
    """Unknown hotel external_code → 404."""
    response = await client.post(
        "/api/v1/whatsapp/send-template",
        json=_template_request(
            hotel_code="NONEXISTENT-HOTEL",
            phone="+34699000097",
        ),
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_send_template_unknown_template_code(
    client: AsyncClient,
    auth_headers: dict,
    db: AsyncSession,
    seed_property,
    seed_endpoint,
) -> None:
    """Template code not registered → 404."""
    # No template seeded, so the lookup will fail
    response = await client.post(
        "/api/v1/whatsapp/send-template",
        json=_template_request(
            hotel_code=seed_property.roomdoo_external_code,
            phone="+34699000096",
            template_code="nonexistent_template",
        ),
        headers=auth_headers,
    )
    assert response.status_code == 404
