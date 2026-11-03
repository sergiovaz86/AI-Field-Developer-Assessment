"""Security-critical MCP gateway flows.

Authorization is applied to each JSON-RPC operation, not to the HTTP request
as one unit.
"""

from __future__ import annotations

from pathlib import Path

from tests.task2.conftest import ADMIN_HEADERS, VIEWER_HEADERS, mcp_client, tools_call, tools_list


async def test_admin_admin_tool_is_forwarded(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post(
            "/mcp",
            headers=ADMIN_HEADERS,
            json=tools_call(1, "admin_reset_key"),
        )
    assert response.status_code == 200
    assert response.json()["result"]["name"] == "admin_reset_key"
    assert len(recorder.calls) == 1


async def test_viewer_admin_tool_is_denied_before_downstream(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json=tools_call(2, "admin_reset_key"),
        )
    assert response.json()["error"] == {"code": -32001, "message": "Unauthorized Tool Call"}
    assert recorder.calls == []


async def test_viewer_mixed_batch_forwards_only_the_permitted_operation(tmp_path: Path) -> None:
    request = [tools_list(1), tools_call(2, "admin_reset_key")]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert [item["id"] for item in body] == [1, 2]
    assert body[0]["result"]["tools"][0]["name"] == "get_customer_record"
    assert body[1]["error"] == {"code": -32001, "message": "Unauthorized Tool Call"}
    assert len(recorder.calls) == 1
    assert recorder.calls[0].body == tools_list(1)
