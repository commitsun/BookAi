"""
Unit tests for app/services/translation_service.py.

The service has three public functions:
  - get_content_in_language: resolve translation with cache fallback
  - store_translation: persist externally-provided translation
  - _translate (private): placeholder that raises TranslationNotAvailable

All tests use AsyncMock/MagicMock to avoid hitting the database.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.message import Message
from app.services.translation_service import (
    TranslationNotAvailable,
    get_content_in_language,
    store_translation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _message(content: str = "Hola", lang: str = "es", msg_id: int = 1) -> MagicMock:
    m = MagicMock(spec=Message)
    m.id = msg_id
    m.content = content
    m.content_language = lang
    return m


# ---------------------------------------------------------------------------
# get_content_in_language — same language
# ---------------------------------------------------------------------------


async def test_same_language_returns_original_without_db_hit():
    """If requested language matches content_language, return original immediately."""
    msg = _message(content="Hola mundo", lang="es")
    db = AsyncMock()

    with patch("app.services.translation_service.message_translation_repo") as repo:
        result = await get_content_in_language(db, msg, "es")

    repo.find.assert_not_called()
    assert result == "Hola mundo"


async def test_same_language_empty_content_returns_empty_string():
    """content=None with matching language → empty string (not None)."""
    msg = _message(lang="es")
    msg.content = None

    result = await get_content_in_language(AsyncMock(), msg, "es")

    assert result == ""


# ---------------------------------------------------------------------------
# get_content_in_language — cache hit
# ---------------------------------------------------------------------------


async def test_cached_translation_returned_without_calling_translate():
    """If a cached translation exists, return it without raising or translating."""
    msg = _message(content="Hola", lang="es")
    cached = MagicMock()
    cached.content = "Hello"

    with patch("app.services.translation_service.message_translation_repo") as repo:
        repo.find = AsyncMock(return_value=cached)

        result = await get_content_in_language(AsyncMock(), msg, "en")

    assert result == "Hello"


# ---------------------------------------------------------------------------
# get_content_in_language — cache miss → TranslationNotAvailable
# ---------------------------------------------------------------------------


async def test_no_cache_raises_translation_not_available():
    """Phase 1: no cache + no AI → raises TranslationNotAvailable."""
    msg = _message(content="Hola", lang="es")

    with patch("app.services.translation_service.message_translation_repo") as repo:
        repo.find = AsyncMock(return_value=None)

        with pytest.raises(TranslationNotAvailable):
            await get_content_in_language(AsyncMock(), msg, "en")


async def test_translation_not_available_message_contains_message_id():
    """The exception message includes the message id for debugging."""
    msg = _message(msg_id=42, lang="es")

    with patch("app.services.translation_service.message_translation_repo") as repo:
        repo.find = AsyncMock(return_value=None)

        with pytest.raises(TranslationNotAvailable, match="42"):
            await get_content_in_language(AsyncMock(), msg, "fr")


# ---------------------------------------------------------------------------
# store_translation
# ---------------------------------------------------------------------------


async def test_store_translation_creates_when_no_existing():
    """store_translation creates a new entry when none exists."""
    with patch("app.services.translation_service.message_translation_repo") as repo:
        repo.find = AsyncMock(return_value=None)
        repo.create = AsyncMock()
        db = AsyncMock()

        await store_translation(db, message_id=1, language="en", content="Hello")

    repo.create.assert_called_once_with(db, 1, "en", "Hello")


async def test_store_translation_overwrites_existing():
    """store_translation overwrites content when a translation already exists."""
    existing = MagicMock()
    existing.content = "Old translation"

    with patch("app.services.translation_service.message_translation_repo") as repo:
        repo.find = AsyncMock(return_value=existing)
        repo.create = AsyncMock()
        db = AsyncMock()

        await store_translation(db, message_id=1, language="en", content="New translation")

    repo.create.assert_not_called()
    assert existing.content == "New translation"
