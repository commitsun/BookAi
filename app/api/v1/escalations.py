"""
GET   /api/v1/escalations                          — list escalations for a property
GET   /api/v1/conversations/{id}/escalations        — escalations for a conversation
PATCH /api/v1/escalations/{id}/resolve              — resolve an escalation
POST  /api/v1/escalations/{id}/chat                 — operator instruction → AI draft
POST  /api/v1/escalations/{id}/refine-draft         — refine current draft
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    get_db, get_instance, get_llm_client, get_sdk_registry, get_sio,
    resolve_property,
)
from app.models.instance import Instance
from app.repositories import escalation_repo
from app.schemas.escalation import (
    EscalationChatRequest,
    EscalationChatResponse,
    EscalationMessageOut,
    EscalationOut,
    RefineDraftRequest,
    RefineDraftResponse,
    ResolveRequest,
)
from app.services.escalation_draft_service import DraftError
from app.services.instance_sdk_registry import InstanceSDKRegistry
from app.services.llm_client import LLMProvider

log = logging.getLogger("escalations_api")

router = APIRouter(tags=["escalations"])


# ── Helpers ──────────────────────────────────────────────────────────

def _esc_to_out(esc, include_messages: bool = False) -> EscalationOut:
    messages = None
    if include_messages and hasattr(esc, "messages") and esc.messages:
        messages = [
            EscalationMessageOut(
                id=m.id,
                sender=m.sender.value if m.sender else "system",
                content=m.content,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for m in sorted(esc.messages, key=lambda m: m.id)
        ]
    return EscalationOut(
        id=esc.id,
        conversation_id=esc.conversation_id,
        session_id=esc.session_id,
        escalation_type=esc.escalation_type,
        reason=esc.reason,
        context=esc.context,
        guest_message=esc.guest_message,
        priority=esc.priority,
        status=esc.status,
        draft_response=esc.draft_response,
        resolved_by=esc.resolved_by,
        resolution_medium=esc.resolution_medium,
        resolution_notes=esc.resolution_notes,
        created_at=esc.created_at.isoformat() if esc.created_at else "",
        resolved_at=esc.resolved_at.isoformat() if esc.resolved_at else None,
        messages=messages,
    )


# ── Endpoints ────────────────────────────────────────────────────────

@router.get(
    "/escalations",
    summary="List escalations for a property",
)
async def list_escalations(
    property_id: int = Query(..., description="Odoo property ID"),
    status: str | None = Query(default=None),
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    prop = await resolve_property(property_id, instance, db)
    internal_id = prop.id if prop else 0
    escs = await escalation_repo.list_for_property(db, internal_id, status)
    return {
        "property_id": property_id,
        "escalations": [_esc_to_out(e) for e in escs],
    }


@router.get(
    "/conversations/{conversation_id}/escalations",
    summary="List escalations for a conversation with messages",
)
async def list_conversation_escalations(
    conversation_id: int,
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
) -> dict:
    escs = await escalation_repo.find_pending_for_conversation(db, conversation_id)
    # Also include resolved ones for the timeline
    from sqlalchemy import select
    from app.models.escalation import Escalation
    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(Escalation)
        .options(selectinload(Escalation.messages))
        .where(Escalation.conversation_id == conversation_id)
        .order_by(Escalation.created_at.desc())
        .limit(20)
    )
    all_escs = list(result.scalars().all())
    return {
        "conversation_id": conversation_id,
        "escalations": [_esc_to_out(e, include_messages=True) for e in all_escs],
    }


@router.patch(
    "/escalations/{escalation_id}/resolve",
    summary="Resolve an escalation",
)
async def resolve_escalation(
    escalation_id: int,
    body: ResolveRequest,
    instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sio=Depends(get_sio),
) -> dict:
    esc = await escalation_repo.find_by_id(db, escalation_id)
    if esc is None:
        raise HTTPException(status_code=404, detail="Escalation not found")
    if esc.status != "pending":
        raise HTTPException(status_code=409, detail="Escalation already resolved")

    await escalation_repo.resolve(
        db, esc,
        resolved_by=None,  # TODO: extract from auth
        resolution_medium=body.resolution_medium,
        resolution_notes=body.resolution_notes,
    )

    # Restore AI if resolved via supervised flow
    if body.resolution_medium != "manual_takeover":
        from app.models.session import AttentionSession
        session = await db.get(AttentionSession, esc.session_id)
        if session and esc.ai_was_enabled:
            session.ai_enabled = True

    await db.commit()

    # Socket.IO event
    try:
        from app.models.session import AttentionSession
        session = await db.get(AttentionSession, esc.session_id)
        property_id = session.property_id if session else None
        if property_id:
            await sio.emit(
                "escalation.resolved",
                {
                    "conversation_id": esc.conversation_id,
                    "escalation_id": esc.id,
                    "resolved_by": esc.resolved_by,
                    "resolution_medium": esc.resolution_medium,
                    "resolution_notes": esc.resolution_notes,
                },
                room=f"property:{property_id}",
            )
    except Exception as exc:
        log.warning("Socket emit failed: %s", exc)

    return {"status": "ok", "escalation_id": esc.id}


# ── Escalation draft (operator ↔ AI) ───────────────────────────────


@router.post(
    "/escalations/{escalation_id}/chat",
    summary="Operator instruction → AI generates draft response",
)
async def escalation_chat(
    escalation_id: int,
    body: EscalationChatRequest,
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sio=Depends(get_sio),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    llm_client: LLMProvider = Depends(get_llm_client),
) -> EscalationChatResponse:
    from app.services.escalation_draft_service import generate_draft

    try:
        draft, messages = await generate_draft(
            escalation_id, body.instruction,
            body.agent_user_id, body.agent_display_name,
            db, sio, sdk_registry, llm_client,
        )
    except DraftError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail,
        ) from exc

    return EscalationChatResponse(
        escalation_id=escalation_id,
        draft_response=draft,
        messages=[
            EscalationMessageOut(
                id=m.id,
                sender=m.sender.value if m.sender else "system",
                content=m.content,
                created_at=(
                    m.created_at.isoformat() if m.created_at else ""
                ),
            )
            for m in messages
        ],
    )


@router.post(
    "/escalations/{escalation_id}/refine-draft",
    summary="Refine the current draft with adjustments",
)
async def refine_escalation_draft(
    escalation_id: int,
    body: RefineDraftRequest,
    _instance: Instance = Depends(get_instance),
    db: AsyncSession = Depends(get_db),
    sio=Depends(get_sio),
    sdk_registry: InstanceSDKRegistry = Depends(get_sdk_registry),
    llm_client: LLMProvider = Depends(get_llm_client),
) -> RefineDraftResponse:
    from app.services.escalation_draft_service import refine_draft

    try:
        draft = await refine_draft(
            escalation_id, body.instruction, body.current_draft,
            db, sio, sdk_registry, llm_client,
        )
    except DraftError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail=exc.detail,
        ) from exc

    return RefineDraftResponse(
        escalation_id=escalation_id,
        draft_response=draft,
    )
