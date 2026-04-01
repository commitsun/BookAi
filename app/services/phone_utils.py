"""Phone number normalization utilities."""

import phonenumbers
from phonenumbers import NumberParseException


def normalize_phone(phone: str, country_hint: str | None = None) -> str:
    """
    Parse and return E.164 digits without the leading '+'.

    Examples:
        "+34 699 323 583" → "34699323583"
        "699323583" with country_hint="ES" → "34699323583"

    Raises ValueError if the number cannot be parsed or is invalid.
    """
    raw = phone.strip()
    if not raw.startswith("+"):
        raw = f"+{raw}"
    try:
        parsed = phonenumbers.parse(raw, country_hint)
    except NumberParseException as exc:
        raise ValueError(f"Cannot parse phone '{phone}': {exc}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"Invalid phone number: '{phone}'")
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return e164.lstrip("+")
