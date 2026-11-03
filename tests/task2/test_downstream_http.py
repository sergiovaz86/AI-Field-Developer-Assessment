"""The Compose downstream adapter serves the existing JSON-RPC mock."""

from __future__ import annotations

import threading

import httpx

from gateway_service.mocks.mcp_http import bind
from scripts.probe_http import main as probe_http


def test_http_adapter_serves_admin_tool_and_health() -> None:
    server = bind("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            health = client.get("/health")
            response = client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "admin_reset_key", "arguments": {}},
                },
            )
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert response.status_code == 200
        assert response.json()["result"] == {"name": "admin_reset_key", "arguments": {}}
        probe_http([f"http://{host}:{port}/health"])
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
