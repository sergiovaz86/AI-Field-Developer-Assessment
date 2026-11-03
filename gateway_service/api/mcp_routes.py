"""Task 2 MCP proxy route.

``POST /mcp`` authenticates the caller, validates each JSON-RPC element,
authorizes ``tools/call`` independently, and forwards only permitted calls.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from gateway_service.config import GatewayConfig
from gateway_service.dependencies import ConfigDependency
from gateway_service.shared.schemas.errors import (
    UNAUTHORIZED_TOOL_CALL_CODE,
    UNAUTHORIZED_TOOL_CALL_MESSAGE,
)
from gateway_service.shared.schemas.jsonrpc import (
    INTERNAL_ERROR_CODE,
    INTERNAL_ERROR_MESSAGE,
    INVALID_REQUEST_CODE,
    INVALID_REQUEST_MESSAGE,
)
from gateway_service.task2_mcp_gateway.authentication import Role, authenticate
from gateway_service.task2_mcp_gateway.authorization import authorize
from gateway_service.task2_mcp_gateway.downstream_proxy import allowlisted, post_jsonrpc
from gateway_service.task2_mcp_gateway.jsonrpc_validation import (
    Envelope,
    EnvelopeFailure,
    Forwardable,
    LocalFault,
    error_object,
    match_downstream,
    parse_body,
    validate_params,
)

LOGGER = logging.getLogger("gateway_service.task2_mcp_gateway")
router = APIRouter()
_OMIT = object()


@router.post("/mcp")
async def mcp_proxy(request: Request, config: ConfigDependency) -> Response:
    """Authenticate, authorize, and forward one JSON-RPC HTTP request."""
    role = authenticate(request.headers.get("authorization"))
    if role is None:
        LOGGER.info("mcp authentication failed")
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        raw = await request.body()
        if len(raw) > config.max_json_body_bytes:
            LOGGER.info("mcp body rejected")
            return _json(error_object(None, INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE))
        parsed = parse_body(raw, max_batch_items=config.max_batch_items)
        if isinstance(parsed, EnvelopeFailure):
            LOGGER.info("mcp envelope rejected code=%s", parsed.code)
            return _json(error_object(None, parsed.code, parsed.message))
        return await _dispatch(request, config, role, parsed)
    except Exception:
        LOGGER.exception("mcp request failed")
        return _json(error_object(None, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE))


async def _dispatch(
    request: Request,
    config: GatewayConfig,
    role: Role,
    envelope: Envelope,
) -> Response:
    slots: list[Any] = []
    forward_calls: list[Forwardable] = []
    forward_positions: list[int] = []
    for item in envelope.items:
        if isinstance(item, LocalFault):
            slots.append(error_object(item.response_id, item.code, item.message))
            continue
        checked = validate_params(item)
        if isinstance(checked, LocalFault):
            LOGGER.info("mcp role=%s method=%s decision=reject", role, item.method)
            slots.append(error_object(checked.response_id, checked.code, checked.message))
            continue
        decision = authorize(role, checked)
        LOGGER.info(
            "mcp role=%s method=%s tool=%s decision=%s",
            role,
            checked.method,
            _tool_name(checked),
            decision,
        )
        if decision == "deny":
            if checked.is_notification:
                slots.append(_OMIT)
            else:
                slots.append(
                    error_object(
                        checked.request_id,
                        UNAUTHORIZED_TOOL_CALL_CODE,
                        UNAUTHORIZED_TOOL_CALL_MESSAGE,
                    )
                )
            continue
        forward_positions.append(len(slots))
        forward_calls.append(checked)
        slots.append(None)

    if forward_calls:
        await _forward(request, config, forward_calls, forward_positions, slots)

    messages = [slot for slot in slots if slot is not _OMIT]
    if any(slot is None for slot in messages):
        LOGGER.error("mcp response assembly left an empty slot")
        return _json(error_object(None, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE))
    if not messages:
        return Response(status_code=204)
    if envelope.batched:
        return _json(messages)
    return _json(messages[0])


async def _forward(
    request: Request,
    config: GatewayConfig,
    calls: list[Forwardable],
    positions: list[int],
    slots: list[Any],
) -> None:
    identities = [call.request_id for call in calls if not call.is_notification]
    separate = len(identities) != len(set(identities))
    if not allowlisted(config):
        _fail_calls(calls, positions, slots)
        return
    client, owned = _borrow_client(request, config)
    try:
        if separate:
            for position, call in zip(positions, calls, strict=True):
                result = await post_jsonrpc(client, config, call.payload)
                slots[position] = _single_slot(call, result.ok, result.status, result.payload)
            return
        payloads = [call.payload for call in calls]
        body: dict[str, Any] | list[dict[str, Any]] = (
            payloads[0] if len(payloads) == 1 else payloads
        )
        result = await post_jsonrpc(client, config, body)
        expected = [
            (position, call)
            for position, call in zip(positions, calls, strict=True)
            if not call.is_notification
        ]
        for position, call in zip(positions, calls, strict=True):
            if call.is_notification:
                slots[position] = _OMIT
        if not expected:
            return
        if not result.ok or result.status != 200:
            for position, call in expected:
                slots[position] = error_object(
                    call.request_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE
                )
            return
        matched = match_downstream(
            [call for _, call in expected],
            result.payload,
            batched=len(payloads) > 1,
        )
        for (position, call), item in zip(expected, matched, strict=True):
            slots[position] = (
                item
                if item is not None
                else error_object(call.request_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)
            )
    finally:
        if owned:
            await client.aclose()


def _single_slot(call: Forwardable, ok: bool, status: int | None, payload: Any) -> Any:
    if call.is_notification:
        return _OMIT
    if not ok or status != 200:
        return error_object(call.request_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)
    matched = match_downstream([call], payload, batched=False)
    if matched[0] is None:
        return error_object(call.request_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE)
    return matched[0]


def _fail_calls(calls: list[Forwardable], positions: list[int], slots: list[Any]) -> None:
    LOGGER.warning("downstream endpoint rejected")
    for position, call in zip(positions, calls, strict=True):
        if call.is_notification:
            slots[position] = _OMIT
        else:
            slots[position] = error_object(
                call.request_id, INTERNAL_ERROR_CODE, INTERNAL_ERROR_MESSAGE
            )


def _borrow_client(request: Request, config: GatewayConfig) -> tuple[httpx.AsyncClient, bool]:
    existing = getattr(request.app.state, "downstream_client", None)
    if isinstance(existing, httpx.AsyncClient):
        return existing, False
    return (
        httpx.AsyncClient(
            timeout=httpx.Timeout(config.downstream_timeout_ms / 1000),
            trust_env=False,
            follow_redirects=False,
        ),
        True,
    )


def _tool_name(call: Forwardable) -> str:
    if call.method != "tools/call" or not isinstance(call.params, dict):
        return "-"
    name = call.params.get("name")
    if not isinstance(name, str):
        return "-"
    return name[:128]


def _json(payload: object) -> Response:
    return Response(
        content=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        status_code=200,
        media_type="application/json",
    )
