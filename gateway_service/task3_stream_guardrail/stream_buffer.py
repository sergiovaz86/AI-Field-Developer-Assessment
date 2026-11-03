"""Bounded withhold buffer for streaming redaction.

The buffer keeps only the longest suffix that can still grow into a
supported match, at most ``MAX_HOLDBACK`` characters. That bound does not
depend on how the upstream divided the text. A candidate that reaches the
maximum without becoming a valid match is replaced with the redaction token
instead of being emitted.
"""

from __future__ import annotations

from gateway_service.task3_stream_guardrail.pii_patterns import (
    MAX_HOLDBACK,
    REDACTION_TOKEN,
    is_viable_prefix,
    longest_viable_suffix_length,
    redact_stable,
)


class StreamBuffer:
    """Hold unresolved text and release only a stable prefix."""

    def __init__(self) -> None:
        self._pending = ""

    @property
    def buffered_length(self) -> int:
        """Return the number of characters currently withheld."""
        return len(self._pending)

    def push(self, text: str) -> str:
        """Append text and return characters that can no longer become PII."""
        if text:
            self._pending += text
        return self._drain(final=False)

    def flush(self) -> str:
        """Redact the remainder at the end of a field."""
        released = self._drain(final=True)
        self._pending = ""
        return released

    def _drain(self, *, final: bool) -> str:
        parts: list[str] = []
        while self._pending:
            if final:
                parts.append(redact_stable(self._pending, REDACTION_TOKEN))
                self._pending = ""
                break
            if len(self._pending) > MAX_HOLDBACK and is_viable_prefix(self._pending[:MAX_HOLDBACK]):
                parts.append(REDACTION_TOKEN)
                self._pending = self._pending[MAX_HOLDBACK:]
                continue
            viable = longest_viable_suffix_length(self._pending)
            if viable == len(self._pending):
                break
            head = self._pending[: len(self._pending) - viable]
            self._pending = self._pending[len(head) :]
            if not head:
                break
            parts.append(redact_stable(head, REDACTION_TOKEN))
        return "".join(parts)
