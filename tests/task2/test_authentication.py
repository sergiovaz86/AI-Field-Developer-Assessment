"""Authentication at POST /mcp."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from tests.task2.conftest import (
    ADMIN_HEADERS,
    DOWNSTREAM_URL,
    VIEWER_HEADERS,
    mcp_client,
    tools_list,
)

from gateway_service.config import GatewayConfig
from gateway_service.shared.exceptions import ConfigurationError


async def test_admin_and_viewer_tokens_are_accepted(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, recorder):
        admin = await client.post("/mcp", headers=ADMIN_HEADERS, json=tools_list(1))
        viewer = await client.post("/mcp", headers=VIEWER_HEADERS, json=tools_list(2))
    assert admin.status_code == 200
    assert admin.json()["result"]["tools"][1]["name"] == "admin_reset_key"
    assert viewer.status_code == 200
    assert viewer.json()["id"] == 2
    assert len(recorder.calls) == 2
    for call in recorder.calls:
        names = [name.lower() for name, _value in call.headers]
        assert "authorization" not in names
        assert all(
            "admin-token" not in value and "viewer-token" not in value
            for _name, value in call.headers
        )


async def test_missing_malformed_and_unknown_tokens_are_unauthorized(tmp_path: Path) -> None:
    bodies = [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer  admin-token",
        "Basic admin-token",
        "Bearer unknown-token",
        "Bearer admin-token ",
    ]
    async with mcp_client(tmp_path) as (client, recorder):
        for header in bodies:
            headers = {} if header is None else {"authorization": header}
            response = await client.post("/mcp", headers=headers, content=b"not-json")
            assert response.status_code == 401, header
            assert response.json() == {
                "error": "unauthorized",
                "message": "Authentication required",
            }
            assert "jsonrpc" not in response.json()
            assert "admin-token" not in response.text
            assert "unknown-token" not in response.text
    assert recorder.calls == []


async def test_bearer_scheme_is_case_insensitive(tmp_path: Path) -> None:
    async with mcp_client(tmp_path) as (client, _recorder):
        response = await client.post(
            "/mcp",
            headers={"authorization": "bearer admin-token"},
            json=tools_list("scheme"),
        )
    assert response.status_code == 200
    assert response.json()["id"] == "scheme"


async def test_failed_authentication_does_not_log_the_token(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with mcp_client(tmp_path) as (client, _recorder):
        await client.post(
            "/mcp",
            headers={"authorization": "Bearer not-the-admin-token"},
            json={"role": "admin"},
        )
        await client.post("/mcp", headers=ADMIN_HEADERS, json=tools_list(1))
    assert "not-the-admin-token" not in caplog.text
    assert "admin-token" not in caplog.text


async def test_downstream_url_must_be_allowlisted() -> None:
    with pytest.raises(ConfigurationError, match="downstream_mcp_url"):
        GatewayConfig.from_environ({"GATEWAY_DOWNSTREAM_MCP_URL": DOWNSTREAM_URL})
