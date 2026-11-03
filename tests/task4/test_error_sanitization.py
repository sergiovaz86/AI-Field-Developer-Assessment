"""Sanitized completion errors and the tenant authentication boundary."""

from __future__ import annotations

from tests.task3.conftest import completion_body
from tests.task3.conftest import make_app as task3_app
from tests.task4.conftest import (
    AUTH,
    OTHER_AUTH,
    TOKEN,
    ScriptedUpstream,
    client_for,
    completion,
    gateway,
    rows,
)

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.mocks.stream_scenarios import render_completion
from gateway_service.task4_model_router.router import ModelRouter
from gateway_service.task4_model_router.sqlite_store import ReservationLedger


def _frames() -> list[bytes]:
    return render_completion(
        {"model": "mock-plain", "messages": [{"role": "user", "content": "hello"}]}
    )


async def test_missing_and_unknown_credentials_are_401(tmp_path) -> None:
    application, _store, database = gateway(
        tmp_path, ScriptedUpstream(_frames()), ScriptedUpstream()
    )
    async with client_for(application) as client:
        missing = await client.post("/v1/chat/completions", json=completion())
        wrong = await client.post(
            "/v1/chat/completions",
            json=completion(),
            headers={"Authorization": "Bearer not-a-tenant-token"},
        )
        spaced = await client.post(
            "/v1/chat/completions",
            json=completion(),
            headers={"Authorization": "Bearer alpha-token-value extra"},
        )
    for response in (missing, wrong, spaced):
        assert response.status_code == 401
        body = response.json()["error"]
        assert body["code"] == "invalid_api_key"
        assert body["correlation_id"]
        assert response.headers["x-correlation-id"] == body["correlation_id"]
        assert TOKEN not in response.text
        assert "not-a-tenant-token" not in response.text
    assert rows(database) == []


async def test_local_quota_rejection_never_calls_a_provider(tmp_path) -> None:
    primary = ScriptedUpstream(_frames())
    backup = ScriptedUpstream(_frames())
    application, _store, database = gateway(
        tmp_path,
        primary,
        backup,
        quota=1,
        output_tokens=8,
        ceiling=8,
    )
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 429
    body = response.json()["error"]
    assert body["code"] == "quota_exceeded"
    assert body["type"] == "rate_limit_error"
    assert body["correlation_id"]
    assert primary.calls == 0
    assert backup.calls == 0
    assert rows(database) == []
    assert "SELECT" not in response.text
    assert str(database) not in response.text


async def test_output_budget_cannot_be_omitted_or_inflated(tmp_path) -> None:
    primary = ScriptedUpstream(_frames())
    backup = ScriptedUpstream()
    application, _store, database = gateway(
        tmp_path,
        primary,
        backup,
        output_tokens=11,
        ceiling=40,
    )
    async with client_for(application) as client:
        omitted = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
        inflated_body = completion()
        inflated_body["max_tokens"] = 10**9
        inflated = await client.post(
            "/v1/chat/completions",
            json=inflated_body,
            headers=OTHER_AUTH,
        )
        tiny_body = completion()
        tiny_body["max_tokens"] = 1
        tiny = await client.post("/v1/chat/completions", json=tiny_body, headers=AUTH)
    assert omitted.status_code == 200
    assert inflated.status_code == 200
    assert tiny.status_code == 200
    stored = rows(database)
    outputs = sorted(row["reserved_output_tokens"] for row in stored)
    assert outputs == [1, 11, 40]
    assert primary.payloads[0]["max_tokens"] == 11
    assert primary.payloads[1]["max_tokens"] == 40
    assert primary.payloads[2]["max_tokens"] == 1


async def test_invalid_output_limit_does_not_reserve(tmp_path) -> None:
    primary = ScriptedUpstream(_frames())
    application, _store, database = gateway(tmp_path, primary, ScriptedUpstream())
    payload = completion()
    payload["max_tokens"] = True
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=payload, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert primary.calls == 0
    assert rows(database) == []


async def test_missing_schema_is_a_sanitized_503(tmp_path) -> None:
    database = tmp_path / "missing.db"
    primary = ScriptedUpstream(_frames())
    store = ReservationLedger(
        database,
        busy_timeout_ms=5000,
        tokens_per_window=50_000,
        window_ms=60_000,
    )
    application = create_app(
        GatewayConfig(
            database_path=database,
            tenant_bearer_tokens=(("alpha", TOKEN),),
        )
    )
    application.state.model_router = ModelRouter(
        ledger=store,
        clock=lambda: 1_000,
        primary=primary,
        backup=ScriptedUpstream(),
        default_output_tokens=16,
        max_output_tokens=32,
        first_byte_timeout_ms=3000,
    )
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert str(database) not in response.text
    assert "schema_migrations" not in response.text
    assert "Traceback" not in response.text
    assert primary.calls == 0


async def test_tenant_credentials_do_not_apply_to_mcp(tmp_path) -> None:
    application = create_app(
        GatewayConfig(
            database_path=tmp_path / "gateway.db",
            tenant_bearer_tokens=(("alpha", TOKEN),),
            downstream_mcp_url="http://mcp-downstream.test/mcp",
            downstream_mcp_allowlist=("http://mcp-downstream.test/mcp",),
        )
    )
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with client_for(application) as client:
        tenant = await client.post(
            "/mcp",
            json=body,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        viewer = await client.post(
            "/mcp",
            json=body,
            headers={"Authorization": "Bearer viewer-token"},
        )
        health = await client.get("/health")
    assert tenant.status_code == 401
    assert viewer.status_code != 401
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert not (tmp_path / "gateway.db").exists()


async def test_unconfigured_completion_route_stays_unauthenticated(tmp_path) -> None:
    application = task3_app(tmp_path)
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion_body("mock-plain"))
    assert response.status_code == 200
    assert b"[DONE]" in response.content
    assert not (tmp_path / "gateway.db").exists()


async def test_bearer_secret_is_absent_from_the_ledger_file(tmp_path) -> None:
    application, _store, database = gateway(
        tmp_path, ScriptedUpstream(_frames()), ScriptedUpstream()
    )
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200
    blob = database.read_bytes()
    assert TOKEN.encode() not in blob
    assert b"alpha-token-value" not in blob
