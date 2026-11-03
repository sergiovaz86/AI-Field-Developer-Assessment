"""Deterministic offline chat provider.

The mock never opens a socket. ``fragment_bytes`` splits each rendered frame
so tests can place SSE boundaries at arbitrary byte offsets, including one
byte at a time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from gateway_service.mocks.stream_scenarios import render_completion


class MockChatProvider:
    """Yield a scripted OpenAI-compatible SSE body."""

    def __init__(self, *, fragment_bytes: int | None = None, line_ending: str = "\n") -> None:
        if fragment_bytes is not None and fragment_bytes < 1:
            raise ValueError("fragment_bytes must be positive")
        self._fragment_bytes = fragment_bytes
        self._line_ending = line_ending
        self.closed_count = 0

    def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[bytes]:
        frames = render_completion(payload, line_ending=self._line_ending)
        return self._iterate(frames)

    async def _iterate(self, frames: list[bytes]) -> AsyncIterator[bytes]:
        for frame in frames:
            if self._fragment_bytes is None:
                yield frame
                continue
            for offset in range(0, len(frame), self._fragment_bytes):
                yield frame[offset : offset + self._fragment_bytes]

    async def aclose(self) -> None:
        self.closed_count += 1
