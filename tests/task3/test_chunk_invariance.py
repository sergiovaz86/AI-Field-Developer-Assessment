"""Seeded checks that redaction does not depend on chunk boundaries."""

from __future__ import annotations

import json
import random

from tests.task3.conftest import (
    ScriptedUpstream,
    completion_body,
    make_app,
    read_stream,
    sse_payloads,
)

from gateway_service.task3_stream_guardrail.pii_patterns import (
    MAX_CARD_CHARS,
    MAX_EMAIL_CHARS,
    MAX_EMAIL_LOCAL,
    REDACTION_TOKEN,
    luhn_valid,
)
from gateway_service.task3_stream_guardrail.stream_buffer import StreamBuffer


def _release(parts: list[str]) -> str:
    buffer = StreamBuffer()
    released = "".join(buffer.push(part) for part in parts)
    assert buffer.buffered_length <= MAX_EMAIL_CHARS
    return released + buffer.flush()


def _splits(text: str, rng: random.Random) -> list[list[str]]:
    splits = [[text], list(text)]
    for _ in range(12):
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            take = rng.randint(1, 9)
            parts.append(text[cursor : cursor + take])
            cursor += take
        splits.append(parts)
    return splits


def _luhn_digits(length: int) -> str:
    body = "4" + ("1" * (length - 2))
    for check in "0123456789":
        candidate = body[: length - 1] + check
        if len(candidate) == length and luhn_valid(candidate):
            return candidate
    raise AssertionError(f"no {length}-digit Luhn number")


def _samples() -> list[str]:
    card16 = _luhn_digits(16)
    card19 = _luhn_digits(19)
    spaced = " ".join(card16[index : index + 4] for index in range(0, 16, 4))
    hyphenated = "-".join(card16[index : index + 4] for index in range(0, 16, 4))
    local = "a" * MAX_EMAIL_LOCAL
    domain = ".".join(["b" * 63, "c" * 63, "d" * 63, "e" * 63])
    maximum = f"{local}@{domain}"
    assert len(maximum) == MAX_EMAIL_CHARS
    over = maximum + "z-extra@example.com"
    email = "alex@example.com"
    ssn = "123-45-6789"
    return [
        f"Hello {email} today",
        f"SSN {ssn} end",
        card16,
        card19,
        spaced,
        hyphenated,
        "4111111111111112",
        f"{email}{ssn}{card16}",
        f"{email} {email}",
        f"{ssn}{ssn}",
        f"pre {card16} mid {hyphenated}",
        "000-12-3456 and 123-45-67890",
        maximum,
        over,
        f"héllo {email} café",
        "no secrets here",
    ]


def test_seeded_splits_match_the_single_delta_result() -> None:
    rng = random.Random(4)
    for text in _samples():
        canonical = _release([text])
        for secret in ("alex@example.com", "123-45-6789", "example.com"):
            if secret in text:
                assert secret not in canonical
        if luhn_valid(text) and 13 <= len(text) <= 19:
            assert text not in canonical
            assert canonical == REDACTION_TOKEN
        for parts in _splits(text, rng):
            assert _release(parts) == canonical
        running = StreamBuffer()
        emitted = ""
        for character in text:
            emitted += running.push(character)
            assert running.buffered_length <= MAX_EMAIL_CHARS
            assert "alex@example.com" not in emitted
            assert "123-45-6789" not in emitted
        assert emitted + running.flush() == canonical


def test_card_shape_stays_within_its_character_limit() -> None:
    assert MAX_CARD_CHARS == 37


def _frame(payload: dict[str, object]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {body}\n\n".encode()


def _stream() -> bytes:
    model = "mock-audit"
    email = "alex@example.com"

    def chunk(delta: dict[str, object], finish: str | None = None) -> dict[str, object]:
        return {
            "id": "chatcmpl-audit",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    frames = [
        _frame(chunk({"role": "assistant"})),
        _frame(chunk({"content": "héllo "})),
        _frame(chunk({"content": email})),
        _frame(chunk({"refusal": "123-45-6789"})),
        _frame(chunk({"function_call": {"name": "send", "arguments": email}})),
        _frame(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": ""},
                        }
                    ]
                }
            )
        ),
    ]
    for character in '{"card":"4111111111111111"}':
        frames.append(
            _frame(chunk({"tool_calls": [{"index": 0, "function": {"arguments": character}}]}))
        )
    frames.append(
        _frame(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_b",
                            "type": "function",
                            "function": {"name": "other", "arguments": "4111-1111-1111-1111"},
                        }
                    ]
                }
            )
        )
    )
    frames.append(_frame(chunk({}, "tool_calls")))
    frames.append(b"data: [DONE]\n\n")
    return b"".join(frames)


def _project(body: bytes) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for payload in sse_payloads(body):
        for choice in payload.get("choices", []):
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            for field in ("content", "refusal"):
                if isinstance(delta.get(field), str):
                    rows.append((field, delta[field]))
            function_call = delta.get("function_call")
            if isinstance(function_call, dict):
                for field in ("name", "arguments"):
                    if isinstance(function_call.get(field), str):
                        rows.append((f"function_call.{field}", function_call[field]))
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    raw_function = call.get("function")
                    function = raw_function if isinstance(raw_function, dict) else {}
                    for field in ("name", "arguments"):
                        if isinstance(function.get(field), str):
                            rows.append((f"tool:{call.get('index')}.{field}", function[field]))
            finish = choice.get("finish_reason")
            if isinstance(finish, str):
                rows.append(("finish", finish))
    return rows


async def _post(tmp_path, chunks: list[bytes]) -> bytes:
    upstream = ScriptedUpstream(chunks)
    status, _, body = await read_stream(make_app(tmp_path, upstream), completion_body("mock-audit"))
    assert status == 200
    assert body.endswith(b"data: [DONE]\n\n")
    return body


async def test_multifield_stream_is_stable_across_byte_splits(tmp_path) -> None:
    raw = _stream()
    canonical = _project(await _post(tmp_path, [raw]))
    pieces = [raw[index : index + 1] for index in range(len(raw))]
    one_byte = _project(await _post(tmp_path, pieces))
    rng = random.Random(9)
    pieces = []
    cursor = 0
    while cursor < len(raw):
        # Split inside the UTF-8 payload and on arbitrary boundaries.
        take = rng.randint(1, 11)
        pieces.append(raw[cursor : cursor + take])
        cursor += take
    randomized = _project(await _post(tmp_path, pieces))
    assert one_byte == canonical == randomized
    rendered = json.dumps(canonical)
    assert "alex@example.com" not in rendered
    assert "123-45-6789" not in rendered
    assert "4111111111111111" not in rendered
    assert "4111-1111-1111-1111" not in rendered
    arguments = "".join(value for field, value in canonical if field == "tool:0.arguments")
    assert json.loads(arguments) == {"card": REDACTION_TOKEN}
    assert ("tool:0.name", "lookup") in canonical
    assert ("tool:1.name", "other") in canonical
    assert any(field == "content" and "héllo " in value for field, value in canonical)
    assert not any(field == "content" and "4111" in value for field, value in canonical)
    assert canonical[-1] == ("finish", "tool_calls")
