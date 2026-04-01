"""Unit tests for session activity evaluation and routing selection."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.models.folio import Folio, FolioStatus
from app.services.session_service import SESSION_ACTIVE_DAYS, is_session_active, pick_session

NOW = datetime.now(timezone.utc)
RECENT = NOW - timedelta(hours=1)
OLD = NOW - timedelta(days=SESSION_ACTIVE_DAYS + 1)


def _folio(status: FolioStatus, charges: float = 0.0) -> Folio:
    f = MagicMock(spec=Folio)
    f.status = status
    f.pending_payment_amount = Decimal(str(charges)) if charges else None
    return f


def _session(last_msg_at: datetime | None = None) -> MagicMock:
    s = MagicMock()
    s.last_message_at = last_msg_at
    s.id = 1
    return s


# ---------------------------------------------------------------------------
# is_session_active
# ---------------------------------------------------------------------------


def test_active_recent_no_folio():
    """No folio + recent message → active by recency alone."""
    assert is_session_active([], RECENT) is True


def test_closed_old_no_folio():
    """No folio + old message → inactive."""
    assert is_session_active([], OLD) is False


def test_closed_none_timestamp_no_folio():
    """No folio + no messages ever → inactive."""
    assert is_session_active([], None) is False


def test_active_folio_onboard_old():
    """folio=onboard (non-terminal) + old message → active via folio."""
    assert is_session_active([_folio(FolioStatus.onboard)], OLD) is True


def test_active_folio_done_with_charges_old():
    """folio=done but has pending charges + old message → active via charges."""
    assert is_session_active([_folio(FolioStatus.done, charges=150.0)], OLD) is True


def test_active_done_recent_no_charges():
    """folio=done, no charges, but recent message → active via recency."""
    assert is_session_active([_folio(FolioStatus.done)], RECENT) is True


def test_closed_done_no_charges_old():
    """folio=done, no charges, old message → inactive (both conditions fail)."""
    assert is_session_active([_folio(FolioStatus.done)], OLD) is False


def test_closed_cancel_no_charges_old():
    """folio=cancel, no charges, old message → inactive."""
    assert is_session_active([_folio(FolioStatus.cancel)], OLD) is False


# ---------------------------------------------------------------------------
# pick_session
# ---------------------------------------------------------------------------


def test_pick_session_most_recent():
    """pick_session returns the session with the most recent last_message_at."""
    s1 = _session(NOW - timedelta(hours=5))
    s1.id = 1
    s1.property_id = 10

    s2 = _session(NOW - timedelta(hours=1))
    s2.id = 2
    s2.property_id = 20

    chosen = pick_session([s1, s2])
    assert chosen.id == 2  # s2 has the more recent message


def test_pick_session_tie_broken_by_id():
    """Ties are broken by lowest session id."""
    ts = NOW - timedelta(hours=2)
    s1 = _session(ts)
    s1.id = 1
    s2 = _session(ts)
    s2.id = 2

    chosen = pick_session([s1, s2])
    assert chosen.id == 1


def test_pick_session_none_timestamp_sorts_last():
    """A session with no messages sorts last (treated as epoch 0)."""
    s_recent = _session(NOW - timedelta(hours=1))
    s_recent.id = 1
    s_none = _session(None)
    s_none.id = 2

    chosen = pick_session([s_recent, s_none])
    assert chosen.id == 1  # the one with a timestamp wins
