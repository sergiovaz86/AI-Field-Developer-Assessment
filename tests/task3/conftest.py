"""ASGI helpers for streaming guardrail tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from gateway_service.config import GatewayConfig
from gateway_service.main import create_app
from gateway_service.providers.base import ChatUpstream


def make_app(tmp_path: Path, upstream: ChatUpstream | None = None) -> Any:
    """Build a gateway that uses the mock provider unless ``upstream`` is set."""
    application = create_app(GatewayConfig(database_path=tmp_path / "gateway.db"))
    if upstream is not None:
        application.state.chat_upstream = upstream
    return application


def client_for(application: Any) -> AsyncClient:
    """Return an ASGI client for one gateway."""
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://gateway.test")


def completion_body(
    model: str,
    content: str = "hello",
    *,
    stream: bool | None = True,
) -> dict[str, Any]:
    """Return a minimal chat-completion request."""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }
    if stream is not None:
        payload["stream"] = stream
    return payload


async def read_stream(application: Any, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    """Post one completion and return status, content type, and body."""
    async with client_for(application) as client:
        response = await client.post("/v1/chat/completions", json=payload)
    return response.status_code, response.headers.get("content-type", ""), response.content


def visible_text(body: bytes) -> str:
    """Join generated text fields in emission order."""
    pieces: list[str] = []
    for payload in sse_payloads(body):
        for choice in payload.get("choices", []):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            for field in ("content", "refusal"):
                value = delta.get(field)
                if isinstance(value, str):
                    pieces.append(value)
            calls = delta.get("tool_calls")
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if not isinstance(function, dict):
                    continue
                for field in ("name", "arguments"):
                    value = function.get(field)
                    if isinstance(value, str):
                        pieces.append(value)
    return "".join(pieces)


def sse_payloads(body: bytes) -> list[dict[str, Any]]:
    """Parse JSON SSE objects. ``[DONE]`` is omitted."""
    payloads: list[dict[str, Any]] = []
    for frame in _frames(body):
        if frame == "[DONE]":
            continue
        payloads.append(json.loads(frame))
    return payloads


def _frames(body: bytes) -> list[str]:
    text = body.decode()
    frames: list[str] = []
    for block in text.split("\n\n"):
        if block == "":
            continue
        assert block.startswith("data: ")
        frames.append(block.removeprefix("data: "))
    return frames


class ScriptedUpstream:
    """Yield caller-supplied chunks, then optionally fail."""

    def __init__(self, chunks: list[bytes], *, fail: str | None = None) -> None:
        self._chunks = chunks
        self._fail = fail
        self.closed_count = 0

    def stream(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        del payload
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._fail is not None:
            from gateway_service.providers.base import UpstreamFailure

            raise UpstreamFailure(self._fail)

    async def aclose(self) -> None:
        self.closed_count += 1
