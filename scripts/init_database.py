"""Apply database/migrations/001_token_rate_limit.sql.

The SQL file owns DDL and the pragmas it contains. This runner records
schema version 1 after that script commits. It does not admit requests or
reconcile token usage.

Run from the project root so relative database paths match the gateway.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway_service.config import GatewayConfig  # noqa: E402
from gateway_service.shared.exceptions import ConfigurationError  # noqa: E402

MIGRATION_VERSION = 1
MIGRATION_DESCRIPTION = "001_token_rate_limit.sql"
MIGRATION_PATH = ROOT / "database" / "migrations" / "001_token_rate_limit.sql"


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of one migration attempt."""

    database_path: Path
    version: int
    recorded: bool


def current_time_ms() -> int:
    """Return the wall clock as integer Unix milliseconds."""
    return time.time_ns() // 1_000_000


def apply_migration(database_path: Path, *, now_ms: int) -> MigrationResult:
    """Apply the approved migration and record version 1 once.

    ``now_ms`` is the injected clock written to schema_migrations.applied_at_ms.
    Concurrent migrators are not supported. The version primary key rejects a
    duplicate insert if two runners race.
    """
    if now_ms < 0:
        raise ValueError("now_ms must be >= 0")
    if not MIGRATION_PATH.is_file():
        raise FileNotFoundError(f"migration file is missing: {MIGRATION_PATH.name}")
    script = MIGRATION_PATH.read_text(encoding="utf-8")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(script)
        if connection.in_transaction:
            raise RuntimeError("migration script left a transaction open")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != "wal":
            raise RuntimeError("migration did not leave the database in WAL mode")
        if busy_timeout is None or int(busy_timeout[0]) != 5000:
            raise RuntimeError("migration did not set the approved busy timeout")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        recorded = existing is None
        if recorded:
            connection.execute(
                """
                INSERT INTO schema_migrations (version, description, applied_at_ms)
                VALUES (?, ?, ?)
                """,
                (MIGRATION_VERSION, MIGRATION_DESCRIPTION, now_ms),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return MigrationResult(
        database_path=database_path,
        version=MIGRATION_VERSION,
        recorded=recorded,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply the gateway SQLite migration.")
    parser.add_argument(
        "--database",
        type=Path,
        help="Database path. Defaults to GATEWAY_DATABASE_PATH or .runtime/gateway.db.",
    )
    parser.add_argument(
        "--now-ms",
        type=int,
        help="Injected Unix millisecond timestamp for the migration version row.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Apply the migration and write a one-line result to stderr."""
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        database_path = (
            args.database
            if args.database is not None
            else GatewayConfig.from_environ().database_path
        )
        now_ms = current_time_ms() if args.now_ms is None else args.now_ms
        result = apply_migration(database_path, now_ms=now_ms)
    except (ConfigurationError, OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"migration failed: {exc}\n")
        raise SystemExit(1) from None
    state = "recorded" if result.recorded else "already recorded"
    sys.stderr.write(f"migration version {result.version} {state} at {result.database_path}\n")


if __name__ == "__main__":
    main()
