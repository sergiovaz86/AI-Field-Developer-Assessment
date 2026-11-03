"""Operating mode, full usage charges, response-byte timeout, and teardown."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from tests.task4.conftest import (
    AUTH,
    ScriptedUpstream,
    client_for,
    completion,
    gateway,
    ledger_for,
    migrate,
    rows,
)

from gateway_service.config import GatewayConfig, require_initialized_database
from gateway_service.main import create_app
from gateway_service.providers.openai_compatible import OpenAICompatibleProvider
from gateway_service.shared.exceptions import ConfigurationError
from gateway_service.task4_model_router.router import drain_ledger_cleanup
from gateway_service.task4_model_router.sqlite_store import LedgerError
from scripts.init_database import apply_migration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _production(**overrides: object) -> GatewayConfig:
    payload: dict[str, object] = {
        "gateway_mode": "production",
        "tenant_bearer_tokens": (("alpha", "alpha-token-value"),),
        "llm_upstream": "openai",
        "primary_provider_base_url": "https://primary.example",
        "backup_provider_base_url": "https://backup.example",
        "primary_provider_api_key": SecretStr("primary-secret"),
        "backup_provider_api_key": SecretStr("backup-secret"),
    }
    payload.update(overrides)
    return GatewayConfig(**payload)  # type: ignore[arg-type]


def test_assessment_mock_is_the_default_mode() -> None:
    config = GatewayConfig.from_environ({})
    assert config.gateway_mode == "assessment_mock"
    require_initialized_database(config)


def test_production_rejects_missing_security_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        GatewayConfig(gateway_mode="production", database_path=tmp_path / "gateway.db")
    with pytest.raises(ValidationError):
        _production(backup_provider_api_key=None, llm_upstream="openai")
    with pytest.raises(ConfigurationError, match="gateway_mode") as caught:
        GatewayConfig.from_environ({"GATEWAY_MODE": "production"})
    assert "primary-secret" not in str(caught.value)
    assert "alpha-token-value" not in str(caught.value)


def test_production_startup_requires_an_initialized_ledger(tmp_path: Path) -> None:
    missing = tmp_path / "gateway.db"
    config = _production(database_path=missing)
    with pytest.raises(ConfigurationError, match="database_path"):
        require_initialized_database(config)
    assert not missing.exists()
    apply_migration(missing, now_ms=1)
    require_initialized_database(config)
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    with pytest.raises(ConfigurationError, match="database_path"):
        require_initialized_database(_production(database_path=empty))


def test_production_check_does_not_bind_or_echo_secrets() -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GATEWAY_")}
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GATEWAY_MODE"] = "production"
    completed = subprocess.run(
        [sys.executable, "-m", "gateway_service", "--check"],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 1
    assert completed.stdout == b""
    assert b"HTTP server was not started." not in completed.stderr
    assert b"primary-secret" not in completed.stderr
    assert b"alpha-token-value" not in completed.stderr


def test_provider_attempt_sees_an_in_flight_reservation(tmp_path: Path) -> None:
    database = migrate(tmp_path)
    seen: list[str] = []

    class Probe(ScriptedUpstream):
        def stream(self, payload: dict[str, object]):
            seen.extend(row["status"] for row in rows(database))
            return super().stream(payload)

    frames = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{"role":"assistant"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    application, _store, _database = gateway(tmp_path, Probe([frames]), ScriptedUpstream())
    asyncio.run(_post(application))
    assert seen == ["in_flight"]


async def _post(application: object) -> None:
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=completion(), headers=AUTH)
    assert response.status_code == 200


def test_restart_keeps_timed_out_and_in_flight_charges(tmp_path: Path) -> None:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=100)
    from gateway_service.task4_model_router.rate_limiter import tenant_hash

    tenant = tenant_hash("alpha")
    asyncio.run(_seed_uncertain(store, tenant))
    restarted = ledger_for(database, quota=100)
    changed = asyncio.run(restarted.recover_once(50_000))
    assert changed == 1
    reserved = asyncio.run(restarted.get("req-reserved"))
    flight = asyncio.run(restarted.get("req-flight"))
    timed_out = asyncio.run(restarted.get("req-timeout"))
    assert reserved is not None and reserved.status == "released"
    assert flight is not None and flight.status == "in_flight"
    assert flight.charged_tokens == 10
    assert timed_out is not None and timed_out.status == "timed_out"
    assert timed_out.charged_tokens == 7
    assert asyncio.run(restarted.charged_total(tenant, 500_000)) == 17


async def _seed_uncertain(store: object, tenant: str) -> None:
    await store.admit(  # type: ignore[attr-defined]
        tenant_id=tenant,
        request_id="req-reserved",
        correlation_id="corr-reserved",
        estimated_input_tokens=4,
        reserved_output_tokens=0,
        now_ms=1_000,
    )
    await store.admit(  # type: ignore[attr-defined]
        tenant_id=tenant,
        request_id="req-flight",
        correlation_id="corr-flight",
        estimated_input_tokens=10,
        reserved_output_tokens=0,
        now_ms=1_000,
        status="in_flight",
    )
    await store.admit(  # type: ignore[attr-defined]
        tenant_id=tenant,
        request_id="req-timeout",
        correlation_id="corr-timeout",
        estimated_input_tokens=7,
        reserved_output_tokens=0,
        now_ms=1_000,
        status="in_flight",
    )
    await store.mark_timed_out("req-timeout", 1_100)  # type: ignore[attr-defined]


async def test_finalize_retries_then_records_the_release(tmp_path: Path) -> None:
    from gateway_service.task4_model_router.router import CompletionSession

    database = migrate(tmp_path)
    store = ledger_for(database, quota=50)

    class FlakyLedger:
        def __init__(self) -> None:
            self._remaining = 2

        async def release(self, request_id: str, now_ms: int):
            if self._remaining:
                self._remaining -= 1
                raise LedgerError("database")
            return await store.release(request_id, now_ms)

        async def settle(self, *args: object, **kwargs: object):
            raise AssertionError((args, kwargs))

    session = CompletionSession(
        ledger=FlakyLedger(),  # type: ignore[arg-type]
        clock=lambda: 2_000,
        primary=ScriptedUpstream(),
        backup=None,
        payload={},
        request_id="req-retry",
        correlation_id="corr-retry",
        first_byte_timeout_s=3,
    )
    await store.admit(
        tenant_id="tenant",
        request_id="req-retry",
        correlation_id="corr-retry",
        estimated_input_tokens=5,
        reserved_output_tokens=0,
        now_ms=2_000,
        status="in_flight",
    )
    await session.finish(failed=True)
    released = await store.get("req-retry")
    assert released is not None
    assert released.status == "released"
    assert released.charged_tokens == 0


async def test_shutdown_drain_waits_for_a_detached_release(tmp_path: Path) -> None:
    from gateway_service.task4_model_router.router import _spawn

    database = migrate(tmp_path)
    store = ledger_for(database, quota=50)
    started = asyncio.Event()
    release = asyncio.Event()

    async def write() -> None:
        started.set()
        await release.wait()
        await store.release("req-drain", 3_000)

    await store.admit(
        tenant_id="tenant",
        request_id="req-drain",
        correlation_id="corr-drain",
        estimated_input_tokens=6,
        reserved_output_tokens=0,
        now_ms=3_000,
        status="in_flight",
    )
    _spawn(write())
    await started.wait()
    current = await store.get("req-drain")
    assert current is not None and current.status == "in_flight"
    release.set()
    await drain_ledger_cleanup()
    finished = await store.get("req-drain")
    assert finished is not None
    assert finished.status == "released"
    assert finished.charged_tokens == 0


async def test_headers_satisfy_the_deadline_before_the_body(tmp_path: Path) -> None:
    headers_seen = asyncio.Event()
    body_entered = asyncio.Event()
    release_body = asyncio.Event()
    frame = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{"content":"Hi"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    class HeldBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:  # type: ignore[override]
            body_entered.set()
            await release_body.wait()
            yield frame

    def handler(_request: httpx.Request) -> httpx.Response:
        headers_seen.set()
        return httpx.Response(200, stream=HeldBody())

    backup = ScriptedUpstream([frame])
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0)) as http_client:
        primary = OpenAICompatibleProvider(
            "https://primary.example",
            "provider-secret",
            timeout_seconds=30,
            header_timeout_seconds=0.05,
            client=http_client,
        )
        application, _store, _database = gateway(
            tmp_path,
            primary,  # type: ignore[arg-type]
            backup,
            timeout_ms=50,
        )

        async def issue() -> httpx.Response:
            async with client_for(application) as client:
                return await client.post(
                    "/v1/chat/completions",
                    json=completion(),
                    headers=AUTH,
                )

        request_task = asyncio.create_task(issue())
        await asyncio.wait_for(headers_seen.wait(), 1)
        await asyncio.wait_for(body_entered.wait(), 1)
        await asyncio.sleep(0.08)
        assert backup.calls == 0
        assert not request_task.done()
        release_body.set()
        response = await request_task
    assert response.status_code == 200
    assert b"Hi" in response.content
    assert b"[DONE]" in response.content
    assert backup.calls == 0
    assert "provider-secret" not in response.text


async def test_partial_frame_and_delayed_continuation_stay_on_the_primary(tmp_path: Path) -> None:
    release_rest = asyncio.Event()
    paused = asyncio.Event()
    head = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{"content":"Hi"}'
    )
    tail = (
        b',"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}\n\n'
        b"data: [DONE]\n\n"
    )

    class SplitBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:  # type: ignore[override]
            yield head
            paused.set()
            await release_rest.wait()
            yield tail

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SplitBody())

    backup = ScriptedUpstream()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(5.0)) as http_client:
        primary = OpenAICompatibleProvider(
            "https://primary.example",
            "provider-secret",
            timeout_seconds=30,
            header_timeout_seconds=0.05,
            client=http_client,
        )
        application, _store, database = gateway(
            tmp_path,
            primary,  # type: ignore[arg-type]
            backup,
            timeout_ms=50,
        )

        async def issue() -> httpx.Response:
            async with client_for(application) as client:
                return await client.post(
                    "/v1/chat/completions",
                    json=completion(),
                    headers=AUTH,
                )

        request_task = asyncio.create_task(issue())
        await asyncio.wait_for(paused.wait(), 1)
        await asyncio.sleep(0.08)
        assert backup.calls == 0
        assert not request_task.done()
        release_rest.set()
        response = await request_task
    assert response.status_code == 200
    assert b"Hi" in response.content
    assert b"[DONE]" in response.content
    assert backup.calls == 0
    stored = rows(database)
    assert stored[0]["charged_tokens"] == 5
    assert stored[0]["actual_input_tokens"] == 2
    assert stored[0]["actual_output_tokens"] == 3


async def test_asgi_lifespan_rejects_a_missing_production_ledger(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    application = create_app(_production(database_path=database))
    with pytest.raises(ConfigurationError, match="database_path"):
        async with application.router.lifespan_context(application):
            pass
    assert not database.exists()


async def test_asgi_lifespan_accepts_an_initialized_ledger_without_logging_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = migrate(tmp_path)
    application = create_app(_production(database_path=database))
    with caplog.at_level(logging.DEBUG):
        async with application.router.lifespan_context(application):
            assert application.state.config.gateway_mode == "production"
    rendered = caplog.text
    assert "primary-secret" not in rendered
    assert "backup-secret" not in rendered
    assert "alpha-token-value" not in rendered


def test_production_check_redacts_a_complete_configuration(tmp_path: Path) -> None:
    database = tmp_path / "gateway.db"
    apply_migration(database, now_ms=1)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GATEWAY_")}
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(
        {
            "GATEWAY_MODE": "production",
            "GATEWAY_DATABASE_PATH": str(database),
            "GATEWAY_LLM_UPSTREAM": "openai",
            "GATEWAY_TENANT_TOKENS": "alpha=alpha-token-value",
            "GATEWAY_PRIMARY_PROVIDER_BASE_URL": "https://primary.example",
            "GATEWAY_BACKUP_PROVIDER_BASE_URL": "https://backup.example",
            "GATEWAY_PRIMARY_PROVIDER_API_KEY": "primary-secret",
            "GATEWAY_BACKUP_PROVIDER_API_KEY": "backup-secret",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "gateway_service", "--check"],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert b"HTTP server was not started." in completed.stderr
    assert b"production" in completed.stderr
    assert b"primary-secret" not in completed.stderr
    assert b"backup-secret" not in completed.stderr
    assert b"alpha-token-value" not in completed.stderr


def test_asgi_factory_refuses_incomplete_production(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GATEWAY_")}
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GATEWAY_MODE"] = "production"
    env["GATEWAY_DATABASE_PATH"] = str(tmp_path / "gateway.db")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway_service.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode != 0
    assert not (tmp_path / "gateway.db").exists()
    combined = completed.stdout + completed.stderr
    assert b"gateway configuration is invalid: gateway_mode" in combined
    assert b"alpha-token-value" not in combined
    assert b"primary-secret" not in combined
    assert b"Uvicorn running" not in combined


def test_production_completion_requires_the_tenant_bearer(tmp_path: Path) -> None:
    database = migrate(tmp_path)
    config = _production(database_path=database)
    application = create_app(config)
    frame = (
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{"content":"Hi"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"id":"chatcmpl-mock","object":"chat.completion.chunk","created":0,'
        b'"model":"mock-plain","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    routed = gateway(tmp_path, ScriptedUpstream([frame]), ScriptedUpstream())
    application.state.model_router = routed[0].state.model_router

    async def run() -> tuple[int, int]:
        async with client_for(application) as client:
            missing = await client.post("/v1/chat/completions", json=completion())
            authed = await client.post(
                "/v1/chat/completions",
                json=completion(),
                headers=AUTH,
            )
        return missing.status_code, authed.status_code

    missing, authed = asyncio.run(run())
    assert missing == 401
    assert authed == 200
