"""Cross-delta redaction, including the one-character acceptance case."""

from __future__ import annotations

from tests.task3.conftest import completion_body, make_app, read_stream, visible_text

from gateway_service.task3_stream_guardrail.pii_patterns import REDACTION_TOKEN
from gateway_service.task3_stream_guardrail.stream_buffer import StreamBuffer


def _push(parts: list[str]) -> str:
    buffer = StreamBuffer()
    released = "".join(buffer.push(part) for part in parts)
    return released + buffer.flush()


def test_one_character_email_is_withheld_until_it_can_be_redacted() -> None:
    email = "alex@example.com"
    buffer = StreamBuffer()
    released = ""
    for character in email:
        released += buffer.push(character)
        assert released == "" or released == REDACTION_TOKEN
        assert "@" not in released
        assert "alex" not in released
        assert "example" not in released
    assert released == ""
    assert buffer.flush() == REDACTION_TOKEN
    assert _push(["alex", "@", "example", ".", "com"]) == REDACTION_TOKEN


async def test_gateway_hides_an_email_split_across_deltas(tmp_path) -> None:
    application = make_app(tmp_path)
    status, content_type, body = await read_stream(
        application, completion_body("mock-email-parts", "unused")
    )
    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert visible_text(body) == REDACTION_TOKEN
    assert b"alex" not in body
    assert b"@" not in body
    assert b"example.com" not in body
    assert body.endswith(b"data: [DONE]\n\n")


async def test_gateway_hides_one_character_deltas_from_the_user_message(tmp_path) -> None:
    application = make_app(tmp_path)
    email = "alex@example.com"
    status, _, body = await read_stream(application, completion_body("mock-chars", email))
    assert status == 200
    assert visible_text(body) == REDACTION_TOKEN
    assert email.encode() not in body
    assert b"@" not in body
