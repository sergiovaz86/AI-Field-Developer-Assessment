"""Phase 1 gateway scaffold and migration checks.

Assessment flow tests are added when the corresponding tasks are authorized.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.shared.exceptions import ConfigurationError
from scripts.init_database import apply_migration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _gateway_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GATEWAY_")}
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), os.environ.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_phase1_app_does_not_create_database(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    application = create_app(GatewayConfig(database_path=database_path))
    assert application.state.config.database_path == database_path
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_phase1_health_endpoint(tmp_path: Path) -> None:
    application = create_app(GatewayConfig(database_path=tmp_path / "gateway.db"))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://gateway.local") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert not (tmp_path / "gateway.db").exists()


def test_phase1_gateway_check_writes_no_stdout() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "gateway_service", "--check"],
        cwd=PROJECT_ROOT,
        env=_gateway_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.stdout == b""
    assert completed.returncode == 0
    assert b"HTTP server was not started." in completed.stderr
    assert b"admin-token" not in completed.stderr
    assert b"viewer-token" not in completed.stderr


def test_phase1_configuration_rejects_noncanonical_integers() -> None:
    with pytest.raises(ConfigurationError, match="GATEWAY_PORT"):
        GatewayConfig.from_environ({"GATEWAY_PORT": "08080"})


def test_phase1_configuration_omits_secrets() -> None:
    config = GatewayConfig.from_environ({"GATEWAY_PRIMARY_PROVIDER_API_KEY": "super-secret-value"})
    rendered = str(config.redacted_view())
    assert "super-secret-value" not in rendered
    assert config.primary_provider_api_key is not None
    assert config.primary_provider_api_key.get_secret_value() == "super-secret-value"


def test_phase1_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "gateway.db"
    first = apply_migration(database_path, now_ms=1_700_000_000_000)
    second = apply_migration(database_path, now_ms=1_700_000_000_500)
    assert first.recorded is True
    assert second.recorded is False
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        versions = connection.execute(
            "SELECT version, description, applied_at_ms FROM schema_migrations"
        ).fetchall()
        assert versions == [(1, "001_token_rate_limit.sql", 1_700_000_000_000)]
        reservation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(token_reservations)")
        }
        tenant_columns = {row[1] for row in connection.execute("PRAGMA table_info(tenant_quotas)")}
        assert "api_key" not in reservation_columns
        assert "api_key" not in tenant_columns
        assert {"charged_tokens", "request_id", "admitted_at_ms"} <= reservation_columns
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{database_path}{suffix}")
        if sidecar.exists():
            assert sidecar.parent == database_path.parent


def test_phase1_reservation_ledger_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    apply_migration(database_path, now_ms=10)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO tenant_quotas (
                tenant_id, tokens_per_window, window_ms, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?)
            """,
            ("tenant-hash", 50_000, 60_000, 10, 10),
        )
        connection.execute(
            """
            INSERT INTO token_reservations (
                tenant_id, request_id, correlation_id,
                estimated_input_tokens, reserved_output_tokens, charged_tokens,
                status, admitted_at_ms, updated_at_ms, finalized_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tenant-hash", "req-released", "corr-1", 2, 3, 0, "released", 10, 10, 11),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                """
                INSERT INTO token_reservations (
                    tenant_id, request_id, correlation_id,
                    estimated_input_tokens, reserved_output_tokens, charged_tokens,
                    status, admitted_at_ms, updated_at_ms, finalized_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tenant-hash", "req-bad-release", "corr-2", 2, 3, 5, "released", 10, 10, 11),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="request_id"):
            connection.execute(
                """
                INSERT INTO token_reservations (
                    tenant_id, request_id, correlation_id,
                    estimated_input_tokens, reserved_output_tokens, charged_tokens,
                    status, admitted_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("tenant-hash", "req-released", "corr-3", 1, 1, 2, "reserved", 12, 12),
            )
        connection.rollback()
    finally:
        connection.close()
