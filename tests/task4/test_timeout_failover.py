"""Provider failover, first-byte deadlines, and backup redaction."""

from __future__ import annotations

import time

import httpx
import pytest
from tests.task3.conftest import visible_text
from tests.task4.conftest import (
    AUTH,
    ScriptedUpstream,
    client_for,
    completion,
    gateway,
    rows,
)

from gateway_service.config import GatewayConfig
from gateway_service.mocks.stream_scenarios import render_completion
from gateway_service.providers.base import UpstreamFailure
from gateway_service.providers.openai_compatible import OpenAICompatibleProvider
from gateway_service.task4_model_router.router import ModelRouter


def _frames(model: str) -> list[bytes]:
    return render_completion({"model": model, "messages": [{"role": "user", "content": "hello"}]})


async def test_primary_success_does_not_call_backup(tmp_path) -> None:
    primary = ScriptedUpstream(_frames("mock-plain"))
    backup = ScriptedUpstream(_frames("mock-plain"))
    application, _store, database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"data: [DONE]\n\n" in response.content
    assert primary.calls == 1
    assert backup.calls == 0
    stored = rows(database)
    assert len(stored) == 1
    assert stored[0]["status"] == "settled"
    assert stored[0]["charged_tokens"] == 2


@pytest.mark.parametrize(
    "category",
    ["http_429", "http_5xx", "network"],
)
async def test_eligible_primary_failure_uses_the_backup(tmp_path, category: str) -> None:
    primary = ScriptedUpstream(fail=category)
    backup = ScriptedUpstream(_frames("mock-plain"))
    application, _store, database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    assert b"[DONE]" in response.content
    assert primary.calls == 1
    assert backup.calls == 1
    assert len(rows(database)) == 1


async def test_primary_first_byte_timeout_uses_the_configured_deadline(tmp_path) -> None:
    assert GatewayConfig().first_byte_timeout_ms == 3000
    primary = ScriptedUpstream(hang=True)
    backup = ScriptedUpstream(_frames("mock-plain"))
    application, _store, _database = gateway(
        tmp_path,
        primary,
        backup,
        timeout_ms=3000,
    )
    router = application.state.model_router
    assert isinstance(router, ModelRouter)
    assert router.first_byte_timeout_ms == 3000
    primary_fast = ScriptedUpstream(hang=True)
    backup_fast = ScriptedUpstream(_frames("mock-plain"))
    started = time.monotonic()
    application, _store, _database = gateway(
        tmp_path,
        primary_fast,
        backup_fast,
        timeout_ms=50,
    )
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert primary_fast.calls == 1
    assert backup_fast.calls == 1
    assert primary_fast.closed == 1
    assert elapsed < 2


async def test_backup_first_byte_timeout_is_sanitized(tmp_path) -> None:
    primary = ScriptedUpstream(fail="http_429")
    backup = ScriptedUpstream(hang=True)
    application, _store, database = gateway(tmp_path, primary, backup, timeout_ms=50)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "upstream_timeout"
    assert body["error"]["correlation_id"]
    rendered = response.text
    assert "http://" not in rendered
    assert "alpha-token-value" not in rendered
    assert str(database) not in rendered
    stored = rows(database)
    assert stored[0]["status"] == "released"
    assert stored[0]["charged_tokens"] == 0
    assert backup.closed == 1


async def test_both_providers_failing_is_sanitized(tmp_path) -> None:
    primary = ScriptedUpstream(fail="http_5xx")
    backup = ScriptedUpstream(fail="network")
    application, _store, database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_failure"
    rendered = response.text
    assert "Traceback" not in rendered
    assert "alpha-token-value" not in rendered
    assert "BEGIN" not in rendered
    assert backup.calls == 1


async def test_other_client_errors_do_not_fail_over(tmp_path) -> None:
    primary = ScriptedUpstream(fail="http")
    backup = ScriptedUpstream(_frames("mock-plain"))
    application, _store, _database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 502
    assert backup.calls == 0


async def test_no_failover_after_a_downstream_byte(tmp_path) -> None:
    frames = _frames("mock-plain")
    primary = ScriptedUpstream([frames[0]], fail_after="http_5xx")
    backup = ScriptedUpstream(frames)
    application, _store, database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    assert b"upstream_stream_failure" in response.content
    assert b"[DONE]" not in response.content
    assert backup.calls == 0
    assert rows(database)[0]["status"] == "released"


async def test_failover_charges_one_logical_request(tmp_path) -> None:
    primary = ScriptedUpstream(fail="http_429")
    backup = ScriptedUpstream(_frames("mock-plain"))
    application, _store, database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    stored = rows(database)
    assert len(stored) == 1
    assert stored[0]["charged_tokens"] == 2
    assert stored[0]["actual_input_tokens"] == 1
    assert stored[0]["actual_output_tokens"] == 1


async def test_backup_stream_is_redacted(tmp_path) -> None:
    primary = ScriptedUpstream(fail="network")
    backup = ScriptedUpstream(_frames("mock-email-parts"))
    application, _store, _database = gateway(tmp_path, primary, backup)
    async with client_for(application) as client:
        response = await client.post(
            "/v1/chat/completions",
            json=completion("mock-email-parts"),
            headers=AUTH,
        )
    assert response.status_code == 200
    assert visible_text(response.content) == "[REDACTED]"
    assert b"alex" not in response.content
    assert b"@" not in response.content
    assert b"example.com" not in response.content
    assert backup.calls == 1


async def test_openai_adapter_classifies_429_and_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OpenAICompatibleProvider(
            "http://primary.test",
            "provider-secret",
            timeout_seconds=1,
            client=http_client,
        )
        with pytest.raises(UpstreamFailure) as caught:
            async for _chunk in provider.stream(
                {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
            ):
                pass
    assert caught.value.category == "http_429"
    assert "provider-secret" not in str(caught.value)
    assert "primary.test" not in str(caught.value)


async def test_missing_usage_keeps_the_reservation_on_the_stream(tmp_path) -> None:
    frame = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
        b'"created":0,"model":"mock-plain","choices":[{"index":0,"delta":'
        b'{"content":"Hello"},"finish_reason":null}]}\n\n'
        b"data: [DONE]\n\n"
    )
    primary = ScriptedUpstream([frame])
    backup = ScriptedUpstream()
    application, _store, database = gateway(tmp_path, primary, backup, output_tokens=8)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    stored = rows(database)
    assert stored[0]["status"] == "settled"
    assert stored[0]["actual_input_tokens"] is None
    assert stored[0]["charged_tokens"] == stored[0]["estimated_input_tokens"] + 8
