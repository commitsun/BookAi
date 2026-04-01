"""
Translation lookup and cache service.

Current behaviour (Phase 1 — no AI):
  - If the requested language matches the message's original language → return original.
  - If a cached translation exists → return it.
  - Otherwise → raise TranslationNotAvailable. The caller decides how to handle it
    (return original, return a placeholder, or surface a 422).

When the AI translator is integrated, replace _translate() with the real implementation.
The rest of the service stays identical: the AI result is persisted and served from
cache on subsequent requests.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.repositories import message_translation_repo

log = logging.getLogger("translation_service")


class TranslationNotAvailable(Exception):
    """Raised when no translation exists and automatic translation is not yet implemented."""


async def get_content_in_language(
    db: AsyncSession,
    message: Message,
    language: str,
) -> str:
    """
    Return the message content in the requested language.

    Resolution order:
      1. Original content matches → return as-is.
      2. Cached translation exists → return it.
      3. Translate, persist, return. (Phase 1: raises TranslationNotAvailable)
    """
    # 1. Already in the requested language
    if message.content_language and message.content_language == language:
        return message.content or ""

    # 2. Check cache
    cached = await message_translation_repo.find(db, message.id, language)
    if cached:
        return cached.content

    # 3. Translate
    content = await _translate(message, language)
    await message_translation_repo.create(db, message.id, language, content)
    await db.flush()
    log.info(
        "translation created message_id=%s lang=%s", message.id, language
    )
    return content


async def store_translation(
    db: AsyncSession,
    message_id: int,
    language: str,
    content: str,
) -> None:
    """
    Persist a translation provided externally (e.g. from the AI layer or a human editor).
    Overwrites any existing translation for the same (message_id, language).
    """
    existing = await message_translation_repo.find(db, message_id, language)
    if existing:
        existing.content = content
    else:
        await message_translation_repo.create(db, message_id, language, content)
    await db.flush()


async def _translate(message: Message, target_language: str) -> str:
    """
    Placeholder. Replace with AI/external translation call in a future phase.
    """
    raise TranslationNotAvailable(
        f"No translation available for message {message.id} "
        f"in language '{target_language}'. "
        "Automatic translation is not yet implemented."
    )
