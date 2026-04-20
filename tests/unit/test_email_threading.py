"""Unit tests for email threading helper functions."""

from app.repositories.email_message_repo import normalize_subject, parse_message_ids


class TestParseMessageIds:
    def test_extracts_single_in_reply_to(self):
        ids = parse_message_ids("<abc@mg.example.com>", None)
        assert ids == ["<abc@mg.example.com>"]

    def test_extracts_multiple_from_references(self):
        refs = "<a@mg.com> <b@mg.com> <c@mg.com>"
        ids = parse_message_ids(None, refs)
        assert ids == ["<a@mg.com>", "<b@mg.com>", "<c@mg.com>"]

    def test_in_reply_to_takes_precedence(self):
        ids = parse_message_ids("<first@mg.com>", "<first@mg.com> <second@mg.com>")
        # deduplication preserves order: first appears once, then second
        assert ids == ["<first@mg.com>", "<second@mg.com>"]

    def test_empty_inputs(self):
        assert parse_message_ids(None, None) == []

    def test_no_angle_brackets(self):
        # Malformed header — no <> brackets
        assert parse_message_ids("abc@mg.com", None) == []

    def test_mixed_none_and_value(self):
        ids = parse_message_ids(None, "<only@mg.com>")
        assert ids == ["<only@mg.com>"]


class TestNormalizeSubject:
    def test_strips_re_prefix(self):
        assert normalize_subject("Re: Confirmación de su reserva") == (
            "confirmación de su reserva"
        )

    def test_strips_multiple_re_prefixes(self):
        assert normalize_subject("Re: Re: Hello") == "hello"

    def test_strips_fwd_prefix(self):
        assert normalize_subject("Fwd: Hello") == "hello"

    def test_strips_fw_prefix(self):
        assert normalize_subject("FW: Hello") == "hello"

    def test_case_insensitive(self):
        assert normalize_subject("RE: Test") == "test"

    def test_strips_aw_prefix(self):
        # German "Antwort"
        assert normalize_subject("AW: Test") == "test"

    def test_empty_subject(self):
        assert normalize_subject("") == ""

    def test_none_subject(self):
        assert normalize_subject(None) == ""

    def test_no_prefix(self):
        assert normalize_subject("Booking Confirmation") == "booking confirmation"

    def test_strips_whitespace(self):
        assert normalize_subject("  Re:   Hello  ") == "hello"
