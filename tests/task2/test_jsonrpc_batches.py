"""Batch authorization, ids, and malformed elements."""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.task2.conftest import ADMIN_HEADERS, VIEWER_HEADERS, mcp_client, tools_call, tools_list


async def test_mixed_batch_authorizes_each_element(tmp_path: Path) -> None:
    request = [
        tools_list(1),
        tools_call(2, "admin_reset_key"),
        tools_call(3, "get_customer_record", {"customer_id": "CUST-10002"}),
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert response.status_code == 200
    assert [item["id"] for item in body] == [1, 2, 3]
    assert body[0]["result"]["tools"][1]["name"] == "admin_reset_key"
    assert body[1]["error"] == {"code": -32001, "message": "Unauthorized Tool Call"}
    assert body[2]["result"]["name"] == "get_customer_record"
    assert len(recorder.calls) == 1
    forwarded = recorder.calls[0].body
    assert isinstance(forwarded, list)
    assert [item["id"] for item in forwarded] == [1, 3]
    assert all(
        item["method"] != "tools/call" or item["params"]["name"] != "admin_reset_key"
        for item in forwarded
    )


async def test_downstream_batch_order_is_not_trusted(tmp_path: Path) -> None:
    request = [tools_list(10), tools_list(20)]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    assert [item["id"] for item in response.json()] == [10, 20]
    assert recorder.calls[0].body == request


async def test_duplicate_ids_stay_in_request_order(tmp_path: Path) -> None:
    request = [
        tools_call("dup", "get_customer_record", {"marker": 1}),
        tools_call("dup", "get_customer_record", {"marker": 2}),
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert [item["id"] for item in body] == ["dup", "dup"]
    assert body[0]["result"]["arguments"] == {"marker": 1}
    assert body[1]["result"]["arguments"] == {"marker": 2}
    assert len(recorder.calls) == 2
    assert recorder.calls[0].body == request[0]
    assert recorder.calls[1].body == request[1]


async def test_null_and_empty_string_ids_are_preserved(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        null_id = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(None))
        empty_id = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(""))
        invalid_params = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"jsonrpc": "2.0", "id": "", "method": "tools/call", "params": {"name": 1}},
        )
        denied = await client.post(
            "/mcp", headers=VIEWER_HEADERS, json=tools_call(None, "admin_reset_key")
        )
        echoed = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"id": "echo-me", "method": "tools/list"},
        )
    assert null_id.status_code == 200
    assert null_id.json()["id"] is None
    assert "result" in null_id.json()
    assert empty_id.json()["id"] == ""
    assert "result" in empty_id.json()
    assert invalid_params.json()["id"] == ""
    assert invalid_params.json()["error"]["code"] == -32602
    assert denied.json()["id"] is None
    assert denied.json()["error"]["code"] == -32001
    assert echoed.json()["id"] == "echo-me"
    assert echoed.json()["error"]["code"] == -32600
    forwarded_ids = [call.body["id"] for call in recorder.calls]
    assert forwarded_ids == [None, ""]
    assert all(call.body["method"] == "tools/list" for call in recorder.calls)


async def test_duplicate_null_ids_stay_ordered(tmp_path: Path) -> None:
    request = [
        tools_call(None, "get_customer_record", {"marker": 1}),
        tools_call(None, "get_customer_record", {"marker": 2}),
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert [item["id"] for item in body] == [None, None]
    assert body[0]["result"]["arguments"] == {"marker": 1}
    assert body[1]["result"]["arguments"] == {"marker": 2}
    assert len(recorder.calls) == 2


async def test_boolean_and_fractional_ids_are_rejected(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        boolean = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"jsonrpc": "2.0", "id": True, "method": "tools/list"},
        )
        fractional = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"},
        )
        integral_float = await client.post(
            "/mcp",
            headers=VIEWER_HEADERS,
            json={"jsonrpc": "2.0", "id": 1.0, "method": "tools/list"},
        )
        integer = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(1))
    for response in (boolean, fractional, integral_float):
        assert response.status_code == 200
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    assert integer.json()["id"] == 1
    assert isinstance(integer.json()["id"], int)
    assert integer.json()["id"] is not True
    assert [call.body["id"] for call in recorder.calls] == [1]


async def test_malformed_element_cannot_bypass_authorization(tmp_path: Path) -> None:
    request = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": 1}},
        {
            "jsonrpc": "2.0",
            "id": True,
            "method": "tools/call",
            "params": {"name": "admin_reset_key"},
        },
        tools_call(3, "admin_reset_key"),
        tools_list(4),
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
    body = response.json()
    assert body[0]["error"]["code"] == -32602
    assert body[1] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert body[2]["error"]["code"] == -32001
    assert body[3]["id"] == 4
    assert "result" in body[3]
    assert len(recorder.calls) == 1
    assert recorder.calls[0].body["method"] == "tools/list"


async def test_empty_batch_and_malformed_json(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        empty = await client.post("/mcp", headers=ADMIN_HEADERS, json=[])
        malformed = await client.post("/mcp", headers=ADMIN_HEADERS, content=b"not-json")
        scalar = await client.post("/mcp", headers=ADMIN_HEADERS, json=1)
    assert empty.json()["error"] == {"code": -32600, "message": "Invalid Request"}
    assert malformed.json()["error"] == {"code": -32700, "message": "Parse error"}
    assert scalar.json()["error"]["code"] == -32600
    assert recorder.calls == []


async def test_concurrent_requests_keep_separate_authorization(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        admin_response, viewer_response = await asyncio.gather(
            client.post("/mcp", headers=ADMIN_HEADERS, json=tools_call(1, "admin_reset_key")),
            client.post("/mcp", headers=VIEWER_HEADERS, json=tools_call(2, "admin_reset_key")),
        )
    assert admin_response.json()["result"]["name"] == "admin_reset_key"
    assert viewer_response.json()["error"]["code"] == -32001
    assert len(recorder.calls) == 1
    assert recorder.calls[0].body["id"] == 1
    assert recorder.calls[0].body["params"]["name"] == "admin_reset_key"
