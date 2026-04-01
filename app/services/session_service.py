"""
Session activity evaluation and routing selection.

This module is the primary extension point for future AI agent routing:
- is_session_active()  → decides if a session should handle incoming messages
- pick_session()       → selects which session to route to when multiple are active

Both functions can be overridden or extended in future phases without changing
the routing logic in webhook_service.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.models.folio import Folio, FolioStatus

if TYPE_CHECKING:
    from app.models.session import AttentionSession

SESSION_ACTIVE_DAYS = 7

_TERMINAL_STATUSES = {FolioStatus.done, FolioStatus.cancel}


def is_session_active(
    folios: list[Folio],
    conversation_last_message_at: datetime | None,
) -> bool:
    """
    Determines whether an AttentionSession should handle incoming messages.

    A session is active if AT LEAST ONE of the following is true:

    1. **Recency** — the conversation had a message within SESSION_ACTIVE_DAYS.
    2. **Folio active** — at least one linked folio is non-terminal
       (status not in done/cancel) OR has a pending payment amount > 0.

    When no folio is attached, only the recency condition applies.

    Extension point: override or wrap this function in future phases to let
    AI agents evaluate conversation context and influence routing decisions.
    """
    # Condition 1: recency
    recent = False
    if conversation_last_message_at is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_ACTIVE_DAYS)
        recent = conversation_last_message_at >= cutoff

    if not folios:
        # No folio attached — only recency applies
        return recent

    # Condition 2: any folio keeps this session open
    folio_active = any(_folio_is_active(f) for f in folios)

    return recent or folio_active


def _folio_is_active(folio: Folio) -> bool:
    has_charges = (folio.pending_payment_amount or 0) > 0
    is_non_terminal = folio.status not in _TERMINAL_STATUSES
    return has_charges or is_non_terminal


def pick_session(active_sessions: list[AttentionSession]) -> AttentionSession:
    """
    When multiple sessions are active, pick the one whose last message is most recent.
    Ties are broken by lowest session id (oldest, most established session).

    Extension point: in future phases an AI agent can override this to select
    based on conversation context, topic, agent availability, etc.
    """
    def _sort_key(s: AttentionSession) -> tuple:
        ts: datetime | None = getattr(s, "last_message_at", None)
        # Negate timestamp → descending sort; None sorts last (treated as epoch 0)
        return (-(ts.timestamp() if ts else 0), s.id)

    return min(active_sessions, key=_sort_key)
