"""PII detector contract: Luhn, SSN structure, and match limits."""

from __future__ import annotations

from gateway_service.task3_stream_guardrail.pii_patterns import (
    MAX_CARD_CHARS,
    MAX_EMAIL_CHARS,
    REDACTION_TOKEN,
    SSN_CHARS,
    is_viable_prefix,
    luhn_valid,
    redact_stable,
)
from gateway_service.task3_stream_guardrail.stream_buffer import StreamBuffer


def test_luhn_accepts_known_pan_and_rejects_neighbor() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert redact_stable("4111111111111111", REDACTION_TOKEN) == REDACTION_TOKEN
    assert redact_stable("4111111111111112", REDACTION_TOKEN) == "4111111111111112"


def test_separated_cards_redact_only_when_luhn_valid() -> None:
    assert redact_stable("4111 1111 1111 1111", REDACTION_TOKEN) == REDACTION_TOKEN
    assert redact_stable("4111-1111-1111-1111", REDACTION_TOKEN) == REDACTION_TOKEN
    assert redact_stable("4111 1111 1111 1112", REDACTION_TOKEN) == "4111 1111 1111 1112"


def test_ssn_rejects_structural_invalids() -> None:
    assert redact_stable("123-45-6789", REDACTION_TOKEN) == REDACTION_TOKEN
    for invalid in ("000-12-3456", "666-12-3456", "900-12-3456", "123-00-6789", "123-45-0000"):
        assert redact_stable(invalid, REDACTION_TOKEN) == invalid
    assert redact_stable("123456789", REDACTION_TOKEN) == "123456789"


def test_email_match_requires_a_real_domain() -> None:
    assert redact_stable("alex@example.com", REDACTION_TOKEN) == REDACTION_TOKEN
    assert redact_stable("alex@example", REDACTION_TOKEN) == "alex@example"
    assert redact_stable("a@b.c", REDACTION_TOKEN) == "a@b.c"


def test_match_limits_are_the_documented_bounds() -> None:
    assert MAX_EMAIL_CHARS == 320
    assert MAX_CARD_CHARS == 37
    assert SSN_CHARS == 11
    assert not is_viable_prefix("a" * 321)


def test_buffer_releases_a_closed_email_and_keeps_capacity() -> None:
    buffer = StreamBuffer()
    assert buffer.push("alex@example.com ") == REDACTION_TOKEN + " "
    assert buffer.buffered_length == 0
