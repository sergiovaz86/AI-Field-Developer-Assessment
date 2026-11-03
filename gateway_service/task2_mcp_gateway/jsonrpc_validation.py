"""Zero-trust JSON-RPC parsing.

Malformed JSON and invalid envelopes are rejected before authorization.
A missing id member is a notification. JSON null and "" are real ids.
Booleans are not numbers. Fractional numbers, including ``1.0``, are
rejected instead of being coerced to integers. ``tools/call`` parameter
checks happen here, before an allow or deny decision.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gateway_service.shared.schemas.jsonrpc import (
    INVALID_PARAMS_CODE,
    INVALID_PARAMS_MESSAGE,
    INVALID_REQUEST_CODE,
    INVALID_REQUEST_MESSAGE,
    PARSE_ERROR_CODE,
    PARSE_ERROR_MESSAGE,
)

RequestId = str | int | None
_ALLOWED_MEMBERS = frozenset({"jsonrpc", "method", "params", "id"})
_TOOL_CALL_PARAMS = frozenset({"name", "arguments"})
_MAX_METHOD_LENGTH = 256
_MAX_ID_LENGTH = 512
_BAD_ID = object()


@dataclass(frozen=True)
class LocalFault:
    """One element that already has a JSON-RPC error response."""

    response_id: RequestId | None
    code: int
    message: str


@dataclass(frozen=True)
class Forwardable:
    """One structurally valid call.

    ``request_id`` is JSON null when the id member is null. A notification is
    distinguished by ``is_notification``, not by a null id.
    """

    request_id: RequestId | None
    is_notification: bool
    method: str
    params: dict[str, Any] | list[Any] | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class Envelope:
    """A parsed single call or batch, in original order."""

    batched: bool
    items: tuple[Forwardable | LocalFault, ...]


@dataclass(frozen=True)
class EnvelopeFailure:
    """A failure of the whole HTTP body, which produces one JSON-RPC object."""

    code: int
    message: str


def parse_body(raw: bytes, *, max_batch_items: int) -> Envelope | EnvelopeFailure:
    """Parse one HTTP body. This function does not rewrite invalid requests."""
    if raw == b"":
        return EnvelopeFailure(PARSE_ERROR_CODE, PARSE_ERROR_MESSAGE)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return EnvelopeFailure(PARSE_ERROR_CODE, PARSE_ERROR_MESSAGE)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return EnvelopeFailure(PARSE_ERROR_CODE, PARSE_ERROR_MESSAGE)
    if isinstance(value, list):
        if len(value) == 0 or len(value) > max_batch_items:
            return EnvelopeFailure(INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
        return Envelope(batched=True, items=tuple(_parse_element(item) for item in value))
    if isinstance(value, dict):
        return Envelope(batched=False, items=(_parse_element(value),))
    return EnvelopeFailure(INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)


def validate_params(call: Forwardable) -> Forwardable | LocalFault:
    """Reject method parameter shapes that must not reach authorization."""
    if call.method == "tools/call":
        params = call.params
        if not isinstance(params, dict) or not set(params).issubset(_TOOL_CALL_PARAMS):
            return _fault(call, INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE)
        name = params.get("name")
        if not isinstance(name, str):
            return _fault(call, INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE)
        arguments = params.get("arguments")
        if "arguments" in params and not isinstance(arguments, dict):
            return _fault(call, INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE)
        return call
    if call.method == "tools/list":
        if call.params is not None and not isinstance(call.params, dict):
            return _fault(call, INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE)
        return call
    if call.params is not None and not isinstance(call.params, dict | list):
        return _fault(call, INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE)
    return call


def error_object(response_id: RequestId | None, code: int, message: str) -> dict[str, Any]:
    """Build one JSON-RPC error object. ``response_id`` None is JSON null."""
    return {
        "jsonrpc": "2.0",
        "id": response_id,
        "error": {"code": code, "message": message},
    }


def match_downstream(
    expected: list[Forwardable],
    payload: Any,
    *,
    batched: bool,
) -> list[dict[str, Any] | None]:
    """Correlate downstream payloads by id.

    One malformed, duplicate, missing, or unexpected response id rejects the
    whole downstream batch. The caller sends duplicate client ids separately,
    so this matcher only sees unique expected ids.
    """
    if not expected or any(call.is_notification for call in expected):
        return [None for _ in expected]
    if not batched:
        if len(expected) != 1:
            return [None for _ in expected]
        return [_response_for_id(payload, expected[0].request_id)]
    if not isinstance(payload, list) or len(payload) != len(expected):
        return [None for _ in expected]
    by_id: dict[tuple[str, str | int | None], dict[str, Any]] = {}
    for element in payload:
        validated = _validated_response(element)
        if validated is None:
            return [None for _ in expected]
        identity, cleaned = validated
        if _id_key(identity) in by_id:
            return [None for _ in expected]
        by_id[_id_key(identity)] = cleaned
    if len(by_id) != len(expected):
        return [None for _ in expected]
    if any(_id_key(call.request_id) not in by_id for call in expected):
        return [None for _ in expected]
    return [by_id[_id_key(call.request_id)] for call in expected]


def _parse_element(value: object) -> Forwardable | LocalFault:
    if not isinstance(value, dict):
        return LocalFault(None, INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
    if not set(value).issubset(_ALLOWED_MEMBERS) or value.get("jsonrpc") != "2.0":
        return LocalFault(_preserved_id(value), INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
    method = value.get("method")
    if (
        "method" not in value
        or not isinstance(method, str)
        or method == ""
        or len(method) > _MAX_METHOD_LENGTH
    ):
        return LocalFault(_preserved_id(value), INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
    if "id" in value:
        parsed_id = _parse_request_id(value.get("id"))
        if parsed_id is _BAD_ID:
            return LocalFault(None, INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
        request_id = parsed_id
        notification = False
    else:
        request_id = None
        notification = True
    params: dict[str, Any] | list[Any] | None = None
    if "params" in value:
        raw_params = value.get("params")
        if not isinstance(raw_params, dict | list):
            response_id = None if notification else request_id
            return LocalFault(response_id, INVALID_REQUEST_CODE, INVALID_REQUEST_MESSAGE)
        params = raw_params
    return Forwardable(
        request_id=request_id if notification is False else None,
        is_notification=notification,
        method=method,
        params=params,
        payload=value,
    )


def _fault(call: Forwardable, code: int, message: str) -> LocalFault:
    response_id = None if call.is_notification else call.request_id
    return LocalFault(response_id, code, message)


def _preserved_id(value: Mapping[str, Any]) -> RequestId | None:
    if "id" not in value:
        return None
    parsed = _parse_request_id(value.get("id"))
    if parsed is _BAD_ID:
        return None
    return parsed


def _parse_request_id(value: object) -> RequestId | object:
    """Accept string, integer, and null ids.

    Booleans are rejected before the integer check because ``bool`` is a
    subclass of ``int``. Floats are rejected, including integral values such
    as ``1.0``, so a fractional token cannot become an integer id.
    """
    if isinstance(value, bool):
        return _BAD_ID
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) <= _MAX_ID_LENGTH:
        return value
    return _BAD_ID


def _validated_response(value: object) -> tuple[RequestId, dict[str, Any]] | None:
    if not isinstance(value, dict) or not set(value).issubset({"jsonrpc", "id", "result", "error"}):
        return None
    if value.get("jsonrpc") != "2.0" or "id" not in value:
        return None
    parsed_id = _parse_request_id(value.get("id"))
    if parsed_id is _BAD_ID:
        return None
    has_result = "result" in value
    has_error = "error" in value
    if has_result == has_error:
        return None
    if has_error:
        error = value.get("error")
        if not isinstance(error, dict) or not set(error).issubset({"code", "message", "data"}):
            return None
        code = error.get("code")
        message = error.get("message")
        if isinstance(code, bool) or not isinstance(code, int) or not isinstance(message, str):
            return None
        cleaned: dict[str, Any] = {"code": code, "message": message}
        if "data" in error:
            cleaned["data"] = error.get("data")
        return parsed_id, {"jsonrpc": "2.0", "id": parsed_id, "error": cleaned}
    return parsed_id, {"jsonrpc": "2.0", "id": parsed_id, "result": value.get("result")}


def _response_for_id(value: object, expected_id: RequestId | None) -> dict[str, Any] | None:
    validated = _validated_response(value)
    if validated is None:
        return None
    identity, cleaned = validated
    if not _same_id(identity, expected_id):
        return None
    return cleaned


def _same_id(left: RequestId | None, right: RequestId | None) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    return False


def _id_key(value: RequestId | None) -> tuple[str, str | int | None]:
    """Return a dict key that does not treat ``True`` as ``1``."""
    if value is None:
        return ("null", None)
    if isinstance(value, bool):
        return ("bool", int(value))
    if isinstance(value, str):
        return ("str", value)
    return ("int", value)
