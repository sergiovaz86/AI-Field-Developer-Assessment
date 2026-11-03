"""Per-call authorization for tools/call."""

from __future__ import annotations

from pathlib import Path

from tests.task2.conftest import ADMIN_HEADERS, VIEWER_HEADERS, mcp_client, tools_call, tools_list

from gateway_service.mocks.mcp_downstream import DOWNSTREAM_TOOLS


async def test_tools_list_is_forwarded_without_filtering(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(4))
    body = response.json()
    assert response.status_code == 200
    assert body["id"] == 4
    assert body["result"]["tools"] == [dict(tool) for tool in DOWNSTREAM_TOOLS]
    assert recorder.calls[0].body == tools_list(4)
    assert recorder.calls[0].url == "http://mcp-downstream.test/mcp"


async def test_admin_can_call_an_admin_tool(tmp_path: Path) -> None:
    request = tools_call(5, "admin_reset_key", {"reason": "rotate"})
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=ADMIN_HEADERS, json=request)
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"name": "admin_reset_key", "arguments": {"reason": "rotate"}},
    }
    assert recorder.calls[0].body == request


async def test_viewer_admin_tool_is_denied_and_not_forwarded(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json=tools_call(6, "admin_reset_key"),
        )
    assert response.status_code == 200
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 6,
        "error": {"code": -32001, "message": "Unauthorized Tool Call"},
    }
    assert recorder.calls == []


async def test_viewer_can_call_a_non_admin_tool(tmp_path: Path) -> None:
    request = tools_call(7, "get_customer_record", {"customer_id": "CUST-10001"})
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    assert response.json()["result"] == {
        "name": "get_customer_record",
        "arguments": {"customer_id": "CUST-10001"},
    }
    assert len(recorder.calls) == 1


async def test_body_role_does_not_grant_admin_and_invalid_params_stop_first(
    tmp_path: Path,
) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        extra = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "admin_reset_key", "role": "admin"},
            },
        )
        missing = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}},
        )
        wrong_type = await client.post(
            "/mcp",
            headers=ADMIN_HEADERS,
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": True},
            },
        )
    assert extra.json()["error"] == {"code": -32602, "message": "Invalid params"}
    assert missing.json()["error"]["code"] == -32602
    assert wrong_type.json()["id"] == 10
    assert wrong_type.json()["error"]["code"] == -32602
    assert recorder.calls == []
