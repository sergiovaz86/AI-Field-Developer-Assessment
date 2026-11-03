"""SQLite reservation ledger.

Each operation opens its own connection, enables foreign keys, sets the
configured busy timeout, and closes the connection before returning. Quota
admission, reconciliation, and release each run inside one ``BEGIN IMMEDIATE``
transaction. Process-local locks are not the admission mutex. An asyncio lock
only serializes the one-time restart scan.

Recovery releases only rows still in ``reserved``. Those rows never reached
the provider because the in-flight commit happens before the socket opens.
Rows in ``in_flight`` or ``timed_out`` stay charged. Their completion or
teardown cannot be established after a crash, so releasing them would hide
usage. That fail-closed charge can outlive the rolling window and can consume
quota until an operator reconciles it. WAL mode is a local-disk journal. It
is not a shared-network filesystem protocol.

A reported usage total replaces the reserved charge in the same transaction,
including when that total is above the tenant quota. Later admissions stay
closed while the rolling sum exceeds the quota. A stream that never reports
usage is settled at the full reservation with the actual columns left null.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

LOGGER = logging.getLogger("gateway.ledger")

_ACTIVE = ("reserved", "in_flight", "timed_out")
Terminal = Literal["settled", "released"]


class LedgerError(Exception):
    """Sanitized ledger failure. The message is a stable category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class QuotaRejected(LedgerError):
    """The new reservation would exceed the tenant window."""

    def __init__(self) -> None:
        super().__init__("quota")


@dataclass(frozen=True)
class Reservation:
    request_id: str
    tenant_id: str
    charged_tokens: int
    status: str
    admitted_at_ms: int
    estimated_input_tokens: int
    reserved_output_tokens: int
    actual_input_tokens: int | None
    actual_output_tokens: int | None


