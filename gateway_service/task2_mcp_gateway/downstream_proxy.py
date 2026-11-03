"""Downstream MCP HTTP proxy.

Calls use httpx.AsyncClient against the allowlisted HTTP JSON-RPC endpoint.
This proxy does not attach to the Task 1 stdio transport, does not forward
inbound credentials, and does not retry. Tool calls may be non-idempotent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from gateway_service.config import GatewayConfig

LOGGER = logging.getLogger("gateway_service.task2_mcp_gateway.downstream")


@dataclass(frozen=True)
class DownstreamResult:
    """One downstream exchange.

    ``reason`` is a log category. It is not a client-facing message.
    """

    ok: bool
    status: int | None
    payload: Any
    reason: str


def allowlisted(config: GatewayConfig) -> bool:
    """Return whether the configured URL is an exact allowlist entry."""
    url = config.downstream_mcp_url
    return url is not None and url in config.downstream_mcp_allowlist


async def post_jsonrpc(
    client: httpx.AsyncClient,
    config: GatewayConfig,
    payload: dict[str, Any] | list[dict[str, Any]],
) -> DownstreamResult:
    """POST one JSON-RPC object or batch. Failures stay opaque to the caller."""
    url = config.downstream_mcp_url
    if url is None or url not in config.downstream_mcp_allowlist:
        LOGGER.warning("downstream endpoint rejected")
        return DownstreamResult(ok=False, status=None, payload=None, reason="rejected")
    try:
        response = await client.post(
            url,
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"content-type": "application/json", "accept": "application/json"},
        )
    except httpx.TimeoutException:
        LOGGER.warning("downstream timeout")
        return DownstreamResult(ok=False, status=None, payload=None, reason="timeout")
    except httpx.HTTPError:
        LOGGER.warning("downstream connection failure")
        return DownstreamResult(ok=False, status=None, payload=None, reason="connect")
    if response.status_code == 204:
        return DownstreamResult(ok=True, status=204, payload=None, reason="empty")
    if response.status_code != 200:
        LOGGER.warning("downstream unexpected status")
        return DownstreamResult(ok=False, status=None, payload=None, reason="status")
    if len(response.content) > config.max_json_body_bytes:
        LOGGER.warning("downstream response exceeded the body limit")
        return DownstreamResult(ok=False, status=None, payload=None, reason="size")
    try:
        decoded = json.loads(response.content)
    except json.JSONDecodeError:
        LOGGER.warning("downstream response was not JSON")
        return DownstreamResult(ok=False, status=None, payload=None, reason="parse")
    return DownstreamResult(ok=True, status=200, payload=decoded, reason="ok")
