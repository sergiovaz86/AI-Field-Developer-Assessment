"""OpenAI-compatible streaming chat completions.

``POST /v1/chat/completions`` redacts generated text before it is written to
the client. ``stream`` must be true. Before the first SSE byte, failures are
JSON error objects. After that byte, an upstream failure is one SSE error
object and the stream stops without ``[DONE]``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from gateway_service.config import GatewayConfig
from gateway_service.providers.base import ChatUpstream, UpstreamFailure
from gateway_service.providers.mock_provider import MockChatProvider
from gateway_service.providers.openai_compatible import OpenAICompatibleProvider
from gateway_service.shared.correlation import new_correlation_id
from gateway_service.shared.schemas.completions import ChatCompletionRequest
from gateway_service.shared.schemas.errors import (
    DATABASE_UNAVAILABLE_CODE,
    DATABASE_UNAVAILABLE_MESSAGE,
    INVALID_API_KEY_CODE,
    INVALID_API_KEY_MESSAGE,
    QUOTA_EXCEEDED_CODE,
    QUOTA_EXCEEDED_MESSAGE,
)
from gateway_service.task3_stream_guardrail.pii_redactor import (
    FieldRedactors,
    ReconstructionError,
    flush_tails,
    prepare_chunk,
)
from gateway_service.task3_stream_guardrail.sse_parser import (
    MAX_SSE_EVENT_BYTES,
    SseByteParser,
    SseData,
    SseDone,
    SseProtocolError,
)
from gateway_service.task4_model_router.rate_limiter import authenticate_tenant
from gateway_service.task4_model_router.router import (
    OutputBudgetError,
    build_router,
)
from gateway_service.task4_model_router.sqlite_store import QuotaRejected

router = APIRouter()
LOGGER = logging.getLogger("gateway_service.chat")

_ERROR_FRAME = (
    b'data: {"error":{"message":"The upstream stream failed.",'
    b'"type":"server_error","code":"upstream_stream_failure"}}\n\n'
)


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    """Stream a redacted chat completion."""
    config: GatewayConfig = request.app.state.config
    correlation_id = new_correlation_id()
    tenant_id: str | None = None
    if config.gateway_mode == "production" or config.tenant_bearer_tokens:
        tenant_id = authenticate_tenant(
            request.headers.get("authorization"),
            config.tenant_bearer_tokens,
        )
        if tenant_id is None:
            return _error(
                401,
                INVALID_API_KEY_MESSAGE,
                INVALID_API_KEY_CODE,
                correlation_id,
                error_type="authentication_error",
            )
    raw = await request.body()
    if len(raw) > config.max_json_body_bytes:
        return _error(400, "The request body is too large.", "request_too_large", correlation_id)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _error(400, "The request body is not valid JSON.", "invalid_json", correlation_id)
    try:
        completion = ChatCompletionRequest.model_validate(payload)
    except ValidationError:
        return _error(400, "The request body is invalid.", "invalid_request", correlation_id)
    if completion.stream is not True:
        return _error(400, "stream must be true", "stream_required", correlation_id)
    if not isinstance(payload, dict):
        return _error(400, "The request body is invalid.", "invalid_request", correlation_id)
    if tenant_id is not None:
        return await _tenant_completion(
            request,
            config,
            payload,
            correlation_id,
            tenant_id,
            completion.model,
        )

    try:
        upstream = _select_upstream(request, config)
    except UpstreamFailure:
        LOGGER.info("chat upstream unavailable correlation=%s", correlation_id)
        return _error(
            502,
            "The upstream request failed.",
            "upstream_failure",
            correlation_id,
            error_type="server_error",
        )

    LOGGER.info("chat completion correlation=%s model=%s", correlation_id, completion.model)
    chunks = upstream.stream(payload)
    guarded = _guard(chunks)
    try:
        first = await anext(guarded)
    except StopAsyncIteration:
        await _close_quietly(guarded, upstream)
        return StreamingResponse(_once(b"data: [DONE]\n\n"), media_type="text/event-stream")
    except UpstreamFailure as exc:
        await _close_quietly(guarded, upstream)
        LOGGER.info("chat upstream failed correlation=%s category=%s", correlation_id, exc.category)
        return _upstream_http_error(exc, correlation_id)

    async def body() -> AsyncIterator[bytes]:
        try:
            yield first
            async for frame in guarded:
                yield frame
        except UpstreamFailure as exc:
            LOGGER.info(
                "chat stream failed correlation=%s category=%s", correlation_id, exc.category
            )
            yield _ERROR_FRAME
        finally:
            await _close_quietly(guarded, upstream)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"x-correlation-id": correlation_id},
    )


async def _tenant_completion(
    request: Request,
    config: GatewayConfig,
    payload: dict[str, Any],
    correlation_id: str,
    tenant_id: str,
    model: str,
) -> JSONResponse | StreamingResponse:
    """Admit one tenant request, then stream the selected provider."""
    model_router = getattr(request.app.state, "model_router", None)
    if model_router is None:
        model_router = build_router(
            config,
            primary=getattr(request.app.state, "primary_upstream", None),
            backup=getattr(request.app.state, "backup_upstream", None),
            ledger=getattr(request.app.state, "ledger", None),
            clock=getattr(request.app.state, "clock", None),
        )
        request.app.state.model_router = model_router
    try:
        session = await model_router.open(
            payload,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
        )
    except OutputBudgetError:
        return _error(400, "The request body is invalid.", "invalid_request", correlation_id)
    except QuotaRejected:
        LOGGER.info("tenant quota rejected correlation=%s", correlation_id)
        return _error(
            429,
            QUOTA_EXCEEDED_MESSAGE,
            QUOTA_EXCEEDED_CODE,
            correlation_id,
            error_type="rate_limit_error",
        )
    except Exception:
        LOGGER.info("tenant admission failed correlation=%s", correlation_id)
        return _error(
            503,
            DATABASE_UNAVAILABLE_MESSAGE,
            DATABASE_UNAVAILABLE_CODE,
            correlation_id,
            error_type="server_error",
        )

    LOGGER.info("chat completion correlation=%s model=%s", correlation_id, model)
    guarded = _guard(session.bytes())
    try:
        first = await anext(guarded)
    except StopAsyncIteration:
        await session.finish(failed=False)
        await _close_session(guarded, session)
        return StreamingResponse(
            _once(b"data: [DONE]\n\n"),
            media_type="text/event-stream",
            headers={"x-correlation-id": correlation_id},
        )
    except UpstreamFailure as exc:
        await session.finish(failed=True)
        await _close_session(guarded, session)
        LOGGER.info(
            "chat upstream failed correlation=%s category=%s",
            correlation_id,
            exc.category,
        )
        return _upstream_http_error(exc, correlation_id)
    except BaseException:
        await session.finish(failed=True)
        await _close_session(guarded, session)
        raise

    async def body() -> AsyncIterator[bytes]:
        try:
            yield first
            async for frame in guarded:
                yield frame
            await session.finish(failed=False)
        except UpstreamFailure as exc:
            LOGGER.info(
                "chat stream failed correlation=%s category=%s",
                correlation_id,
                exc.category,
            )
            await session.finish(failed=True)
            yield _ERROR_FRAME
        finally:
            await _close_session(guarded, session)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"x-correlation-id": correlation_id},
    )


def _select_upstream(request: Request, config: GatewayConfig) -> ChatUpstream:
    injected = getattr(request.app.state, "chat_upstream", None)
    if injected is not None:
        return injected
    if config.llm_upstream == "mock":
        return MockChatProvider()
    base_url = config.primary_provider_base_url
    secret = config.primary_provider_api_key
    if base_url is None or secret is None:
        raise UpstreamFailure("unconfigured")
    return OpenAICompatibleProvider(
        base_url,
        secret.get_secret_value(),
        timeout_seconds=config.first_byte_timeout_ms / 1000,
    )


async def _guard(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    parser = SseByteParser()
    redactors = FieldRedactors()
    template: dict[str, Any] = {
        "id": "chatcmpl-gateway",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock",
    }
    try:
        async for chunk in chunks:
            for piece in _bounded_pieces(chunk):
                for frame in _frames(parser.feed(piece), redactors, template):
                    yield frame
        for frame in _frames(parser.finish(), redactors, template):
            yield frame
    finally:
        aclose = getattr(chunks, "aclose", None)
        if aclose is not None:
            await aclose()


def _frames(
    events: list[Any],
    redactors: FieldRedactors,
    template: dict[str, Any],
) -> list[bytes]:
    frames: list[bytes] = []
    for event in events:
        if isinstance(event, SseProtocolError):
            raise UpstreamFailure("protocol")
        if isinstance(event, SseDone):
            frames.extend(_encode(chunk) for chunk in flush_tails(template, redactors))
            frames.append(b"data: [DONE]\n\n")
            continue
        if not isinstance(event, SseData):
            raise UpstreamFailure("protocol")
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError:
            raise UpstreamFailure("protocol") from None
        if not isinstance(payload, dict):
            raise UpstreamFailure("protocol")
        for field in ("id", "model", "created", "object"):
            if field in payload:
                template[field] = payload[field]
        try:
            prepared = prepare_chunk(payload, redactors)
        except ReconstructionError:
            raise UpstreamFailure("protocol") from None
        frames.extend(_encode(chunk) for chunk in prepared)
    return frames


def _bounded_pieces(chunk: bytes) -> list[bytes]:
    """Split one upstream read so the parser retains at most one frame."""
    if len(chunk) <= MAX_SSE_EVENT_BYTES:
        return [chunk]
    return [
        chunk[offset : offset + MAX_SSE_EVENT_BYTES]
        for offset in range(0, len(chunk), MAX_SSE_EVENT_BYTES)
    ]


def _encode(payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}\n\n".encode()


def _upstream_http_error(exc: UpstreamFailure, correlation_id: str) -> JSONResponse:
    if exc.category == "timeout":
        return _error(
            504,
            "The upstream request timed out.",
            "upstream_timeout",
            correlation_id,
            error_type="server_error",
        )
    return _error(
        502,
        "The upstream request failed.",
        "upstream_failure",
        correlation_id,
        error_type="server_error",
    )


def _error(
    status: int,
    message: str,
    code: str,
    correlation_id: str,
    *,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
                "correlation_id": correlation_id,
            }
        },
        headers={"x-correlation-id": correlation_id},
    )


async def _once(frame: bytes) -> AsyncIterator[bytes]:
    yield frame


async def _close_session(guarded: AsyncIterator[bytes], session: Any) -> None:
    """Close the stream even when the client task is already cancelling."""
    task = asyncio.current_task()
    pending = 0
    if task is not None:
        while task.cancelling():
            task.uncancel()
            pending += 1
    try:
        aclose = getattr(guarded, "aclose", None)
        if aclose is not None:
            await aclose()
        await session.aclose()
    finally:
        if task is not None:
            for _number in range(pending):
                task.cancel()


async def _close_quietly(guarded: AsyncIterator[bytes], upstream: ChatUpstream) -> None:
    aclose = getattr(guarded, "aclose", None)
    if aclose is not None:
        await aclose()
    await upstream.aclose()
