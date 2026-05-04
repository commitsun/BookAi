"""Pydantic schemas for escalation endpoints."""

from pydantic import BaseModel, Field


# ── Read schemas (shared by list + detail endpoints) ────────────────

class EscalationMessageOut(BaseModel):
    id: int
    sender: str
    content: str | None
    created_at: str


class EscalationOut(BaseModel):
    id: int
    conversation_id: int
    session_id: int
    escalation_type: str
    reason: str
    context: str | None
    guest_message: str
    priority: int
    status: str
    draft_response: str | None
    resolved_by: str | None
    resolution_medium: str | None
    resolution_notes: str | None
    created_at: str
    resolved_at: str | None
    messages: list[EscalationMessageOut] | None = None


# ── Resolve ─────────────────────────────────────────────────────────

class ResolveRequest(BaseModel):
    resolution_medium: str | None = Field(
        default=None,
        description="whatsapp | phone | in_person | ai_supervised | manual_takeover | other",
    )
    resolution_notes: str | None = None


# ── Escalation chat (operator ↔ AI draft generation) ───────────────

class EscalationChatRequest(BaseModel):
    instruction: str = Field(..., min_length=1)
    agent_user_id: int | None = None
    agent_display_name: str | None = None


class EscalationChatResponse(BaseModel):
    escalation_id: int
    draft_response: str | None
    messages: list[EscalationMessageOut]


# ── Refine draft ────────────────────────────────────────────────────

class RefineDraftRequest(BaseModel):
    instruction: str = Field(..., min_length=1)
    current_draft: str | None = None


class RefineDraftResponse(BaseModel):
    escalation_id: int
    draft_response: str
