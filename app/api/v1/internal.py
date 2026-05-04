"""
POST /api/v1/internal/conversations              — create internal chat thread
GET  /api/v1/internal/conversations              — list user's internal threads
POST /api/v1/internal/conversations/{id}/messages — send message (triggers AI pipeline)
PATCH /api/v1/internal/conversations/{id}/title   — update thread title
GET  /api/v1/internal/conversations/{id}/messages — get message history
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import get_db, get_instance, get_sio, get_sdk_registry, get_wa_client
from app.models.instance import Instance
from app.services.instance_sdk_registry import InstanceSDKRegistry

log = logging.getLogger("internal_chat")

router = APIRouter(prefix="/internal", tags=["internal-chat"])


# ── Schemas ───────────────────────────────────────────────────────────

class CreateConversationRequest(BaseModel):
    property_id: int
    odoo_user_id: int | None = None
    odoo_user_login: str | None = None
    conversation_type: str = Field(default="internal", pattern="^(internal|roomdoo)$")


class CreateConversationResponse(BaseModel):
    conversation_id: int
    title: str | None
    conversation_type: str


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1)
    odoo_user_id: int | None = None


class SendMessageResponse(BaseModel):
    message_id: int
    conversation_id: int


class UpdateTitleRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)


class InternalConversationOut(BaseModel):
    id: int
    title: str | None
    conversation_type: str
    created_at: str
    updated_at: str | None
    last_message: dict | None = None


# ── Endpoints ─────────────────────────────────────────────────────────

@router.post(
    "/conversations",
    response_model=CreateConversationResponse,
    summary="Create a new internal chat thread",
)
async def create_conversation(
    body: CreateConversationRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> CreateConversationResponse:
    from app.api.dependencies import resolve_property
    prop = await resolve_property(body.property_id, instance, db)
    if prop is None:
        from fastapi import HTTPException as _Exc
        raise _Exc(status_code=404, detail="Property not found")

    from app.services.internal_chat_service import create_internal_conversation
    conv = await create_internal_conversation(
        db,
        property_id=prop.id,
        conversation_type=body.conversation_type,
        odoo_user_id=body.odoo_user_id,
        odoo_user_login=body.odoo_user_login,
    )
    await db.commit()

    return CreateConversationResponse(
        conversation_id=conv.id,
        title=conv.title,
        conversation_type=conv.conversation_type,
    )


@router.get(
    "/conversations",
    summary="List internal conversations for a user",
)
async def list_conversations(
    property_id: int = Query(..., description="Odoo property ID"),
    odoo_user_id: int | None = Query(default=None),
    odoo_user_login: str | None = Query(default=None),
    conversation_type: str = Query(default="internal"),
    limit: int = Query(default=50, ge=1, le=200),
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.api.dependencies import resolve_property
    prop = await resolve_property(property_id, instance, db)
    internal_id = prop.id if prop else 0

    from app.services.internal_chat_service import list_internal_conversations
    convs = await list_internal_conversations(
        db,
        property_id=internal_id,
        conversation_type=conversation_type,
        odoo_user_id=odoo_user_id,
        odoo_user_login=odoo_user_login,
        limit=limit,
    )

    items = []
    for conv in convs:
        last_msg = None
        if conv.messages:
            m = conv.messages[0]  # ordered desc
            last_msg = {
                "id": m.id,
                "sender": m.sender.value if m.sender else None,
                "content": (m.content or "")[:200],
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        items.append({
            "id": conv.id,
            "title": conv.title,
            "conversation_type": conv.conversation_type,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "last_message": last_msg,
        })

    return {"conversations": items}


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    summary="Send message in internal chat (triggers AI pipeline)",
)
async def send_message(
    conversation_id: int,
    body: SendMessageRequest,
    request: Request,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    sio=Depends(get_sio),
    wa_client=Depends(get_wa_client),
) -> SendMessageResponse:
    from app.models.conversation import Conversation
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.conversation_type == "guest":
        raise HTTPException(404, "Internal conversation not found")

    from app.services.internal_chat_service import send_internal_message
    llm_client = request.app.state.llm_client
    mcp_manager = request.app.state.mcp_manager

    msg = await send_internal_message(
        db=db,
        conversation=conv,
        content=body.content,
        odoo_user_id=body.odoo_user_id,
        instance=instance,
        sdk_registry=sdk_registry,
        llm_client=llm_client,
        wa_client=wa_client,
        sio=sio,
        mcp_manager=mcp_manager,
    )

    return SendMessageResponse(
        message_id=msg.id,
        conversation_id=conversation_id,
    )


@router.get(
    "/conversations/{conversation_id}/messages",
    summary="Get message history for internal conversation",
)
async def get_messages(
    conversation_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: int | None = Query(default=None),
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models.conversation import Conversation
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.conversation_type == "guest":
        raise HTTPException(404, "Internal conversation not found")

    from app.repositories import message_repo
    messages = await message_repo.find_recent_by_conversation(
        db, conversation_id, limit=limit,
    )
    messages.reverse()

    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "id": m.id,
                "sender": m.sender.value if m.sender else None,
                "content": m.content,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in messages
        ],
    }


@router.patch(
    "/conversations/{conversation_id}/title",
    summary="Update conversation title",
)
async def update_title(
    conversation_id: int,
    body: UpdateTitleRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models.conversation import Conversation
    conv = await db.get(Conversation, conversation_id)
    if not conv or conv.conversation_type == "guest":
        raise HTTPException(404, "Internal conversation not found")

    conv.title = body.title
    await db.commit()
    return {"conversation_id": conversation_id, "title": conv.title}