class ReservationLedger:
    """On-disk token ledger backed by the approved migration schema."""

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int,
        tokens_per_window: int,
        window_ms: int,
    ) -> None:
        self._path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._tokens_per_window = tokens_per_window
        self._window_ms = window_ms
        self._recovered = False
        self._recover_lock = asyncio.Lock()

    @property
    def window_ms(self) -> int:
        return self._window_ms

    async def recover_once(self, now_ms: int) -> int:
        """Release unstarted reservations. Return how many rows changed."""
        if self._recovered:
            return 0
        async with self._recover_lock:
            if self._recovered:
                return 0
            changed = await self._recover(now_ms)
            self._recovered = True
            return changed

    async def admit(
        self,
        *,
        tenant_id: str,
        request_id: str,
        correlation_id: str,
        estimated_input_tokens: int,
        reserved_output_tokens: int,
        now_ms: int,
        status: str = "reserved",
    ) -> Reservation:
        """Reserve input plus output budget, or reject without writing a row."""
        requested = estimated_input_tokens + reserved_output_tokens
        if (
            requested < 1
            or estimated_input_tokens < 0
            or reserved_output_tokens < 0
            or status not in {"reserved", "in_flight"}
        ):
            raise LedgerError("invalid_reservation")
        cutoff = now_ms - self._window_ms
        async with self._transaction() as db:
            await db.execute(
                """
                INSERT INTO tenant_quotas (
                    tenant_id, tokens_per_window, window_ms, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO NOTHING
                """,
                (
                    tenant_id,
                    self._tokens_per_window,
                    self._window_ms,
                    now_ms,
                    now_ms,
                ),
            )
            quota_row = await (
                await db.execute(
                    "SELECT tokens_per_window FROM tenant_quotas WHERE tenant_id = ?",
                    (tenant_id,),
                )
            ).fetchone()
            if quota_row is None:
                raise LedgerError("schema")
            quota = int(quota_row[0])
            used_row = await (await db.execute(_SUM_SQL, (tenant_id, cutoff))).fetchone()
            used = int(used_row[0] if used_row is not None else 0)
            if used + requested > quota:
                raise QuotaRejected
            await db.execute(
                """
                INSERT INTO token_reservations (
                    request_id, tenant_id, correlation_id,
                    estimated_input_tokens, reserved_output_tokens, charged_tokens,
                    status, admitted_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    tenant_id,
                    correlation_id,
                    estimated_input_tokens,
                    reserved_output_tokens,
                    requested,
                    status,
                    now_ms,
                    now_ms,
                ),
            )
        loaded = await self.get(request_id)
        if loaded is None:
            raise LedgerError("schema")
        return loaded

    async def mark_in_flight(self, request_id: str, now_ms: int) -> None:
        """Commit the provider-started state before any upstream socket opens."""
        await self._transition(request_id, now_ms, "in_flight", charged=None)

    async def mark_timed_out(self, request_id: str, now_ms: int) -> None:
        """Keep the charge and record that teardown is not yet confirmed."""
        await self._transition(request_id, now_ms, "timed_out", charged=None)

    async def settle(
        self,
        request_id: str,
        now_ms: int,
        actual_input_tokens: int | None,
        actual_output_tokens: int | None,
    ) -> Reservation | None:
        """Reconcile one logical request. A second call leaves the row unchanged."""
        async with self._transaction() as db:
            current = await self._lock_row(db, request_id)
            if current is None:
                return None
            if current.status in {"settled", "released"}:
                return current
            charged = current.charged_tokens
            stored_input = actual_input_tokens
            stored_output = actual_output_tokens
            if actual_input_tokens is None or actual_output_tokens is None:
                stored_input = None
                stored_output = None
            else:
                desired = actual_input_tokens + actual_output_tokens
                if desired < 0:
                    raise LedgerError("invalid_usage")
                charged = desired
            await db.execute(
                """
                UPDATE token_reservations
                SET status = 'settled',
                    charged_tokens = ?,
                    actual_input_tokens = ?,
                    actual_output_tokens = ?,
                    updated_at_ms = ?,
                    finalized_at_ms = ?
                WHERE request_id = ?
                  AND status IN ('reserved', 'in_flight', 'timed_out')
                """,
                (
                    charged,
                    stored_input,
                    stored_output,
                    now_ms,
                    now_ms,
                    request_id,
                ),
            )
        return await self.get(request_id)

    async def release(self, request_id: str, now_ms: int) -> Reservation | None:
        """Drop the charge after confirmed teardown. A settled row stays settled."""
        async with self._transaction() as db:
            current = await self._lock_row(db, request_id)
            if current is None or current.status in {"settled", "released"}:
                return current
            await db.execute(
                """
                UPDATE token_reservations
                SET status = 'released',
                    charged_tokens = 0,
                    updated_at_ms = ?,
                    finalized_at_ms = ?
                WHERE request_id = ?
                  AND status IN ('reserved', 'in_flight', 'timed_out')
                """,
                (now_ms, now_ms, request_id),
            )
        return await self.get(request_id)

    async def get(self, request_id: str) -> Reservation | None:
        async with self._connection() as db:
            row = await (await db.execute(_SELECT_SQL, (request_id,))).fetchone()
        if row is None:
            return None
        return _reservation(row)

    async def charged_total(self, tenant_id: str, now_ms: int) -> int:
        cutoff = now_ms - self._window_ms
        async with self._connection() as db:
            row = await (await db.execute(_SUM_SQL, (tenant_id, cutoff))).fetchone()
        return int(row[0] if row is not None else 0)

    async def count_rows(self, tenant_id: str) -> int:
        async with self._connection() as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM token_reservations WHERE tenant_id = ?",
                    (tenant_id,),
                )
            ).fetchone()
        return int(row[0] if row is not None else 0)

    async def pragma_probe(self) -> tuple[int, int, str]:
        """Return foreign_keys, busy_timeout, and journal_mode for this connection."""
        async with self._connection() as db:
            foreign_keys = await (await db.execute("PRAGMA foreign_keys")).fetchone()
            busy = await (await db.execute("PRAGMA busy_timeout")).fetchone()
            journal = await (await db.execute("PRAGMA journal_mode")).fetchone()
        return (
            int(foreign_keys[0] if foreign_keys else 0),
            int(busy[0] if busy else 0),
            str(journal[0] if journal else ""),
        )

    async def _recover(self, now_ms: int) -> int:
        async with self._transaction() as db:
            cursor = await db.execute(
                """
                UPDATE token_reservations
                SET status = 'released',
                    charged_tokens = 0,
                    updated_at_ms = ?,
                    finalized_at_ms = ?
                WHERE status = 'reserved'
                """,
                (now_ms, now_ms),
            )
            return int(cursor.rowcount if cursor.rowcount is not None else 0)

    async def _transition(
        self,
        request_id: str,
        now_ms: int,
        status: str,
        *,
        charged: int | None,
    ) -> None:
        async with self._transaction() as db:
            current = await self._lock_row(db, request_id)
            if current is None or current.status not in _ACTIVE:
                return
            if now_ms < current.admitted_at_ms:
                raise LedgerError("clock")
            await db.execute(
                """
                UPDATE token_reservations
                SET status = ?,
                    updated_at_ms = ?
                WHERE request_id = ?
                  AND status IN ('reserved', 'in_flight', 'timed_out')
                """,
                (status, now_ms, request_id),
            )
            if charged is not None:
                await db.execute(
                    """
                    UPDATE token_reservations
                    SET charged_tokens = ?
                    WHERE request_id = ? AND status = ?
                    """,
                    (charged, request_id, status),
                )

    async def _lock_row(self, db: aiosqlite.Connection, request_id: str) -> Reservation | None:
        row = await (await db.execute(_SELECT_SQL, (request_id,))).fetchone()
        if row is None:
            return None
        return _reservation(row)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                yield db
                await db.execute("COMMIT")
            except QuotaRejected:
                await _rollback(db)
                raise
            except LedgerError:
                await _rollback(db)
                raise
            except Exception as exc:
                await _rollback(db)
                LOGGER.info("ledger operation failed type=%s", type(exc).__name__)
                raise LedgerError("database") from None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._busy_timeout_ms < 0:
            raise LedgerError("schema")
        db = await aiosqlite.connect(self._path, isolation_level=None)
        try:
            await _require_schema(db)
            await _apply_pragmas(db, self._busy_timeout_ms)
            yield db
        finally:
            await db.close()


_SUM_SQL = """
SELECT COALESCE(SUM(charged_tokens), 0)
FROM token_reservations
WHERE tenant_id = ?
  AND (
        admitted_at_ms > ?
        OR status IN ('reserved', 'in_flight', 'timed_out')
      )
"""

_SELECT_SQL = """
SELECT request_id, tenant_id, charged_tokens, status, admitted_at_ms,
       estimated_input_tokens, reserved_output_tokens,
       actual_input_tokens, actual_output_tokens
FROM token_reservations
WHERE request_id = ?
"""


def _reservation(row: tuple[object, ...]) -> Reservation:
    return Reservation(
        request_id=str(row[0]),
        tenant_id=str(row[1]),
        charged_tokens=int(row[2]),
        status=str(row[3]),
        admitted_at_ms=int(row[4]),
        estimated_input_tokens=int(row[5]),
        reserved_output_tokens=int(row[6]),
        actual_input_tokens=None if row[7] is None else int(row[7]),
        actual_output_tokens=None if row[8] is None else int(row[8]),
    )


async def _apply_pragmas(db: aiosqlite.Connection, busy_timeout_ms: int) -> None:
    await (await db.execute("PRAGMA foreign_keys = ON")).fetchone()
    await (await db.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")).fetchone()
    await (await db.execute("PRAGMA journal_mode = WAL")).fetchone()


async def _require_schema(db: aiosqlite.Connection) -> None:
    table = await (
        await db.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        )
    ).fetchone()
    if table is None:
        raise LedgerError("schema")
    version = await (
        await db.execute("SELECT version FROM schema_migrations WHERE version = 1")
    ).fetchone()
    if version is None:
        raise LedgerError("schema")


async def _rollback(db: aiosqlite.Connection) -> None:
    try:
        await db.execute("ROLLBACK")
    except Exception:
        LOGGER.info("ledger rollback failed")
