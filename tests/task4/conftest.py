"""Ledger and ASGI helpers for Task 4."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.providers.base import UpstreamFailure
from gateway_service.task4_model_router.router import ModelRouter
from gateway_service.task4_model_router.sqlite_store import ReservationLedger
from scripts.init_database import apply_migration

TOKEN = "alpha-token-value"
OTHER_TOKEN = "beta-token-value"


def migrate(tmp_path: Path) -> Path:
    database = tmp_path / "gateway.db"
    apply_migration(database, now_ms=1)
    return database


def ledger_for(
    database: Path,
    *,
    quota: int = 50_000,
    window_ms: int = 60_000,
    busy_timeout_ms: int = 5000,
) -> ReservationLedger:
    return ReservationLedger(
        database,
        busy_timeout_ms=busy_timeout_ms,
        tokens_per_window=quota,
        window_ms=window_ms,
    )


def rows(database: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute("SELECT * FROM token_reservations ORDER BY id"))
    finally:
        connection.close()


class ScriptedUpstream:
    """Provider double with a recorded call count and a scripted first byte."""

    def __init__(
        self,
        frames: list[bytes] | None = None,
        *,
        fail: str | None = None,
        hang: bool = False,
        fail_after: str | None = None,
    ) -> None:
        self.frames = list(frames or [])
        self.fail = fail
        self.hang = hang
        self.fail_after = fail_after
        self.calls = 0
        self.closed = 0
        self.payloads: list[dict[str, Any]] = []

    def stream(self, payload: dict[str, Any]):
        self.calls += 1
        self.payloads.append(dict(payload))
        return self._iterate()

    async def _iterate(self):
        try:
            if self.fail is not None:
                raise UpstreamFailure(self.fail)
            if self.hang:
                await asyncio.Event().wait()
            for frame in self.frames:
                yield frame
            if self.fail_after is not None:
                raise UpstreamFailure(self.fail_after)
        finally:
            self.closed += 1

    async def aclose(self) -> None:
        return None


def gateway(
    tmp_path: Path,
    primary: ScriptedUpstream,
    backup: ScriptedUpstream,
    *,
    quota: int = 50_000,
    output_tokens: int = 32,
    ceiling: int = 64,
    timeout_ms: int = 3000,
    clock: Callable[[], int] | None = None,
    tokens: tuple[tuple[str, str], ...] = (("alpha", TOKEN), ("beta", OTHER_TOKEN)),
) -> tuple[Any, ReservationLedger, Path]:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=quota)
    selected_clock = clock if clock is not None else (lambda: 5_000)
    config = GatewayConfig(
        database_path=database,
        tenant_bearer_tokens=tokens,
        default_tokens_per_window=quota,
        default_max_output_tokens=output_tokens,
        max_output_token_budget=ceiling,
        first_byte_timeout_ms=timeout_ms,
    )
    application = create_app(config)
    application.state.model_router = ModelRouter(
        ledger=store,
        clock=selected_clock,
        primary=primary,
        backup=backup,
        default_output_tokens=output_tokens,
        max_output_tokens=ceiling,
        first_byte_timeout_ms=timeout_ms,
    )
    return application, store, database


def client_for(application: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://gateway.test")


def completion(model: str = "mock-plain", content: str = "hello") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": True,
    }


AUTH = {"Authorization": f"Bearer {TOKEN}"}
OTHER_AUTH = {"Authorization": f"Bearer {OTHER_TOKEN}"}
