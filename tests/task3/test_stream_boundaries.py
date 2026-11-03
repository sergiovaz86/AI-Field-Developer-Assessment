"""Chat-completion streaming contract."""

from __future__ import annotations

import json

import httpx
import pytest
from tests.task3.conftest import (
    ScriptedUpstream,
    client_for,
    completion_body,
    make_app,
    read_stream,
    sse_payloads,
    visible_text,
)

from gateway_service.mocks.stream_scenarios import render_completion
from gateway_service.providers.base import UpstreamFailure
from gateway_service.providers.mock_provider import MockChatProvider
from gateway_service.providers.openai_compatible import OpenAICompatibleProvider
from gateway_service.task3_stream_guardrail.pii_patterns import REDACTION_TOKEN


async def test_ordinary_stream_preserves_metadata_and_done(tmp_path) -> None:
    payload = completion_body("mock-plain")
    status, content_type, body = await read_stream(make_app(tmp_path), payload)
    assert status == 200
    assert content_type.startswith("text/event-stream")
    payloads = sse_payloads(body)
    assert payloads[0]["id"] == "chatcmpl-mock"
    assert payloads[0]["object"] == "chat.completion.chunk"
    assert payloads[0]["model"] == "mock-plain"
    assert payloads[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert visible_text(body) == "Hello from the mock provider."
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"
    assert payloads[-1]["usage"]["total_tokens"] == 2
    assert body.endswith(b"data: [DONE]\n\n")


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("mock-email", "Reach me at [REDACTED] today"),
        ("mock-ssn", "SSN [REDACTED] is on file"),
        ("mock-ssn-invalid", "SSN 000-12-3456 stays"),
        ("mock-card", REDACTION_TOKEN),
        ("mock-card-bad", "4111111111111112"),
        ("mock-card-spaces", REDACTION_TOKEN),
        ("mock-card-hyphens", REDACTION_TOKEN),
        ("mock-multi", "[REDACTED] and [REDACTED] and [REDACTED]."),
        ("mock-adjacent", "[REDACTED][REDACTED]"),
        ("mock-prefix-end", "see [REDACTED]"),
        ("mock-utf8", "café "),
    ],
)
async def test_scripted_completions_redact_only_supported_pii(
    tmp_path, model: str, expected: str
) -> None:
    _, _, body = await read_stream(make_app(tmp_path), completion_body(model))
    assert visible_text(body) == expected
    assert b"data: [DONE]\n\n" in body


async def test_crlf_upstream_is_normalized_and_redacted(tmp_path) -> None:
    upstream = MockChatProvider(line_ending="\r\n")
    _, _, body = await read_stream(
        make_app(tmp_path, upstream), completion_body("mock-email-parts")
    )
    assert visible_text(body) == REDACTION_TOKEN
    assert b"\r\n" not in body
    assert b"@" not in body


async def test_single_byte_upstream_fragments_do_not_leak(tmp_path) -> None:
    upstream = MockChatProvider(fragment_bytes=1)
    payload = completion_body("mock-chars", "alex@example.com")
    _, _, body = await read_stream(make_app(tmp_path, upstream), payload)
    assert visible_text(body) == REDACTION_TOKEN
    assert b"@" not in body
    assert upstream.closed_count == 1


async def test_several_events_in_one_chunk_keep_order(tmp_path) -> None:
    frames = b"".join(render_completion({"model": "mock-email", "messages": []}))
    upstream = ScriptedUpstream([frames])
    _, _, body = await read_stream(make_app(tmp_path, upstream), completion_body("mock-email"))
    assert visible_text(body) == "Reach me at [REDACTED] today"
    roles = [item["choices"][0]["delta"].get("role") for item in sse_payloads(body)]
    assert roles[0] == "assistant"


async def test_malformed_first_frame_is_a_json_error(tmp_path) -> None:
    upstream = ScriptedUpstream([b"data: {not-json}\n\n"])
    status, content_type, body = await read_stream(
        make_app(tmp_path, upstream), completion_body("mock-plain")
    )
    assert status == 502
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    assert payload["error"]["code"] == "upstream_failure"
    assert "not-json" not in body.decode()
    assert upstream.closed_count == 1


async def test_midstream_failure_emits_one_error_event_without_done(tmp_path) -> None:
    role = render_completion({"model": "mock-plain", "messages": []})[0]
    upstream = ScriptedUpstream([role], fail="network")
    status, _, body = await read_stream(make_app(tmp_path, upstream), completion_body("mock-plain"))
    assert status == 200
    assert b"assistant" in body
    assert b"upstream_stream_failure" in body
    assert b"[DONE]" not in body
    assert b"Traceback" not in body
    assert upstream.closed_count == 1


async def test_timeout_before_output_is_504(tmp_path) -> None:
    upstream = ScriptedUpstream([], fail="timeout")
    status, _, body = await read_stream(make_app(tmp_path, upstream), completion_body("mock-plain"))
    assert status == 504
    payload = json.loads(body)
    assert payload["error"]["message"] == "The upstream request timed out."
    assert payload["error"]["code"] == "upstream_timeout"
    assert "http" not in payload["error"]["message"]


