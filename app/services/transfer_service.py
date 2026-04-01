"""
Transfer service — moves a conversation from one property session to another.

Called by POST /api/v1/conversations/{id}/transfer.

Steps (all inside a single transaction):
  1. Validate destination property belongs to the same instance.
  2. Find the current active session (regular or unrouted).
  3. Guard: raise 422 if already assigned to the destination property.
  4. Create a transfer note on the source session (if one exists).
  5. Close the source session.
  6. Create / get the destination session.
  7. Create a transfer note on the destination session.
  8. Commit.
  9. Emit Socket.IO events (fire-and-forget).
"""

from __future__ import annotations

import logging

import socketio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageSender
from app.models.instance import Instance
from app.services.note_templates import render_note, unrouted_label
from app.repositories import (
    conversation_repo,
    instance_repo,
    message_repo,
    session_repo,
)
from app.realtime.events import (
    EVENT_CONVERSATION_UPDATED,
    build_conversation_payload,
)

log = logging.getLogger("transfer_service")


async def transfer_conversation(
    db: AsyncSession,
    sio: socketio.AsyncServer,
    instance: Instance,
    conversation_id: int,
    destination_property_id: int,
    note_text: str,
) -> dict:
    """
    Transfer a conversation to a different property within the same instance.

    Returns a dict with from_session_id, to_session_id, destination_property_id.
    Raises HTTPException on validation errors.
    """
    # --- Validate conversation ---
    conversation = await conversation_repo.find_by_id(db, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # --- Validate destination property ---
    dest_prop = await instance_repo.find_property_by_id(
        db, destination_property_id, instance.id
    )
    if dest_prop is None:
        raise HTTPException(
            status_code=404,
            detail=f"Property {destination_property_id} not found in this instance",
        )

    # --- Find current active session ---
    active_sessions = await session_repo.find_active_for_conversation(
        db, conversation_id
    )

    # Pick source session: any active session that is NOT the destination
    source_session = None
    for s in active_sessions:
        if s.property_id == destination_property_id:
            raise HTTPException(
                status_code=422,
                detail="Conversation is already assigned to this property",
            )
        source_session = s  # take the first (normally there is at most one)

    # --- Load source property name and channel for note text / close decision ---
    source_name: str | None = None
    src_prop = None
    if source_session is not None and source_session.property_id is not None:
        src_prop = await instance_repo.find_property_by_id(
            db, source_session.property_id, instance.id
        )
        source_name = src_prop.name if src_prop else None

    # Close source session unless source has a different WhatsApp channel than
    # the destination. When channels differ both properties can still operate
    # independently on their own number.
    # Unrouted sessions (src_prop=None) and properties without a channel
    # assigned are always closed — they have no channel to keep active.
    different_channel = (
        src_prop is not None
        and src_prop.channel_endpoint_id is not None
        and src_prop.channel_endpoint_id != dest_prop.channel_endpoint_id
    )
    same_channel = not different_channel

    lang = instance.default_language

    # --- Note on source session ---
    if source_session is not None:
        await message_repo.create_note(
            db,
            conversation_id=conversation_id,
            attention_session_id=source_session.id,
            sender=MessageSender.system,
            content=render_note(
                "transfer.outgoing", lang,
                dest_name=dest_prop.name,
                agent_note=note_text,
            ),
            template_code="transfer.outgoing",
        )
        if same_channel:
            await session_repo.close_sessions(db, [source_session])

    # --- Create / get destination session ---
    dest_session, _ = await session_repo.get_or_create_active(
        db, conversation_id, destination_property_id
    )

    # --- Note on destination session ---
    origin_label = source_name or unrouted_label(lang)
    await message_repo.create_note(
        db,
        conversation_id=conversation_id,
        attention_session_id=dest_session.id,
        sender=MessageSender.system,
        content=render_note(
            "transfer.incoming", lang,
            origin_name=origin_label,
            agent_note=note_text,
        ),
        template_code="transfer.incoming",
    )

    await db.commit()

    # --- Socket.IO events (best-effort) ---
    await _emit_transfer_events(
        db=db,
        sio=sio,
        conversation=conversation,
        source_session=source_session,
        dest_session=dest_session,
        destination_property_id=destination_property_id,
    )

    return {
        "conversation_id": conversation_id,
        "from_session_id": (
            source_session.id if source_session else None
        ),
        "to_session_id": dest_session.id,
        "destination_property_id": destination_property_id,
    }


async def _emit_transfer_events(
    db: AsyncSession,
    sio: socketio.AsyncServer,
    conversation,
    source_session,
    dest_session,
    destination_property_id: int,
) -> None:
    try:
        contact = conversation.contact

        async def _emit_to(
            room: str, property_id: int | None, needs_attention: bool = False
        ) -> None:
            pid = property_id or 0
            if pid != 0:
                counts = await conversation_repo.get_unread_counts(
                    db, [conversation.id], pid
                )
                unread = counts.get(conversation.id, 0)
            else:
                unread = 0
            payload = build_conversation_payload(
                conversation,
                contact,
                unread_count=unread,
                needs_attention=needs_attention,
            )
            await sio.emit(EVENT_CONVERSATION_UPDATED, payload, room=room)

        # Notify source property (or admin inbox if unrouted)
        if source_session is not None:
            if source_session.property_id is not None:
                await _emit_to(
                    f"property:{source_session.property_id}",
                    source_session.property_id,
                )
            else:
                await _emit_to("property:0", None)

        # Notify destination property — always needs_attention=True since the
        # transfer note was just created and has not been read yet.
        await _emit_to(
            f"property:{destination_property_id}",
            destination_property_id,
            needs_attention=True,
        )

    except Exception as exc:
        log.warning("Socket.IO emit failed on transfer: %s", exc)
