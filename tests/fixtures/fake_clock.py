"""Deterministic millisecond clock for offline tests."""


class FakeClock:
    """Injectable clock whose time advances only when a test says so."""

    def __init__(self, now_ms: int = 0) -> None:
        if now_ms < 0:
            raise ValueError("now_ms must be >= 0")
        self._now_ms = now_ms

    def now_ms(self) -> int:
        """Return the current timestamp in Unix milliseconds."""
        return self._now_ms

    def advance(self, delta_ms: int) -> None:
        """Move the clock forward by a non-negative duration."""
        if delta_ms < 0:
            raise ValueError("delta_ms must be >= 0")
        self._now_ms += delta_ms
