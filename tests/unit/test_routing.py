"""Unit tests for the channel messaging window check in chatter_service.

See also tests/unit/test_chatter_service_window.py for comprehensive
multi-channel coverage.
"""

from datetime import datetime, timedelta, timezone

from app.services.chatter_service import _window_open


def test_window_open_within_24h():
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=1)
    assert _window_open(last_inbound, "whatsapp") is True


def test_window_closed_after_24h():
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=25)
    assert _window_open(last_inbound, "whatsapp") is False


def test_window_closed_when_none():
    assert _window_open(None, "whatsapp") is False


def test_window_open_just_under_24h():
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=23, minutes=59)
    assert _window_open(last_inbound, "whatsapp") is True


def test_window_closed_exactly_at_boundary():
    # Exactly 24 hours ago — boundary: not >= cutoff
    last_inbound = datetime.now(timezone.utc) - timedelta(hours=24, seconds=1)
    assert _window_open(last_inbound, "whatsapp") is False
