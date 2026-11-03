"""Deterministic downstream MCP HTTP stand-in.

The handler speaks JSON-RPC over one POST. Batch responses are returned in
reverse request order so the gateway cannot rely on arrival order. It does
not read Authorization and it does not call Task 1.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

DOWNSTREAM_TOOLS: tuple[dict[str, str], ...] = (
    {"name": "get_customer_record", "description": "Read a customer record."},
    {"name": "admin_reset_key", "description": "Reset an access key."},
)


@dataclass(frozen=True)
class DownstreamCall:
    """One request observed by the mock transport."""

    url: str
    headers: tuple[tuple[str, str], ...]
    body: Any


class DownstreamRecorder:
    """Records downstream attempts, including attempts that later fail."""

    def __init__(self) -> None:
        self.calls: list[DownstreamCall] = []

    def add(self, request: httpx.Request) -> None:
        try:
            body: Any = json.loads(request.content)
        except json.JSONDecodeError:
            body = request.content
        self.calls.append(
            DownstreamCall(
                url=str(request.url),
                headers=tuple(request.headers.items()),
                body=body,
            )
        )


def mock_response(request: httpx.Request) -> httpx.Response:
    """Answer one downstream POST from the mock catalog."""
    try:
        payload = json.loads(request.content)
    except json.JSONDecodeError:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            },
        )
    if isinstance(payload, list):
        responses = [item for item in (_response_for(element) for element in payload) if item]
        if not responses:
            return httpx.Response(204)
        responses.reverse()
        return httpx.Response(200, json=responses)
    if isinstance(payload, dict):
        response = _response_for(payload)
        if response is None:
            return httpx.Response(204)
        return httpx.Response(200, json=response)
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        },
    )


def recording_transport(
    recorder: DownstreamRecorder,
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> httpx.MockTransport:
    """Build a transport that records every attempt and then handles it."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        recorder.add(request)
        if handler is not None:
            return handler(request)
        return mock_response(request)

    return httpx.MockTransport(dispatch)


def _response_for(item: object) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }
    request_id = item.get("id")
    method = item.get("method")
    if method == "tools/list":
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [dict(tool) for tool in DOWNSTREAM_TOOLS]},
        }
    elif method == "tools/call":
        params = item.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        if not isinstance(arguments, dict):
            arguments = {}
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"name": name, "arguments": arguments},
        }
    else:
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "Method not found"},
        }
    if "id" not in item:
        return None
    return body
