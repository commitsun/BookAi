"""
Service for processing folio lifecycle events from Odoo.

Creates internal notes in the active sessions linked to the folio.
Notes are rendered in the instance's default language and stored with
template_code for future lazy translation.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instance import Instance
from app.models.message import MessageSender
from app.repositories import folio_repo, message_repo, session_repo
from app.schemas.folio import (
    FolioEventRequest,
    FolioEventType,
    ModificationType,
)
from app.services.note_templates import render_note

log = logging.getLogger("folio_events")


def _resolve_template_key(event_type: FolioEventType, data: dict) -> str:
    if event_type == FolioEventType.folio_modified:
        return f"folio_modified.{data['modification_type']}"
    return event_type.value


def _build_context(event_type: FolioEventType, data: dict) -> dict:
    if event_type == FolioEventType.payment_registered:
        return {"amount": data["amount"], "currency": data["currency"]}
    if event_type == FolioEventType.precheckin_completed:
        return {
            "guest_name": data["guest_name"],
            "room_number": data["room_number"],
        }
    if event_type == FolioEventType.status_changed:
        return {"new_status": data["new_status"]}
    if (
        event_type == FolioEventType.folio_modified
        and data.get("modification_type") == ModificationType.dates_changed
    ):
        return {
            "checkin_date": data.get("checkin_date", ""),
            "checkout_date": data.get("checkout_date", ""),
        }
    return {}


async def process_folio_event(
    db: AsyncSession,
    instance: Instance,
    folio_code: str,
    request: FolioEventRequest,
) -> int:
    """Create notes for a folio lifecycle event in all active linked sessions.

    Returns the number of notes created (0 if no active sessions, no error).
    Raises HTTPException 404 if the folio does not exist.
    """
    folio = await folio_repo.find_by_code(db, folio_code)
    if folio is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Folio '{folio_code}' not found",
        )

    sessions = await session_repo.find_active_for_folio(db, folio.id)
    if not sessions:
        log.info("No active sessions for folio %s — note skipped", folio_code)
        return 0

    template_key = _resolve_template_key(request.event_type, request.data)
    context = _build_context(request.event_type, request.data)
    lang = instance.default_language
    content = render_note(template_key, lang, **context)

    for session in sessions:
        await message_repo.create_note(
            db,
            conversation_id=session.conversation_id,
            attention_session_id=session.id,
            sender=MessageSender.system,
            content=content,
            content_language=lang,
            template_code=template_key,
            template_payload=context,
        )

    log.info(
        "Folio event %s for %s → %d note(s) created",
        request.event_type,
        folio_code,
        len(sessions),
    )
    return len(sessions)
