"""OpenAI-compatible HTTP streaming adapter.

The adapter posts to ``{base_url}/chat/completions`` with the configured
bearer credential. Failures are reported as ``UpstreamFailure`` categories.
Exception text does not include the URL, the credential, or the response body.

``open_attempt`` completes when the HTTP response headers are available.
That is the first response byte. Body chunks, partial SSE frames, and later
content deltas are read after that point and are not part of the header wait.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from gateway_service.providers.base import UpstreamFailure
from gateway_service.task3_stream_guardrail.sse_parser import MAX_SSE_EVENT_BYTES

_QUEUE_END = object()


class OpenAICompatibleProvider:
    """Stream chat completions from one OpenAI-compatible HTTP endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: float,
        header_timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._owns_client = client is None
        if header_timeout_seconds is None:
            timeout = httpx.Timeout(timeout_seconds)
        else:
            timeout = httpx.Timeout(
                connect=header_timeout_seconds,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=header_timeout_seconds,
            )
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[bytes]:
        body = _stream_body(payload)
        return self._read(body)

    def open_attempt(self, payload: Mapping[str, Any]) -> HttpAttempt:
        """Start one request and expose response headers before body bytes."""
        return HttpAttempt(self._client, self._url, self._api_key, _stream_body(payload))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _read(self, body: Mapping[str, Any]) -> AsyncIterator[bytes]:
        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=body,
                headers=_headers(self._api_key),
            ) as response:
                if response.status_code >= 400:
                    raise UpstreamFailure(_http_category(response.status_code))
                async for chunk in response.aiter_bytes(chunk_size=MAX_SSE_EVENT_BYTES):
                    if chunk:
                        yield chunk
        except UpstreamFailure:
            raise
        except httpx.TimeoutException:
            raise UpstreamFailure("timeout") from None
        except httpx.HTTPError:
            raise UpstreamFailure("network") from None


class HttpAttempt:
    """One upstream POST whose header arrival is separate from body reads."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        api_key: str,
        body: Mapping[str, Any],
    ) -> None:
        self._queue: asyncio.Queue[bytes | UpstreamFailure | object] = asyncio.Queue()
        self._started = asyncio.Event()
        self._failure: UpstreamFailure | None = None
        self._closed = False
        self._task = asyncio.create_task(self._pump(client, url, api_key, body))

    async def wait_for_response_byte(self) -> None:
        """Return once response headers have arrived, or the attempt failed first."""
        await self._started.wait()
        if self._failure is not None:
            raise self._failure

    def body(self) -> AsyncIterator[bytes]:
        return self._body()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            return

    async def _body(self) -> AsyncIterator[bytes]:
        try:
            while True:
                item = await self._queue.get()
                if item is _QUEUE_END:
                    return
                if isinstance(item, UpstreamFailure):
                    raise item
                if isinstance(item, bytes):
                    yield item
        finally:
            await self.aclose()

    async def _pump(
        self,
        client: httpx.AsyncClient,
        url: str,
        api_key: str,
        body: Mapping[str, Any],
    ) -> None:
        try:
            async with client.stream("POST", url, json=body, headers=_headers(api_key)) as response:
                if response.status_code >= 400:
                    self._failure = UpstreamFailure(_http_category(response.status_code))
                    self._started.set()
                    return
                self._started.set()
                async for chunk in response.aiter_bytes(chunk_size=MAX_SSE_EVENT_BYTES):
                    if chunk:
                        await self._queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except UpstreamFailure as exc:
            self._failure = exc
            await self._queue.put(exc)
        except httpx.TimeoutException:
            failure = UpstreamFailure("timeout")
            if self._started.is_set():
                failure.after_response_byte = True
            self._failure = failure
            await self._queue.put(failure)
        except httpx.HTTPError:
            failure = UpstreamFailure("network")
            self._failure = failure
            await self._queue.put(failure)
        finally:
            if not self._started.is_set():
                if self._failure is None:
                    self._failure = UpstreamFailure("network")
                self._started.set()
            await self._queue.put(_QUEUE_END)


def _stream_body(payload: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in payload.items() if key != "stream"}
    body["stream"] = True
    return body


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }


def _http_category(status_code: int) -> str:
    if status_code == 429:
        return "http_429"
    if 500 <= status_code <= 599:
        return "http_5xx"
    return "http"
