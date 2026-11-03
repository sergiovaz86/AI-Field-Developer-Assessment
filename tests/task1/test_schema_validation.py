"""Tool-argument failures use JSON-RPC -32602."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.exceptions import MCPError
from tests.task1.conftest import open_task1_session

CASES: tuple[tuple[str, dict[str, object]], ...] = (
    ("get_customer_record", {}),
    ("get_customer_record", {"customer_id": "CUST-10001", "note": "nope"}),
    ("get_customer_record", {"customer_id": "CUST-1"}),
    ("get_customer_record", {"customer_id": "CUST-1000A"}),
    ("get_customer_record", {"customer_id": "cust-10001"}),
    ("get_customer_record", {"customer_id": "CUST-100011"}),
    ("get_customer_record", {"customer_id": " CUST-10001"}),
    ("get_customer_record", {"customer_id": 10001}),
    ("get_customer_record", {"customer_id": None}),
    ("get_customer_record", {"customer_id": True}),
    ("trigger_refund", {"customer_id": "CUST-10001", "reason": "1234567890"}),
    (
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": 1, "reason": "1234567890", "note": 1},
    ),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": 0, "reason": "1234567890"}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": -1, "reason": "1234567890"}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": True, "reason": "1234567890"}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": False, "reason": "1234567890"}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": "10", "reason": "1234567890"}),
    (
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": float("nan"), "reason": "1234567890"},
    ),
    (
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": float("inf"), "reason": "1234567890"},
    ),
    (
        "trigger_refund",
        {"customer_id": "CUST-10001", "amount": float("-inf"), "reason": "1234567890"},
    ),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": 1, "reason": "123456789"}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": 1, "reason": 1234567890}),
    ("trigger_refund", {"customer_id": "CUST-10001", "amount": 1, "reason": None}),
    ("trigger_refund", {"customer_id": "nope", "amount": 1, "reason": "1234567890"}),
)


async def test_invalid_tool_arguments_are_jsonrpc_invalid_params(tmp_path: Path) -> None:
    async with open_task1_session(tmp_path) as (session, _initialized, stderr_path):
        for name, arguments in CASES:
            with pytest.raises(MCPError) as caught:
                await session.call_tool(name, arguments)
            assert caught.value.code == -32602, arguments
            assert caught.value.message == "Invalid params", arguments
        recovered = await session.call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10002", "amount": 1, "reason": "1234567890"},
        )
        assert recovered.is_error is False
        assert recovered.structured_content is not None
        assert recovered.structured_content["refund_id"] == "RFND-000001"
        assert "Traceback" not in stderr_path.read_text(encoding="utf-8")
