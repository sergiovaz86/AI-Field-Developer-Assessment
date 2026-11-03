"""Usage reconciliation, release, and the missing-usage fallback."""

from __future__ import annotations

import pytest
from tests.task4.conftest import ledger_for, migrate

from gateway_service.task4_model_router.rate_limiter import tenant_hash
from gateway_service.task4_model_router.sqlite_store import QuotaRejected

TENANT = tenant_hash("alpha")


async def _reserve(store, request_id: str, amount: int = 30, now_ms: int = 1_000):
    reserved = await store.admit(
        tenant_id=TENANT,
        request_id=request_id,
        correlation_id=request_id,
        estimated_input_tokens=amount,
        reserved_output_tokens=0,
        now_ms=now_ms,
    )
    await store.mark_in_flight(request_id, now_ms)
    return reserved


async def test_provider_usage_reconciles_downward(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=50_000)
    await _reserve(store, "req-usage", 30)
    settled = await store.settle("req-usage", 1_100, 4, 6)
    assert settled is not None
    assert settled.status == "settled"
    assert settled.charged_tokens == 10
    assert settled.actual_input_tokens == 4
    assert settled.actual_output_tokens == 6
    assert await store.charged_total(TENANT, 1_100) == 10


async def test_reconciliation_is_idempotent(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=50_000)
    await _reserve(store, "req-once", 30)
    first = await store.settle("req-once", 1_100, 2, 3)
    second = await store.settle("req-once", 1_200, 100, 100)
    assert first is not None and second is not None
    assert second.charged_tokens == 5
    assert second.actual_input_tokens == 2
    assert await store.charged_total(TENANT, 1_200) == 5


async def test_failed_request_releases_its_charge(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=50_000)
    await _reserve(store, "req-fail", 25)
    released = await store.release("req-fail", 1_300)
    assert released is not None
    assert released.status == "released"
    assert released.charged_tokens == 0
    assert await store.charged_total(TENANT, 1_300) == 0
    again = await store.release("req-fail", 1_400)
    assert again is not None and again.status == "released"


async def test_timeout_keeps_the_charge_until_release(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=100, window_ms=60_000)
    await _reserve(store, "req-timeout", 40, now_ms=1_000)
    await store.mark_timed_out("req-timeout", 1_050)
    current = await store.get("req-timeout")
    assert current is not None and current.status == "timed_out"
    assert await store.charged_total(TENANT, 200_000) == 40
    released = await store.release("req-timeout", 200_100)
    assert released is not None and released.charged_tokens == 0
    assert await store.charged_total(TENANT, 200_100) == 0


async def test_missing_usage_keeps_the_full_reservation(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=50_000)
    await _reserve(store, "req-missing", 18)
    settled = await store.settle("req-missing", 1_500, None, None)
    assert settled is not None
    assert settled.status == "settled"
    assert settled.charged_tokens == 18
    assert settled.actual_input_tokens is None
    assert settled.actual_output_tokens is None


async def test_usage_above_the_reservation_is_charged_and_blocks_admission(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=30)
    await _reserve(store, "req-cap", 30)
    settled = await store.settle("req-cap", 1_600, 20, 20)
    assert settled is not None
    assert settled.status == "settled"
    assert settled.charged_tokens == 40
    assert settled.actual_input_tokens == 20
    assert settled.actual_output_tokens == 20
    assert await store.charged_total(TENANT, 1_600) == 40
    with pytest.raises(QuotaRejected):
        await store.admit(
            tenant_id=TENANT,
            request_id="req-blocked",
            correlation_id="corr-blocked",
            estimated_input_tokens=1,
            reserved_output_tokens=0,
            now_ms=1_600,
        )
    other = tenant_hash("beta")
    admitted = await store.admit(
        tenant_id=other,
        request_id="req-other",
        correlation_id="corr-other",
        estimated_input_tokens=30,
        reserved_output_tokens=0,
        now_ms=1_600,
    )
    assert admitted.charged_tokens == 30
    again = await store.settle("req-cap", 1_700, 1, 1)
    assert again is not None
    assert again.charged_tokens == 40
    assert await store.charged_total(TENANT, 1_700) == 40


async def test_usage_above_the_reservation_uses_spare_quota(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=100)
    await _reserve(store, "req-spare", 10)
    raised = await store.settle("req-spare", 1_700, 15, 10)
    assert raised is not None
    assert raised.charged_tokens == 25
    assert raised.actual_input_tokens == 15
    assert await store.charged_total(TENANT, 1_700) == 25


async def test_release_does_not_erase_a_settled_charge(tmp_path) -> None:
    store = ledger_for(migrate(tmp_path), quota=50_000)
    await _reserve(store, "req-keep", 20)
    await store.settle("req-keep", 1_800, 3, 4)
    kept = await store.release("req-keep", 1_900)
    assert kept is not None
    assert kept.status == "settled"
    assert kept.charged_tokens == 7