async def test_stream_false_is_rejected_without_calling_upstream(tmp_path) -> None:
    upstream = ScriptedUpstream([b"data: [DONE]\n\n"])
    status, _, body = await read_stream(
        make_app(tmp_path, upstream), completion_body("mock-plain", stream=False)
    )
    assert status == 400
    assert json.loads(body)["error"]["code"] == "stream_required"
    assert upstream.closed_count == 0


async def test_invalid_json_and_missing_fields_are_400(tmp_path) -> None:
    application = make_app(tmp_path)
    async with client_for(application) as client:
        invalid = await client.post(
            "/v1/chat/completions",
            content=b"not-json",
            headers={"content-type": "application/json"},
        )
        missing = await client.post("/v1/chat/completions", json={"model": "mock-plain"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_json"
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"


def test_unlisted_generated_fields_are_dropped() -> None:
    from gateway_service.task3_stream_guardrail.pii_redactor import FieldRedactors, prepare_chunk

    redactors = FieldRedactors()
    chunk = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "refusal": "alex@example.com",
                    "audio": "alex@example.com",
                    "function_call": {
                        "name": "lookup",
                        "arguments": "alex@example.com",
                        "note": "alex@example.com",
                    },
                },
                "finish_reason": "stop",
            }
        ],
    }
    rendered = json.dumps(prepare_chunk(chunk, redactors))
    assert "alex@" not in rendered
    assert "audio" not in rendered
    assert "note" not in rendered
    assert rendered.count(REDACTION_TOKEN) >= 2


async def test_malformed_tool_index_is_a_sanitized_stream_error(tmp_path) -> None:
    role = render_completion({"model": "mock-plain", "messages": []})[0]
    bad = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{"tool_calls":[{"index":"nope",'
        b'"function":{"arguments":"alex@example.com"}}]},"finish_reason":null}]}\n\n'
    )
    upstream = ScriptedUpstream([role, bad])
    status, _, body = await read_stream(make_app(tmp_path, upstream), completion_body("mock-plain"))
    assert status == 200
    assert b"assistant" in body
    assert b"upstream_stream_failure" in body
    assert b"[DONE]" not in body
    assert b"alex@" not in body
    assert b"nope" not in body
    assert b"ReconstructionError" not in body


async def test_tool_arguments_are_redacted(tmp_path) -> None:
    _, _, body = await read_stream(make_app(tmp_path), completion_body("mock-tool"))
    assert "alex@example.com" not in body.decode()
    assert REDACTION_TOKEN in visible_text(body)
    assert "lookup" in visible_text(body)
    finish = sse_payloads(body)[-1]["choices"][0]["finish_reason"]
    assert finish == "tool_calls"
    assert body.endswith(b"data: [DONE]\n\n")


async def test_openai_adapter_streams_through_the_guard(tmp_path) -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["url"] = str(request.url)
        frames = b"".join(render_completion(json.loads(request.content)))
        return httpx.Response(200, content=frames)

    provider = OpenAICompatibleProvider(
        "http://llm.test/v1",
        "super-secret-value",
        timeout_seconds=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    _, _, body = await read_stream(
        make_app(tmp_path, provider), completion_body("mock-email-parts")
    )
    assert visible_text(body) == REDACTION_TOKEN
    assert captured["authorization"] == "Bearer super-secret-value"
    assert captured["url"] == "http://llm.test/v1/chat/completions"
    assert b"super-secret-value" not in body


async def test_unconfigured_openai_mode_is_sanitized(tmp_path) -> None:
    from gateway_service.config import GatewayConfig
    from gateway_service.main import create_app

    application = create_app(
        GatewayConfig(database_path=tmp_path / "gateway.db", llm_upstream="openai")
    )
    status, _, body = await read_stream(application, completion_body("gpt-test"))
    assert status == 502
    rendered = body.decode()
    assert "super-secret" not in rendered
    assert "http" not in rendered
    assert "gateway.db" not in rendered


async def test_upstream_http_error_hides_the_response_body(tmp_path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"secret.internal token=abc")

    provider = OpenAICompatibleProvider(
        "http://llm.test/v1",
        "super-secret-value",
        timeout_seconds=1,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    status, _, body = await read_stream(make_app(tmp_path, provider), completion_body("mock-plain"))
    assert status == 502
    assert b"secret.internal" not in body
    assert b"super-secret-value" not in body


async def test_provider_timeout_exception_maps_to_504(tmp_path) -> None:
    class TimeoutUpstream:
        def stream(self, payload: dict[str, object]):
            del payload
            return self._stream()

        async def _stream(self):
            raise UpstreamFailure("timeout")
            yield b""

        async def aclose(self) -> None:
            return None

    status, _, body = await read_stream(
        make_app(tmp_path, TimeoutUpstream()), completion_body("mock-plain")
    )
    assert status == 504
    assert b"Timeout" not in body


def test_adapter_timeout_category_has_no_url() -> None:
    error = UpstreamFailure("timeout")
    assert str(error) == "timeout"
    assert "http" not in str(error)
