"""
Integration tests for the Meta webhook verification endpoint.

GET /webhook/whatsapp?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import TEST_VERIFY_TOKEN


async def test_verify_correct_token(
    client: AsyncClient,
    db: AsyncSession,
    seed_endpoint,
) -> None:
    """Valid verify_token → 200 + challenge echoed back."""
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "test_challenge_string",
        },
    )
    assert response.status_code == 200
    assert response.text == "test_challenge_string"


async def test_verify_wrong_token(
    client: AsyncClient,
    seed_endpoint,
) -> None:
    """Unknown verify_token → 403."""
    response = await client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "completely-wrong-token",
            "hub.challenge": "irrelevant",
        },
    )
    assert response.status_code == 403
