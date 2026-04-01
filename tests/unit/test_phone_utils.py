"""Unit tests for phone number normalization."""

import pytest

from app.services.phone_utils import normalize_phone


def test_normalize_e164_with_plus():
    assert normalize_phone("+34612345678") == "34612345678"


def test_normalize_already_digits_no_plus():
    # normalize_phone prepends + if missing, so "34612345678" → "+34612345678" → "34612345678"
    assert normalize_phone("34612345678") == "34612345678"


def test_normalize_spanish_mobile_full_e164():
    # normalize_phone prepends '+' before parse, so local numbers without country code
    # must be passed in E.164 form (e.g. "34612345678" or "+34612345678").
    assert normalize_phone("+34612345678") == "34612345678"


def test_normalize_strips_whitespace():
    assert normalize_phone("+34 612 345 678") == "34612345678"


def test_normalize_strips_leading_whitespace():
    assert normalize_phone("  +34612345678  ") == "34612345678"


def test_invalid_short_number_raises():
    with pytest.raises(ValueError):
        normalize_phone("123", "ES")


def test_invalid_number_raises():
    with pytest.raises(ValueError):
        normalize_phone("+99000000000000000")
