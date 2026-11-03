"""Deterministic customer reads and refund transitions."""

from __future__ import annotations

import threading
from pathlib import Path

from tests.task1.conftest import open_task1_session

from task1_mcp_server.customer_store import CustomerStore
from task1_mcp_server.errors import UNKNOWN_CUSTOMER_MESSAGE


async def test_get_customer_record_returns_the_fixture(tmp_path: Path) -> None:
    async with open_task1_session(tmp_path) as (session, _initialized, _stderr):
        result = await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        assert result.is_error is False
        assert result.structured_content == {
            "customer_id": "CUST-10001",
            "full_name": "Avery Chen",
            "email": "avery.chen@example.com",
            "status": "active",
            "account_balance_cents": 15000,
            "refund_count": 0,
        }


async def test_refund_ids_advance_only_after_a_known_customer(tmp_path: Path) -> None:
    async with open_task1_session(tmp_path) as (session, _initialized, stderr_path):
        unknown = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-99999", "amount": 5, "reason": "1234567890"},
        )
        assert unknown.is_error is True
        assert unknown.structured_content is None
        assert unknown.content[0].type == "text"
        assert UNKNOWN_CUSTOMER_MESSAGE in unknown.content[0].text
        missing = await session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
        assert missing.is_error is True
        assert UNKNOWN_CUSTOMER_MESSAGE in missing.content[0].text

        first = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10001", "amount": 10, "reason": "duplicate charge"},
        )
        second = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10001", "amount": 2.5, "reason": "          "},
        )
        suspended = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10003", "amount": 1, "reason": "account adjustment"},
        )
        over_balance = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10002", "amount": 100, "reason": "larger than balance"},
        )
        record = await session.call_tool("get_customer_record", {"customer_id": "CUST-10001"})
        untouched_balance = await session.call_tool(
            "get_customer_record", {"customer_id": "CUST-10002"}
        )
        still_suspended = await session.call_tool(
            "get_customer_record", {"customer_id": "CUST-10003"}
        )
        assert first.structured_content == {
            "refund_id": "RFND-000001",
            "customer_id": "CUST-10001",
            "amount": 10.0,
            "reason": "duplicate charge",
            "status": "completed",
            "prior_refund_count": 0,
            "refund_count": 1,
        }
        assert second.structured_content is not None
        assert second.structured_content["refund_id"] == "RFND-000002"
        assert second.structured_content["amount"] == 2.5
        assert second.structured_content["reason"] == "          "
        assert second.structured_content["prior_refund_count"] == 1
        assert second.structured_content["refund_count"] == 2
        assert suspended.structured_content is not None
        assert suspended.structured_content["refund_id"] == "RFND-000003"
        assert suspended.structured_content["customer_id"] == "CUST-10003"
        assert over_balance.structured_content is not None
        assert over_balance.structured_content["refund_id"] == "RFND-000004"
        assert record.structured_content is not None
        assert record.structured_content["refund_count"] == 2
        assert record.structured_content["account_balance_cents"] == 15000
        assert record.structured_content["status"] == "active"
        assert untouched_balance.structured_content is not None
        assert untouched_balance.structured_content["refund_count"] == 1
        assert untouched_balance.structured_content["account_balance_cents"] == 8200
        assert untouched_balance.structured_content["status"] == "active"
        assert still_suspended.structured_content is not None
        assert still_suspended.structured_content["status"] == "suspended"
        assert still_suspended.structured_content["account_balance_cents"] == 0
        assert still_suspended.structured_content["refund_count"] == 1
        stderr = stderr_path.read_text(encoding="utf-8")
        assert "Traceback" not in stderr
        assert "CUST-99999" not in stderr


def test_concurrent_refunds_receive_unique_ids() -> None:
    store = CustomerStore()
    barrier = threading.Barrier(8)
    results: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            record = store.trigger_refund("CUST-10001", 1.0, "1234567890")
            with lock:
                results.append(record.refund_id)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(results) == [f"RFND-{number:06d}" for number in range(1, 9)]
    snapshot = store.get_customer("CUST-10001")
    assert snapshot is not None
    assert snapshot.refund_count == 8
    assert snapshot.account_balance_cents == 15000
    assert snapshot.status == "active"
