"""
GET   /api/v1/conversations              — inbox listing for a property
GET   /api/v1/conversations/search                   — search conversations
GET   /api/v1/conversations/{conversation_id}/messages — message history
PATCH /api/v1/conversations/{conversation_id}/read  — mark conversation as read
POST  /api/v1/conversations/{conversation_id}/assign   — assign to a property
POST  /api/v1/conversations/{conversation_id}/transfer — transfer to another property
"""

import logging

import socketio
from fastapi import (
    APIRouter, Depends, HTTPException, Query, status as http_status,
)
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import get_db, get_instance, get_sio
from app.models.conversation import Conversation
from app.models.folio import FolioStatus
from app.models.contact import Contact
from app.models.folio import Folio, SessionFolio
from app.models.instance import Instance
from app.models.message import Message, MessageKind
from app.models.session import AttentionSession
from app.repositories import conversation_repo, instance_repo, message_translation_repo, session_repo
from app.realtime.events import EVENT_CONVERSATION_UPDATED, build_conversation_payload
from app.schemas.conversation import (
    AssignConversationRequest,
    AssignConversationResponse,
    ContactSummary,
    ConversationListItem,
    ConversationsListResponse,
    LastMessageSummary,
    MessageOut,
    MessagesResponse,
    TransferConversationRequest,
    TransferConversationResponse,
    TransferTargetProperty,
    TransferTargetsResponse,
)
from app.services import transfer_service
from app.services.note_templates import SUPPORTED_LANGUAGES, render_note

log = logging.getLogger("conversations")
router = APIRouter(prefix="/conversations", tags=["conversations"])

# ---------------------------------------------------------------------------
# GET /conversations
# ---------------------------------------------------------------------------

_LIST_SUMMARY = "List conversations for a property"

_LIST_DESCRIPTION = """
Returns the conversation inbox for a property, ordered by most recent message
first. Each item includes the contact, a summary of the last message, and the
unread message count for the requesting property.

Pass `property_id=0` to retrieve conversations that have not yet been claimed
by any property (no AttentionSession).

### Unread counts

`unread_count` reflects inbound messages received after the property's last
call to `PATCH /conversations/{id}/read`. A NULL read cursor (conversation
never marked as read) counts all inbound messages as unread.

### Pagination

Pass `limit` (default 50, max 200). Cursor pagination is not yet implemented;
to load all conversations fetch with a high limit or increase it as needed.

### Real-time updates

Subscribe to Socket.IO `conversation.created` and `conversation.updated` events
on the `property:{id}` room to keep the inbox list up to date without polling.
Both events carry the current `unread_count` for the subscribing property.
"""


