"""ASGI client for the Task 2 MCP gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport, AsyncClient

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.mocks.mcp_downstream import DownstreamRecorder, recording_transport

DOWNSTREAM_URL = "http://mcp-downstream.test/mcp"
ADMIN_HEADERS = {"authorization": "Bearer admin-token"}
VIEWER_HEADERS = {"authorization": "Bearer viewer-token"}
Handler = Callable[[httpx.Request], httpx.Response]


def make_config(tmp_path: Path, **overrides: Any) -> GatewayConfig:
    """Build a gateway config whose downstream URL is allowlisted."""
    payload: dict[str, Any] = {
        "database_path": tmp_path / "gateway.db",
        "downstream_mcp_url": DOWNSTREAM_URL,
        "downstream_mcp_allowlist": (DOWNSTREAM_URL,),
    }
    payload.update(overrides)
    return GatewayConfig(**payload)


@asynccontextmanager
async def mcp_client(
    tmp_path: Path,
    handler: Handler | None = None,
    **config_overrides: Any,
) -> AsyncIterator[tuple[AsyncClient, DownstreamRecorder]]:
    """Yield an ASGI client and the recorder for its mock downstream."""
    application = create_app(make_config(tmp_path, **config_overrides))
    recorder = DownstreamRecorder()
    downstream = httpx.AsyncClient(transport=recording_transport(recorder, handler))
    application.state.downstream_client = downstream
    async with downstream:
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as client:
            yield client, recorder


def tools_list(request_id: object) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}


def tools_call(
    request_id: object,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}
