"""
Unit tests for app/services/note_templates.py.
Pure function tests — no DB, no fixtures.
"""

import pytest

from app.services.note_templates import render_note, unrouted_label, SUPPORTED_LANGUAGES


def test_folio_created_es():
    result = render_note("folio_created", "es")
    assert result and "reserva" in result.lower()


def test_folio_created_all_languages():
    for lang in ("es", "gl", "pt", "en", "fr"):
        result = render_note("folio_created", lang)
        assert result, f"Empty template for folio_created/{lang}"


def test_payment_registered_interpolation():
    result = render_note("payment_registered", "es", amount="250.00", currency="EUR")
    assert "250.00" in result
    assert "EUR" in result


def test_precheckin_interpolation():
    result = render_note("precheckin_completed", "en", guest_name="John Doe", room_number="201")
    assert "John Doe" in result
    assert "201" in result


def test_dates_changed_interpolation():
    result = render_note(
        "folio_modified.dates_changed",
        "es",
        checkin_date="2026-04-01",
        checkout_date="2026-04-05",
    )
    assert "2026-04-01" in result
    assert "2026-04-05" in result


def test_status_changed_interpolation():
    result = render_note("status_changed", "fr", new_status="onboard")
    assert "onboard" in result


def test_unknown_template_key_raises():
    with pytest.raises(KeyError):
        render_note("nonexistent_event", "es")


def test_transfer_outgoing_interpolation():
    result = render_note(
        "transfer.outgoing", "es",
        dest_name="Hotel Vigo", agent_note="El huésped llega antes",
    )
    assert "Hotel Vigo" in result
    assert "El huésped llega antes" in result


def test_transfer_incoming_interpolation():
    result = render_note(
        "transfer.incoming", "en",
        origin_name="Hotel Madrid", agent_note="Early arrival",
    )
    assert "Hotel Madrid" in result
    assert "Early arrival" in result


def test_transfer_templates_all_languages():
    for key in ("transfer.outgoing", "transfer.incoming"):
        for lang in ("es", "gl", "pt", "en", "fr"):
            result = render_note(key, lang, dest_name="X", origin_name="X", agent_note="Y")
            assert result, f"Empty template for {key}/{lang}"


def test_unrouted_label_all_languages():
    for lang in ("es", "gl", "pt", "en", "fr"):
        label = unrouted_label(lang)
        assert label, f"Empty unrouted label for {lang}"


def test_unrouted_label_fallback():
    assert unrouted_label("xx") == unrouted_label("es")
