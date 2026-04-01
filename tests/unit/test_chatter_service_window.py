"""
Unit tests for the channel messaging window logic in chatter_service.py.

Covers:
  - WhatsApp: window closes 24 h after last inbound message
  - Non-WhatsApp channels: no time restriction, only requires any prior inbound
  - Edge cases: no inbound message at all
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.chatter_service import _window_open


def _ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ---------------------------------------------------------------------------
# WhatsApp — 24-hour window
# ---------------------------------------------------------------------------


def test_whatsapp_window_open_within_24h():
    """Last inbound 20 h ago → window open."""
    assert _window_open(_ago(20), "whatsapp") is True


def test_whatsapp_window_closed_after_24h():
    """Last inbound 25 h ago → window closed."""
    assert _window_open(_ago(25), "whatsapp") is False


def test_whatsapp_window_closed_with_no_inbound():
    """No inbound ever → window closed for WhatsApp."""
    assert _window_open(None, "whatsapp") is False


def test_whatsapp_window_exactly_at_boundary():
    """Last inbound exactly 24 h ago is still closed (cutoff is exclusive)."""
    assert _window_open(_ago(24), "whatsapp") is False


# ---------------------------------------------------------------------------
# Non-WhatsApp channels — no time restriction
# ---------------------------------------------------------------------------


def test_telegram_window_open_after_25h():
    """Telegram has no time restriction: last inbound 25 h ago → window open."""
    assert _window_open(_ago(25), "telegram") is True


def test_sms_window_open_after_long_time():
    """SMS has no restriction: last inbound 100 h ago → window open."""
    assert _window_open(_ago(100), "sms") is True


def test_non_whatsapp_window_closed_with_no_inbound():
    """Non-WhatsApp: no inbound ever → window closed (must have received at least one)."""
    assert _window_open(None, "telegram") is False
