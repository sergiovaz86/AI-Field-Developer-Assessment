"""Rolling-window admission at millisecond boundaries."""

from __future__ import annotations

import pytest
from tests.task4.conftest import ledger_for, migrate

from gateway_service.task4_model_router.rate_limiter import tenant_hash
from gateway_service.task4_model_router.sqlite_store import QuotaRejected

TENANT = tenant_hash("alpha")
OTHER = tenant_hash("beta")


@pytest.fixture
def database(tmp_path):
    return migrate(tmp_path)


async def test_first_request_is_admitted(database) -> None:
    store = ledger_for(database, quota=50_000)
    reserved = await store.admit(
        tenant_id=TENANT,
        request_id="req-1",
        correlation_id="corr-1",
        estimated_input_tokens=20,
        reserved_output_tokens=10,
        now_ms=1_000,
    )
    assert reserved.status == "reserved"
    assert reserved.charged_tokens == 30
    assert await store.charged_total(TENANT, 1_000) == 30
    foreign_keys, busy, journal = await store.pragma_probe()
    assert foreign_keys == 1
    assert busy == 5000
    assert journal.lower() == "wal"


async def test_exact_quota_boundary_admits_and_one_more_token_rejects(database) -> None:
    store = ledger_for(database, quota=50_000)
    admitted = await store.admit(
        tenant_id=TENANT,
        request_id="req-full",
        correlation_id="corr-full",
        estimated_input_tokens=40_000,
        reserved_output_tokens=10_000,
        now_ms=1_000,
    )
    assert admitted.charged_tokens == 50_000
    with pytest.raises(QuotaRejected):
        await store.admit(
            tenant_id=TENANT,
            request_id="req-over",
            correlation_id="corr-over",
            estimated_input_tokens=1,
            reserved_output_tokens=0,
            now_ms=1_000,
        )
    assert await store.count_rows(TENANT) == 1
    assert await store.charged_total(TENANT, 1_000) == 50_000


async def test_tenants_do_not_share_a_window(database) -> None:
    store = ledger_for(database, quota=100)
    await store.admit(
        tenant_id=TENANT,
        request_id="req-a",
        correlation_id="corr-a",
        estimated_input_tokens=100,
        reserved_output_tokens=0,
        now_ms=1_000,
    )
    other = await store.admit(
        tenant_id=OTHER,
        request_id="req-b",
        correlation_id="corr-b",
        estimated_input_tokens=100,
        reserved_output_tokens=0,
        now_ms=1_000,
    )
    assert other.charged_tokens == 100
    with pytest.raises(QuotaRejected):
        await store.admit(
            tenant_id=TENANT,
            request_id="req-a2",
            correlation_id="corr-a2",
            estimated_input_tokens=1,
            reserved_output_tokens=0,
            now_ms=1_000,
        )


async def test_settled_usage_expires_on_the_millisecond_boundary(database) -> None:
    store = ledger_for(database, quota=100, window_ms=60_000)
    admitted = await store.admit(
        tenant_id=TENANT,
        request_id="req-window",
        correlation_id="corr-window",
        estimated_input_tokens=100,
        reserved_output_tokens=0,
        now_ms=1_000,
    )
    await store.mark_in_flight(admitted.request_id, 1_000)
    await store.settle(admitted.request_id, 1_000, None, None)
    assert await store.charged_total(TENANT, 60_999) == 100
    with pytest.raises(QuotaRejected):
        await store.admit(
            tenant_id=TENANT,
            request_id="req-still",
            correlation_id="corr-still",
            estimated_input_tokens=1,
            reserved_output_tokens=0,
            now_ms=60_999,
        )
    expired = await store.admit(
        tenant_id=TENANT,
        request_id="req-next",
        correlation_id="corr-next",
        estimated_input_tokens=100,
        reserved_output_tokens=0,
        now_ms=61_000,
    )
    assert expired.charged_tokens == 100
    assert await store.charged_total(TENANT, 61_000) == 100


async def test_active_reservation_still_counts_after_the_window(database) -> None:
    store = ledger_for(database, quota=100, window_ms=60_000)
    admitted = await store.admit(
        tenant_id=TENANT,
        request_id="req-active",
        correlation_id="corr-active",
        estimated_input_tokens=80,
        reserved_output_tokens=0,
        now_ms=1_000,
    )
    await store.mark_in_flight(admitted.request_id, 1_000)
    assert await store.charged_total(TENANT, 121_000) == 80
    with pytest.raises(QuotaRejected):
        await store.admit(
            tenant_id=TENANT,
            request_id="req-blocked",
            correlation_id="corr-blocked",
            estimated_input_tokens=30,
            reserved_output_tokens=0,
            now_ms=121_000,
        )
    allowed = await store.admit(
        tenant_id=TENANT,
        request_id="req-room",
        correlation_id="corr-room",
        estimated_input_tokens=20,
        reserved_output_tokens=0,
        now_ms=121_000,
    )
    assert allowed.charged_tokens == 20


async def test_one_row_is_not_summed_twice(database) -> None:
    store = ledger_for(database, quota=50_000)
    admitted = await store.admit(
        tenant_id=TENANT,
        request_id="req-once",
        correlation_id="corr-once",
        estimated_input_tokens=15,
        reserved_output_tokens=5,
        now_ms=2_000,
    )
    await store.mark_in_flight(admitted.request_id, 2_000)
    assert await store.charged_total(TENANT, 2_500) == 20
