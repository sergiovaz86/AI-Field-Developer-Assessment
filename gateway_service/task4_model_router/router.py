"""Primary-then-backup streaming for one admitted completion.

The 3000 ms deadline applies to the first HTTP response byte. For the HTTP
adapter that is response-header arrival. A scripted upstream has no header
channel, so its first yielded body byte is that signal. Body delays, partial
SSE frames, and later deltas do not start the backup. The backup is also
forbidden after any body byte is yielded toward the client. Both attempts
share one reservation row. The Task 3 guardrail wraps whichever iterator is
returned.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from gateway_service.config import GatewayConfig
from gateway_service.providers.base import ChatUpstream, UpstreamFailure
from gateway_service.providers.mock_provider import MockChatProvider
from gateway_service.providers.openai_compatible import OpenAICompatibleProvider
from gateway_service.task4_model_router.failover_policy import is_failover_eligible
from gateway_service.task4_model_router.rate_limiter import RateLimiter
from gateway_service.task4_model_router.sqlite_store import (
    LedgerError,
    ReservationLedger,
)
from gateway_service.task4_model_router.token_counter import (
    OutputBudgetError,
    estimate_input_tokens,
    output_budget,
)
from gateway_service.task4_model_router.usage_accounting import UsageSniffer

LOGGER = logging.getLogger("gateway.router")
_CLEANUP_TASKS: set[asyncio.Task[None]] = set()

Clock = Callable[[], int]
_READ_TIMEOUT_SECONDS = 30.0


class CompletionSession:
    """One logical completion after quota admission."""

    def __init__(
        self,
        *,
        ledger: ReservationLedger,
        clock: Clock,
        primary: ChatUpstream,
        backup: ChatUpstream | None,
        payload: Mapping[str, Any],
        request_id: str,
        correlation_id: str,
        first_byte_timeout_s: float,
    ) -> None:
        self.request_id = request_id
        self.correlation_id = correlation_id
        self._ledger = ledger
        self._clock = clock
        self._primary = primary
        self._backup = backup
        self._payload = payload
        self._timeout_s = first_byte_timeout_s
        self._iterator: AsyncIterator[bytes] | None = None
        self._sniffer = UsageSniffer()
        self._finished = False
        self._started = False
        self._completed = False
        self.backup_used = False
        self.primary_calls = 0
        self.backup_calls = 0

    def bytes(self) -> AsyncIterator[bytes]:
        return self._stream()

    async def finish(self, *, failed: bool) -> None:
        if self._finished:
            return
        now_ms = self._clock()
        if self._completed:
            failed = False
        for _attempt in range(3):
            try:
                await self._write_ledger(now_ms, failed=failed)
            except LedgerError:
                LOGGER.info(
                    "ledger finalize failed correlation=%s",
                    self.correlation_id,
                )
                continue
            self._finished = True
            return
        LOGGER.info(
            "ledger finalize incomplete correlation=%s",
            self.correlation_id,
        )

    async def _write_ledger(self, now_ms: int, *, failed: bool) -> None:
        if self._sniffer.saw_usage:
            await self._ledger.settle(
                self.request_id,
                now_ms,
                self._sniffer.prompt_tokens,
                self._sniffer.completion_tokens,
            )
            return
        if failed or not self._started:
            await self._ledger.release(self.request_id, now_ms)
            return
        await self._ledger.settle(self.request_id, now_ms, None, None)

    async def aclose(self) -> None:
        iterator = self._iterator
        self._iterator = None
        if iterator is not None:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    LOGGER.info(
                        "upstream close failed correlation=%s",
                        self.correlation_id,
                    )
        if not self._finished:
            worker = _spawn(self.finish(failed=True))
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                raise

    async def _stream(self) -> AsyncIterator[bytes]:
        try:
            iterator, first = await self._open(self._primary, backup=False)
        except UpstreamFailure as exc:
            if (
                exc.after_response_byte
                or self._backup is None
                or not is_failover_eligible(exc.category)
            ):
                if exc.category == "timeout":
                    await self._mark_timeout()
                raise
            try:
                iterator, first = await self._open(self._backup, backup=True)
            except UpstreamFailure as backup_exc:
                if backup_exc.category == "timeout":
                    await self._mark_timeout()
                raise
            self.backup_used = True
        self._iterator = iterator
        self._started = True
        self._sniffer.feed(first)
        yield first
        async for chunk in iterator:
            self._sniffer.feed(chunk)
            yield chunk
        self._completed = True

    async def _open(
        self,
        upstream: ChatUpstream,
        *,
        backup: bool,
    ) -> tuple[AsyncIterator[bytes], bytes]:
        if backup:
            self.backup_calls += 1
        else:
            self.primary_calls += 1
        opener = getattr(upstream, "open_attempt", None)
        if opener is not None:
            return await self._open_http(opener)
        iterator = upstream.stream(self._payload)
        try:
            first = await asyncio.wait_for(anext(iterator), self._timeout_s)
        except StopAsyncIteration:
            await _close_iterator(iterator)
            raise UpstreamFailure("protocol") from None
        except TimeoutError:
            await _close_iterator(iterator)
            raise UpstreamFailure("timeout") from None
        except UpstreamFailure:
            await _close_iterator(iterator)
            raise
        except Exception:
            await _close_iterator(iterator)
            LOGGER.info(
                "upstream attempt failed correlation=%s",
                self.correlation_id,
            )
            raise UpstreamFailure("network") from None
        if not isinstance(first, bytes) or first == b"":
            await _close_iterator(iterator)
            raise UpstreamFailure("protocol")
        return iterator, first

    async def _open_http(self, opener: Any) -> tuple[AsyncIterator[bytes], bytes]:
        attempt = opener(self._payload)
        try:
            await asyncio.wait_for(attempt.wait_for_response_byte(), self._timeout_s)
        except TimeoutError:
            await attempt.aclose()
            raise UpstreamFailure("timeout") from None
        except UpstreamFailure:
            await attempt.aclose()
            raise
        iterator = attempt.body()
        try:
            first = await anext(iterator)
        except StopAsyncIteration:
            await _close_iterator(iterator)
            raise UpstreamFailure("protocol") from None
        except UpstreamFailure:
            await _close_iterator(iterator)
            raise
        except Exception:
            await _close_iterator(iterator)
            LOGGER.info(
                "upstream body failed correlation=%s",
                self.correlation_id,
            )
            raise UpstreamFailure("network") from None
        if not isinstance(first, bytes) or first == b"":
            await _close_iterator(iterator)
            raise UpstreamFailure("protocol")
        return iterator, first

    async def _mark_timeout(self) -> None:
        try:
            await self._ledger.mark_timed_out(self.request_id, self._clock())
        except LedgerError:
            LOGGER.info(
                "ledger timeout mark failed correlation=%s",
                self.correlation_id,
            )


class ModelRouter:
    """Authenticate a completion, reserve tokens, then stream one provider."""

    def __init__(
        self,
        *,
        ledger: ReservationLedger,
        clock: Clock,
        primary: ChatUpstream,
        backup: ChatUpstream | None,
        default_output_tokens: int,
        max_output_tokens: int,
        first_byte_timeout_ms: int,
    ) -> None:
        self.limiter = RateLimiter(ledger)
        self._clock = clock
        self._primary = primary
        self._backup = backup
        self._default_output_tokens = default_output_tokens
        self._max_output_tokens = max_output_tokens
        self._first_byte_timeout_ms = first_byte_timeout_ms

    @property
    def first_byte_timeout_ms(self) -> int:
        return self._first_byte_timeout_ms

    async def open(
        self,
        payload: Mapping[str, Any],
        *,
        tenant_id: str,
        correlation_id: str,
    ) -> CompletionSession:
        budget = output_budget(
            payload,
            default=self._default_output_tokens,
            ceiling=self._max_output_tokens,
        )
        estimated = estimate_input_tokens(payload)
        now_ms = self._clock()
        request_id = uuid.uuid4().hex
        reservation = await self.limiter.admit(
            tenant_id=tenant_id,
            request_id=request_id,
            correlation_id=correlation_id,
            estimated_input_tokens=estimated,
            reserved_output_tokens=budget,
            now_ms=now_ms,
            status="in_flight",
        )
        forward = dict(payload)
        forward["stream"] = True
        forward["max_tokens"] = budget
        forward["stream_options"] = {"include_usage": True}
        return CompletionSession(
            ledger=self.limiter.ledger,
            clock=self._clock,
            primary=self._primary,
            backup=self._backup,
            payload=forward,
            request_id=reservation.request_id,
            correlation_id=correlation_id,
            first_byte_timeout_s=self._first_byte_timeout_ms / 1000,
        )


def build_router(
    config: GatewayConfig,
    *,
    primary: ChatUpstream | None = None,
    backup: ChatUpstream | None = None,
    ledger: ReservationLedger | None = None,
    clock: Clock | None = None,
) -> ModelRouter:
    """Build a router from configuration and optional test doubles."""
    selected_clock = clock if clock is not None else _wall_clock
    selected_ledger = (
        ledger
        if ledger is not None
        else ReservationLedger(
            config.database_path,
            busy_timeout_ms=config.sqlite_busy_timeout_ms,
            tokens_per_window=config.default_tokens_per_window,
            window_ms=config.window_ms,
        )
    )
    selected_primary = (
        primary
        if primary is not None
        else _provider_from_config(
            config,
            base_url=config.primary_provider_base_url,
            api_key=config.primary_provider_api_key.get_secret_value(),
            mock=config.llm_upstream == "mock",
        )
    )
    selected_backup = backup
    if selected_backup is None and config.backup_provider_base_url is not None:
        secret = config.backup_provider_api_key
        if secret is not None and secret.get_secret_value() != "":
            selected_backup = _provider_from_config(
                config,
                base_url=config.backup_provider_base_url,
                api_key=secret.get_secret_value(),
                mock=False,
            )
    if selected_backup is None and config.llm_upstream == "mock":
        selected_backup = MockChatProvider()
    return ModelRouter(
        ledger=selected_ledger,
        clock=selected_clock,
        primary=selected_primary,
        backup=selected_backup,
        default_output_tokens=config.default_max_output_tokens,
        max_output_tokens=config.max_output_token_budget,
        first_byte_timeout_ms=config.first_byte_timeout_ms,
    )


def _provider_from_config(
    config: GatewayConfig,
    *,
    base_url: str | None,
    api_key: str,
    mock: bool,
) -> ChatUpstream:
    if mock or base_url is None:
        return MockChatProvider()
    return OpenAICompatibleProvider(
        base_url,
        api_key,
        timeout_seconds=_READ_TIMEOUT_SECONDS,
        header_timeout_seconds=config.first_byte_timeout_ms / 1000,
    )


async def drain_ledger_cleanup() -> None:
    """Wait for cancellation writes that were detached from the request task."""
    pending = tuple(_CLEANUP_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _spawn(work: Any) -> asyncio.Task[None]:
    task = asyncio.create_task(work)
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)
    return task


def _wall_clock() -> int:
    return time.time_ns() // 1_000_000


async def _close_iterator(iterator: AsyncIterator[bytes]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        LOGGER.info("upstream iterator close failed")


__all__ = ["CompletionSession", "ModelRouter", "OutputBudgetError", "build_router"]
