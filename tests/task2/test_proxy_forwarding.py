"""Downstream failure handling and the Task 1 import boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
from httpx import ASGITransport, AsyncClient
from tests.task2.conftest import ADMIN_HEADERS, VIEWER_HEADERS, mcp_client, tools_list

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.mocks.mcp_downstream import mock_response
from task1_mcp_server.server import create_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]


async def test_downstream_jsonrpc_error_is_preserved(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "error": {
                    "code": -32601,
                    "message": "Method not found",
                    "data": {"retryable": False},
                },
            },
        )

    async with mcp_client(tmp_path, handler) as (client, recorder):
        response = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(7))
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "Method not found", "data": {"retryable": False}},
    }
    assert len(recorder.calls) == 1


async def test_timeout_and_connection_failure_are_sanitized(tmp_path: Path) -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out talking to http://secret.internal/mcp")

    async with mcp_client(tmp_path, timeout) as (client, recorder):
        response = await client.post("/mcp", headers=ADMIN_HEADERS, json=tools_list(1))
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert "secret.internal" not in response.text
    assert len(recorder.calls) == 1

    def connect(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused at http://secret.internal/mcp")

    async with mcp_client(tmp_path, connect) as (client, recorder):
        response = await client.post("/mcp", headers=ADMIN_HEADERS, json=tools_list(1))
    assert response.json()["error"]["message"] == "Internal error"
    assert "secret.internal" not in response.text
    assert len(recorder.calls) == 1


async def test_malformed_downstream_responses_are_sanitized(tmp_path: Path) -> None:
    payloads = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(503, json={"leak": "http://secret.internal"}),
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 99, "result": {"tools": []}}),
        httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": 1, "message": "x", "detail": "http://secret.internal"},
            },
        ),
    ]

    for downstream in payloads:

        def handler(_request: httpx.Request, body: httpx.Response = downstream) -> httpx.Response:
            return body

        async with mcp_client(tmp_path, handler) as (client, _recorder):
            response = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(1))
        assert response.json() == {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Internal error"},
        }
        assert "secret.internal" not in response.text
        assert "not-json" not in response.text


async def test_ambiguous_downstream_batches_are_sanitized(tmp_path: Path) -> None:
    secret = "http://secret.internal"

    def respond(payload: list[object]) -> httpx.Response:
        return httpx.Response(200, json=payload)

    cases = [
        respond(
            [
                {"jsonrpc": "2.0", "id": 10, "result": {"leak": secret}},
                {"jsonrpc": "2.0", "id": 10, "result": {"tools": []}},
            ]
        ),
        respond([{"jsonrpc": "2.0", "id": 10, "result": {"tools": []}}]),
        respond(
            [
                {"jsonrpc": "2.0", "id": 10, "result": {"tools": []}},
                {"jsonrpc": "2.0", "id": 99, "result": {"leak": secret}},
            ]
        ),
        respond(
            [
                {"jsonrpc": "2.0", "id": 10, "result": {"tools": []}},
                {"result": {"leak": secret}},
            ]
        ),
        respond(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "result": {"ok": True},
                    "error": {"code": 1, "message": secret},
                },
                {"jsonrpc": "2.0", "id": 20, "result": {"tools": []}},
            ]
        ),
    ]
    request = [tools_list(10), tools_list(20)]
    for downstream in cases:

        def handler(
            _request: httpx.Request,
            body: httpx.Response = downstream,
        ) -> httpx.Response:
            return body

        async with mcp_client(tmp_path, handler) as (client, _recorder):
            response = await client.post("/mcp", headers=VIEWER_HEADERS, json=request)
        body = response.json()
        assert [item["id"] for item in body] == [10, 20]
        assert body[0]["error"] == {"code": -32603, "message": "Internal error"}
        assert body[1]["error"] == {"code": -32603, "message": "Internal error"}
        assert secret not in response.text
        assert "99" not in response.text


def test_mock_batch_responses_are_reversed() -> None:
    payload = [tools_list(10), tools_list(20)]
    request = httpx.Request(
        "POST",
        "http://mcp-downstream.test/mcp",
        content=json.dumps(payload).encode(),
    )
    response = mock_response(request)
    assert [item["id"] for item in response.json()] == [20, 10]


async def test_unconfigured_downstream_does_not_open_a_connection(tmp_path: Path) -> None:
    application = create_app(GatewayConfig(database_path=tmp_path / "gateway.db"))
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        response = await client.post("/mcp", headers=ADMIN_HEADERS, json=tools_list(1))
    assert response.json()["error"] == {"code": -32603, "message": "Internal error"}
    assert "http" not in response.text
    assert "gateway.db" not in response.text


def test_gateway_does_not_import_task1_and_task1_still_builds() -> None:
    server = create_server()
    assert server.name == "task1-mcp-server"
    root = PROJECT_ROOT / "gateway_service"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("task1_mcp_server"), path
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
                assert not any(name.startswith("task1_mcp_server") for name in imported)
