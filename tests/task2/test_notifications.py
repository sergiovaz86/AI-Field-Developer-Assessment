"""Notifications produce no JSON-RPC response."""

from __future__ import annotations

from pathlib import Path

import httpx
from tests.task2.conftest import VIEWER_HEADERS, mcp_client, tools_call, tools_list


async def test_single_notification_returns_204_and_is_forwarded(tmp_path: Path) -> None:
    notification = {"jsonrpc": "2.0", "method": "tools/list"}
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=notification)
    assert response.status_code == 204
    assert response.content == b""
    assert recorder.calls[0].body == notification


async def test_notification_only_batch_returns_204(tmp_path: Path) -> None:
    request = [
        {"jsonrpc": "2.0", "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "ping"},
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    assert response.status_code == 204
    assert response.content == b""
    assert recorder.calls[0].body == request


async def test_permitted_tool_notification_is_forwarded(tmp_path: Path) -> None:
    notification = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_customer_record"},
    }
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=notification)
    assert response.status_code == 204
    assert response.content == b""
    assert recorder.calls[0].body == notification


async def test_notification_downstream_failure_stays_empty(tmp_path: Path) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out at http://secret.internal/mcp")

    notification = {"jsonrpc": "2.0", "method": "tools/list"}
    async with mcp_client(tmp_path, timeout) as (client, recorder):
        timed = await client.post("/mcp", headers=VIEWER_HEADERS, json=notification)
    assert timed.status_code == 204
    assert timed.content == b""
    assert "secret.internal" not in timed.text
    assert len(recorder.calls) == 1

    def leaked(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"http://secret.internal token=admin-token")

    async with mcp_client(tmp_path, leaked) as (client, recorder):
        failed = await client.post("/mcp", headers=VIEWER_HEADERS, json=notification)
    assert failed.status_code == 204
    assert failed.content == b""
    assert "secret.internal" not in failed.text
    assert "admin-token" not in failed.text
    assert len(recorder.calls) == 1


async def test_denied_notification_is_not_forwarded_and_has_no_body(tmp_path: Path) -> None:
    notification = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=notification)
    assert response.status_code == 204
    assert response.content == b""
    assert recorder.calls == []


async def test_mixed_batch_omits_notifications_and_keeps_order(tmp_path: Path) -> None:
    request = [
        {"jsonrpc": "2.0", "method": "tools/list"},
        tools_call(2, "get_customer_record"),
        tools_call(3, "admin_reset_key"),
        tools_list(4),
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert [item["id"] for item in body] == [2, 3, 4]
    assert body[0]["result"]["name"] == "get_customer_record"
    assert body[1]["error"]["code"] == -32001
    assert body[2]["result"]["tools"][0]["name"] == "get_customer_record"
    forwarded = recorder.calls[0].body
    assert [item.get("id") for item in forwarded] == [None, 2, 4]
    assert forwarded[0]["method"] == "tools/list"
