"""Admissions from independent SQLite connections."""

from __future__ import annotations

import asyncio

from tests.task4.conftest import ledger_for, migrate

from gateway_service.task4_model_router.rate_limiter import tenant_hash
from gateway_service.task4_model_router.sqlite_store import QuotaRejected

TENANT = tenant_hash("alpha")


async def test_three_connections_admit_one_winner(tmp_path) -> None:
    database = migrate(tmp_path)
    stores = [ledger_for(database, quota=90) for _number in range(3)]

    async def once(number: int):
        return await stores[number].admit(
            tenant_id=TENANT,
            request_id=f"worker-{number}",
            correlation_id=f"corr-{number}",
            estimated_input_tokens=40,
            reserved_output_tokens=0,
            now_ms=9_000,
        )

    outcomes = await asyncio.gather(
        once(0),
        once(1),
        once(2),
        return_exceptions=True,
    )
    assert sum(not isinstance(item, Exception) for item in outcomes) == 2
    assert sum(isinstance(item, QuotaRejected) for item in outcomes) == 1
    assert await stores[0].charged_total(TENANT, 9_000) == 80
