"""Bounded memory, cancellation, and isolated concurrent redactors."""

from __future__ import annotations

import asyncio

from tests.task3.conftest import client_for, completion_body, make_app, visible_text

from gateway_service.mocks.stream_scenarios import render_completion
from gateway_service.task3_stream_guardrail.pii_patterns import MAX_HOLDBACK, REDACTION_TOKEN
from gateway_service.task3_stream_guardrail.pii_redactor import PiiRedactor
from gateway_service.task3_stream_guardrail.stream_buffer import StreamBuffer


def test_long_output_does_not_grow_the_holdback() -> None:
    buffer = StreamBuffer()
    released = 0
    for _ in range(2_000):
        released += len(buffer.push("Hello world. "))
        assert buffer.buffered_length <= MAX_HOLDBACK
    released += len(buffer.flush())
    assert buffer.buffered_length == 0
    assert released > 10_000


def test_overlong_viable_candidate_fails_closed() -> None:
    local = "a" * 64
    domain = "b" * 63 + "." + "c" * 63 + "." + "d" * 63 + "." + "e" * 63
    candidate = f"{local}@{domain}"
    assert len(candidate) == 320
    buffer = StreamBuffer()
    assert buffer.push(candidate) == ""
    assert buffer.buffered_length == MAX_HOLDBACK
    released = buffer.push("z")
    assert released == REDACTION_TOKEN
    assert buffer.buffered_length <= MAX_HOLDBACK
    assert "a@b" not in released


async def test_concurrent_streams_do_not_share_redaction_state(tmp_path) -> None:
    application = make_app(tmp_path)

    async def one(model: str, content: str) -> bytes:
        async with client_for(application) as client:
            response = await client.post(
                "/v1/chat/completions", json=completion_body(model, content)
            )
        return response.content

    first, second = await asyncio.gather(
        one("mock-chars", "alex@example.com"),
        one("mock-chars", "other@example.org"),
    )
    assert visible_text(first) == REDACTION_TOKEN
    assert visible_text(second) == REDACTION_TOKEN
    assert b"alex" not in first
    assert b"other" not in second
    assert b"@" not in first
    assert b"@" not in second


def test_active_field_cap_fails_closed() -> None:
    from gateway_service.task3_stream_guardrail.pii_redactor import (
        MAX_ACTIVE_FIELDS,
        FieldRedactors,
        ReconstructionError,
    )

    redactors = FieldRedactors()
    for index in range(MAX_ACTIVE_FIELDS):
        assert redactors.feed(f"0:tool:{index}:arguments", "a") == ""
        assert redactors.buffered_length <= MAX_ACTIVE_FIELDS * MAX_HOLDBACK
    try:
        redactors.feed(f"0:tool:{MAX_ACTIVE_FIELDS}:arguments", "a")
    except ReconstructionError as exc:
        assert exc.reason == "field_limit"
    else:
        raise AssertionError("field limit was not enforced")


def test_redactors_are_independent_objects() -> None:
    first = PiiRedactor()
    second = PiiRedactor()
    assert first.feed("alex@example.com") == ""
    assert second.feed("Hello ") == "Hello "
    assert first.finish() == REDACTION_TOKEN
    assert second.finish() == ""


async def test_client_disconnect_closes_the_upstream(tmp_path) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingUpstream:
        def stream(self, payload: dict[str, object]):
            del payload
            return self._iterate()

        async def _iterate(self):
            started.set()
            yield render_completion({"model": "mock-plain", "messages": []})[0]
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    application = make_app(tmp_path, BlockingUpstream())

    async def consume() -> None:
        async with client_for(application) as client:
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json=completion_body("mock-plain"),
            ) as response:
                async for _chunk in response.aiter_bytes():
                    await asyncio.Event().wait()

    request_task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    request_task.cancel()
    try:
        await asyncio.wait_for(request_task, timeout=2)
    except asyncio.CancelledError:
        pass
    await asyncio.wait_for(closed.wait(), timeout=2)
