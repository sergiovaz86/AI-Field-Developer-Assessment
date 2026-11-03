"""Concurrent admission and reservation lifecycle."""

from __future__ import annotations

import asyncio

import pytest
from tests.task4.conftest import ledger_for, migrate, rows

from gateway_service.task4_model_router.rate_limiter import tenant_hash
from gateway_service.task4_model_router.sqlite_store import LedgerError, QuotaRejected

TENANT = tenant_hash("alpha")


async def test_concurrent_admissions_serialize_on_the_database(tmp_path) -> None:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=100)

    async def once(number: int):
        return await store.admit(
            tenant_id=TENANT,
            request_id=f"req-{number}",
            correlation_id=f"corr-{number}",
            estimated_input_tokens=60,
            reserved_output_tokens=0,
            now_ms=1_000,
        )

    outcomes = await asyncio.gather(once(1), once(2), return_exceptions=True)
    admitted = [item for item in outcomes if not isinstance(item, Exception)]
    rejected = [item for item in outcomes if isinstance(item, QuotaRejected)]
    assert len(admitted) == 1
    assert len(rejected) == 1
    assert await store.charged_total(TENANT, 1_000) == 60


async def test_independent_connections_admit_concurrently(tmp_path) -> None:
    database = migrate(tmp_path)
    left = ledger_for(database, quota=100)
    right = ledger_for(database, quota=100)

    async def once(store, number: int):
        return await store.admit(
            tenant_id=TENANT,
            request_id=f"conn-{number}",
            correlation_id=f"corr-{number}",
            estimated_input_tokens=60,
            reserved_output_tokens=0,
            now_ms=2_000,
        )

    outcomes = await asyncio.gather(
        once(left, 1),
        once(right, 2),
        return_exceptions=True,
    )
    assert sum(isinstance(item, QuotaRejected) for item in outcomes) == 1
    assert await left.charged_total(TENANT, 2_000) == 60
    assert await right.count_rows(TENANT) == 1


async def test_reopen_and_restart_keep_in_flight_charges(tmp_path) -> None:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=50_000)
    await store.admit(
        tenant_id=TENANT,
        request_id="req-keep",
        correlation_id="corr-keep",
        estimated_input_tokens=12,
        reserved_output_tokens=3,
        now_ms=3_000,
    )
    await store.mark_in_flight("req-keep", 3_000)
    reopened = ledger_for(database, quota=50_000)
    assert await reopened.charged_total(TENANT, 3_000) == 15
    code = (
        "import asyncio, sys\n"
        "from pathlib import Path\n"
        "from gateway_service.task4_model_router.sqlite_store import ReservationLedger\n"
        "async def main():\n"
        "    store = ReservationLedger(Path(sys.argv[1]), busy_timeout_ms=5000,\n"
        "        tokens_per_window=50000, window_ms=60000)\n"
        "    print(await store.charged_total(sys.argv[2], int(sys.argv[3])))\n"
        "asyncio.run(main())\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys_executable(),
        "-c",
        code,
        str(database),
        TENANT,
        "3000",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert stdout.decode().strip() == "15"


async def test_recovery_releases_only_unstarted_rows(tmp_path) -> None:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=50_000)
    await store.admit(
        tenant_id=TENANT,
        request_id="req-reserved",
        correlation_id="corr-reserved",
        estimated_input_tokens=9,
        reserved_output_tokens=1,
        now_ms=4_000,
    )
    await store.admit(
        tenant_id=TENANT,
        request_id="req-flight",
        correlation_id="corr-flight",
        estimated_input_tokens=8,
        reserved_output_tokens=2,
        now_ms=4_000,
    )
    await store.mark_in_flight("req-flight", 4_000)
    restarted = ledger_for(database, quota=50_000)
    changed = await restarted.recover_once(5_000)
    assert changed == 1
    reserved = await restarted.get("req-reserved")
    flight = await restarted.get("req-flight")
    assert reserved is not None and reserved.status == "released"
    assert reserved.charged_tokens == 0
    assert flight is not None and flight.status == "in_flight"
    assert flight.charged_tokens == 10
    assert await restarted.charged_total(TENANT, 200_000) == 10
    assert await restarted.recover_once(6_000) == 0


async def test_lifecycle_transitions_are_atomic_and_ordered(tmp_path) -> None:
    database = migrate(tmp_path)
    store = ledger_for(database, quota=50_000)
    await store.admit(
        tenant_id=TENANT,
        request_id="req-life",
        correlation_id="corr-life",
        estimated_input_tokens=6,
        reserved_output_tokens=4,
        now_ms=7_000,
    )
    await store.mark_in_flight("req-life", 7_000)
    await store.mark_timed_out("req-life", 7_100)
    timed_out = await store.get("req-life")
    assert timed_out is not None
    assert timed_out.status == "timed_out"
    assert timed_out.charged_tokens == 10
    assert timed_out.actual_input_tokens is None
    released = await store.release("req-life", 7_200)
    assert released is not None
    assert released.status == "released"
    assert released.charged_tokens == 0
    stored = rows(database)
    assert stored[0]["status"] == "released"
    assert stored[0]["finalized_at_ms"] == 7_200


async def test_database_lock_is_a_sanitized_ledger_error(tmp_path) -> None:
    import sqlite3

    database = migrate(tmp_path)
    holder = sqlite3.connect(database)
    holder.execute("BEGIN IMMEDIATE")
    store = ledger_for(database, quota=50_000, busy_timeout_ms=50)
    with pytest.raises(LedgerError) as caught:
        await store.admit(
            tenant_id=TENANT,
            request_id="req-lock",
            correlation_id="corr-lock",
            estimated_input_tokens=1,
            reserved_output_tokens=1,
            now_ms=8_000,
        )
    holder.rollback()
    holder.close()
    assert caught.value.category == "database"
    assert "SELECT" not in str(caught.value)
    assert str(database) not in str(caught.value)


def sys_executable() -> str:
    import sys

    return sys.executable