@router.get(
    "/",
    response_model=ConversationsListResponse,
    summary=_LIST_SUMMARY,
    description=_LIST_DESCRIPTION,
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Property not found or not accessible"},
    },
)
async def list_conversations(
    property_id: int = Query(
        ...,
        description=(
            "ID of the property (hotel). "
            "Use 0 for unrouted conversations."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> ConversationsListResponse:
    if property_id == 0:
        conversations = await conversation_repo.list_unrouted(db, limit)
    else:
        conversations = await conversation_repo.list_for_property(
            db, property_id, limit
        )

    items = await _build_items_with_last_message(
        db, conversations, property_id
    )
    return ConversationsListResponse(
        property_id=property_id, conversations=items
    )


# ---------------------------------------------------------------------------
# GET /conversations/search
# ---------------------------------------------------------------------------

_SEARCH_SUMMARY = "Search conversations for a property"

_SEARCH_DESCRIPTION = """
Returns conversations matching the search criteria, ordered by most recent
message first. Each item includes `unread_count` for the requesting property,
identical in meaning to the inbox listing endpoint.

### Search parameters

- `q` — free-text match against guest display name **or** folio code (ILIKE).
  Accent-insensitive: "garcia" matches "García".
- `status` — exact match on the cached reservation status:
  `draft`, `confirm`, `onboard`, `done`, `cancel`

At least one of `q` or `status` must be provided.

### Data freshness

Folio fields (`status`, `checkin_date`, `checkout_date`) reflect the last
push from Roomdoo via `PATCH /api/v1/folios/{code}`. They may lag behind
Odoo by up to the interval between Roomdoo sync calls.
"""


@router.get(
    "/search",
    response_model=ConversationsListResponse,
    summary=_SEARCH_SUMMARY,
    description=_SEARCH_DESCRIPTION,
    responses={
        400: {"description": "At least one search parameter required"},
        401: {"description": "Missing or invalid Bearer token"},
    },
)
async def search_conversations(
    property_id: int = Query(..., description="ID of the property (hotel)"),
    q: str | None = Query(
        default=None,
        description="Match guest name or folio code (case-insensitive)",
        examples=["Garcia"],
    ),
    status: FolioStatus | None = Query(
        default=None,
        description=(
            "Reservation status: `draft`, `confirm`, "
            "`onboard`, `done`, `cancel`"
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> ConversationsListResponse:
    if not q and not status:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one of: q, status",
        )

    last_msg_at = (
        select(func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id)
        .correlate(Conversation)
        .scalar_subquery()
    )

    stmt = (
        select(Conversation)
        .join(Contact, Contact.id == Conversation.contact_id)
        .join(
            AttentionSession,
            AttentionSession.conversation_id == Conversation.id,
        )
        .outerjoin(
            SessionFolio, SessionFolio.session_id == AttentionSession.id
        )
        .outerjoin(Folio, Folio.id == SessionFolio.folio_id)
        .where(AttentionSession.property_id == property_id)
        .options(selectinload(Conversation.contact))
        .group_by(Conversation.id)
        .order_by(last_msg_at.desc().nullslast())
        .limit(limit)
    )

    if q:
        # unaccent() on both sides so "garcia" matches "García"
        pattern = f"%{q}%"
        unaccent = func.unaccent
        stmt = stmt.where(
            or_(
                unaccent(Contact.display_name).ilike(unaccent(pattern)),
                Folio.odoo_external_code.ilike(pattern),
            )
        )
    if status:
        stmt = stmt.where(Folio.status == status)

    result = await db.execute(stmt)
    conversations = list(result.scalars().unique().all())

    items = await _build_items_with_last_message(
        db, conversations, property_id
    )
    return ConversationsListResponse(
        property_id=property_id, conversations=items
    )


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}/messages
# ---------------------------------------------------------------------------

_SUMMARY = "List messages for a conversation"

_DESCRIPTION = """
Returns the message history for a conversation in reverse-chronological
order, paginated by cursor (`before_id`).

### Language / translation

Supply `language` (BCP-47 tag, e.g. `es`, `zh`, `en`) to request translated
content:

| Scenario | `content` returned | `is_translated` |
|---|---|---|
| Original language matches | original `content` | `false` |
| Cached translation exists | translated content | `true` |
| No translation cached yet | original `content` | `false` |

When `is_translated=false` and `language` was requested, the client should
trigger a translation request (endpoint TBD in a future phase).

### Pagination

Use `before_id` (the `id` of the oldest message in the current page) to
fetch the previous page. Default page size is 50, maximum 200.
"""

_RESPONSES = {
    401: {"description": "Missing or invalid Bearer token"},
    404: {"description": "Conversation not found"},
}


@router.get(
    "/{conversation_id}/messages",
    response_model=MessagesResponse,
    summary=_SUMMARY,
    description=_DESCRIPTION,
    responses=_RESPONSES,
)
async def get_messages(
    conversation_id: int,
    language: str | None = Query(
        default=None,
        description="BCP-47 target language (e.g. 'es', 'zh', 'en')",
        examples=["es"],
    ),
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(
        default=None,
        description="Cursor: return messages with id < before_id",
    ),
    property_id: int | None = Query(
        default=None,
        description=(
            "When provided, notes are filtered to only those belonging to "
            "sessions of this property. Regular messages are always included. "
            "Use 0 for the unrouted inbox."
        ),
    ),
    _instance: Instance = Depends(get_instance),  # auth side-effect only
    db: AsyncSession = Depends(get_db),
) -> MessagesResponse:
    query = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.where(Message.id < before_id)

    if property_id is not None:
        # Each property sees only messages from its own sessions.
        # property_id=0 means the unrouted inbox (sessions with property_id IS NULL).
        effective_property_id = None if property_id == 0 else property_id
        property_sessions_sq = (
            select(AttentionSession.id).where(
                AttentionSession.property_id == effective_property_id
            )
        )
        query = query.where(
            Message.attention_session_id.in_(property_sessions_sq)
        )

    result = await db.execute(query)
    messages = list(result.scalars().all())
    messages.reverse()

    out: list[MessageOut] = []
    translation_written = False
    for msg in messages:
        content = msg.content
        is_translated = False

        if language and msg.content_language != language and msg.content:
            cached = await message_translation_repo.find(db, msg.id, language)
            if cached:
                content = cached.content
                is_translated = True
            elif (
                msg.kind == MessageKind.note
                and msg.template_code is not None
                and language in SUPPORTED_LANGUAGES
            ):
                try:
                    ctx = msg.template_payload or {}
                    translated = render_note(msg.template_code, language, **ctx)
                    await message_translation_repo.create(db, msg.id, language, translated)
                    translation_written = True
                    content = translated
                    is_translated = True
                except KeyError:
                    pass  # unknown template key — return original

        out.append(MessageOut(
            id=msg.id,
            conversation_id=msg.conversation_id,
            channel_endpoint_id=msg.channel_endpoint_id,
            kind=msg.kind.value,
            direction=msg.direction.value,
            sender=msg.sender.value,
            content=content,
            content_language=language if is_translated else msg.content_language,  # noqa: E501
            is_translated=is_translated,
            agent_user_id=msg.agent_user_id,
            agent_display_name=msg.agent_display_name,
            wa_message_id=msg.wa_message_id,
            wa_message_type=msg.wa_message_type,
            delivery_status=msg.delivery_status.value,
            routing_status=(
                msg.routing_status.value if msg.routing_status else None
            ),
            template_code=msg.template_code,
            created_at=msg.created_at.isoformat() if msg.created_at else "",
        ))

    if translation_written:
        await db.commit()

    return MessagesResponse(
        conversation_id=conversation_id,
        language=language,
        messages=out,
    )


# ---------------------------------------------------------------------------
# PATCH /conversations/{conversation_id}/read
# ---------------------------------------------------------------------------


@router.patch(
    "/{conversation_id}/read",
    status_code=204,
    summary="Mark conversation as read",
    responses={
        401: {"description": "Missing or invalid Bearer token"},
    },
)
async def mark_conversation_read(
    conversation_id: int,
    property_id: int = Query(
        ..., description="Property marking this conversation as read"
    ),
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> None:
    await conversation_repo.mark_read(db, conversation_id, property_id)
    await db.commit()


# ---------------------------------------------------------------------------
# POST /conversations/{conversation_id}/assign
# ---------------------------------------------------------------------------


@router.post(
    "/{conversation_id}/assign",
    response_model=AssignConversationResponse,
    summary="Assign a conversation to a property",
    description=(
        "Creates an AttentionSession linking the conversation to the given property. "
        "Use this to manually route conversations that arrived as `unassigned` or "
        "`ambiguous` (visible in `property:0`). "
        "Idempotent: calling twice with the same property returns `created=false`."
    ),
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Conversation or property not found"},
    },
)
async def assign_conversation(
    conversation_id: int,
    body: AssignConversationRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> AssignConversationResponse:
    conversation = await conversation_repo.find_by_id(db, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    prop = await instance_repo.find_property_by_id(db, body.property_id, instance.id)
    if prop is None:
        raise HTTPException(
            status_code=404,
            detail=f"Property {body.property_id} not found in this instance",
        )

    attention_session, created = await session_repo.get_or_create_active(
        db, conversation_id, body.property_id
    )
    await db.commit()

    try:
        last_msg_result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.id.desc())
            .limit(1)
        )
        last_msg = last_msg_result.scalar_one_or_none()
        unread_counts = await conversation_repo.get_unread_counts(
            db, [conversation_id], body.property_id
        )
        await sio.emit(
            EVENT_CONVERSATION_UPDATED,
            build_conversation_payload(
                conversation,
                conversation.contact,
                last_message=last_msg,
                unread_count=unread_counts.get(conversation_id, 0),
            ),
            room=f"property:{body.property_id}",
        )
    except Exception as exc:
        log.warning("Socket.IO emit failed on assign: %s", exc)

    return AssignConversationResponse(
        conversation_id=conversation_id,
        property_id=body.property_id,
        attention_session_id=attention_session.id,
        created=created,
    )


# ---------------------------------------------------------------------------
# GET /conversations/{conversation_id}/transfer-targets
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}/transfer-targets",
    response_model=TransferTargetsResponse,
    summary="List properties available as transfer destinations",
    description=(
        "Returns all properties in the instance that share the same channel "
        "endpoint as this conversation. These are the valid "
        "destinations for POST /transfer. Properties on a different channel "
        "endpoint are excluded because the guest would need to start a new thread."
    ),
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Conversation not found"},
    },
)
async def list_transfer_targets(
    conversation_id: int,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> TransferTargetsResponse:
    conversation = await conversation_repo.find_by_id(db, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    active_sessions = await session_repo.find_active_for_conversation(
        db, conversation_id
    )
    current_property_id = next(
        (s.property_id for s in active_sessions if s.property_id is not None),
        None,
    )

    props = await instance_repo.find_properties_with_channel(db, instance.id)

    return TransferTargetsResponse(
        conversation_id=conversation_id,
        properties=[
            TransferTargetProperty(
                id=p.id,
                name=p.name,
                roomdoo_external_code=p.roomdoo_external_code,
            )
            for p in props
            if p.id != current_property_id
        ],
    )


# ---------------------------------------------------------------------------
# POST /conversations/{conversation_id}/transfer
# ---------------------------------------------------------------------------


@router.post(
    "/{conversation_id}/transfer",
    response_model=TransferConversationResponse,
    summary="Transfer a conversation to another property",
    description=(
        "Moves a conversation from its current session to a different property "
        "within the same instance. Creates transfer notes in both the source "
        "and destination sessions. The source session is closed and a new active "
        "session is created for the destination property.\n\n"
        "Raises 422 if the conversation is already assigned to the destination."
    ),
    responses={
        401: {"description": "Missing or invalid Bearer token"},
        404: {"description": "Conversation or property not found"},
        422: {"description": "Already assigned to this property"},
    },
)
async def transfer_conversation(
    conversation_id: int,
    body: TransferConversationRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sio: socketio.AsyncServer = Depends(get_sio),
) -> TransferConversationResponse:
    result = await transfer_service.transfer_conversation(
        db=db,
        sio=sio,
        instance=instance,
        conversation_id=conversation_id,
        destination_property_id=body.destination_property_id,
        note_text=body.note,
    )
    return TransferConversationResponse(**result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _build_items_with_last_message(
    db: AsyncSession,
    conversations: list,
    property_id: int = 0,
) -> list[ConversationListItem]:
    """Batch-load last message and unread counts for each conversation."""
    conversation_ids = [c.id for c in conversations]
    last_messages: dict[int, Message] = {}
    if conversation_ids:
        # Exclude auto-generated folio-event notes (template_code IS NOT NULL)
        # so the inbox preview shows the last real message, not a silent log entry.
        max_id_subq = (
            select(func.max(Message.id).label("max_id"))
            .where(
                Message.conversation_id.in_(conversation_ids),
                ~(
                    (Message.kind == MessageKind.note)
                    & Message.template_code.isnot(None)
                ),
            )
            .group_by(Message.conversation_id)
            .subquery()
        )
        result = await db.execute(
            select(Message).where(
                Message.id.in_(select(max_id_subq.c.max_id))
            )
        )
        for msg in result.scalars().all():
            last_messages[msg.conversation_id] = msg

    unread_counts = await conversation_repo.get_unread_counts(
        db, conversation_ids, property_id
    )
    needs_attention = await conversation_repo.get_needs_attention(
        db, conversation_ids, property_id
    )

    items: list[ConversationListItem] = []
    for conv in conversations:
        contact = conv.contact
        last_msg = last_messages.get(conv.id)
        items.append(
            ConversationListItem(
                id=conv.id,
                created_at=(
                    conv.created_at.isoformat() if conv.created_at else ""
                ),
                updated_at=(
                    conv.updated_at.isoformat() if conv.updated_at else None
                ),
                contact=ContactSummary(
                    id=contact.id,
                    phone_code=contact.phone_code,
                    display_name=contact.display_name,
                ),
                last_message=LastMessageSummary(
                    id=last_msg.id,
                    direction=last_msg.direction.value,
                    sender=last_msg.sender.value,
                    content=last_msg.content,
                    created_at=(
                        last_msg.created_at.isoformat()
                        if last_msg.created_at else ""
                    ),
                ) if last_msg else None,
                unread_count=unread_counts.get(conv.id, 0),
                needs_attention=needs_attention.get(conv.id, False),
            )
        )
    return items
