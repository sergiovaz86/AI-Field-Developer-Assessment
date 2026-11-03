"""Incremental SSE parser.

HTTP chunks are bytes, not events. The parser keeps an incomplete UTF-8
sequence and an incomplete frame until a blank line closes the event.
``data: [DONE]`` is reported separately and is not parsed as JSON.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass

MAX_SSE_EVENT_BYTES = 262_144


@dataclass(frozen=True)
class SseData:
    """One completed data payload, without the ``data:`` prefix."""

    data: str


@dataclass(frozen=True)
class SseDone:
    """The terminal ``[DONE]`` marker."""


@dataclass(frozen=True)
class SseProtocolError:
    """A frame that cannot be parsed. The reason is a stable category."""

    reason: str


SseEvent = SseData | SseDone | SseProtocolError


class SseByteParser:
    """Assemble LF or CRLF SSE events from arbitrary byte chunks.

    The incomplete frame, including its data lines, never exceeds
    ``max_event_bytes``. A larger HTTP read is accepted when it contains
    several smaller complete events. The UTF-8 decoder holds at most one
    incomplete code point between reads.
    """

    def __init__(self, max_event_bytes: int = MAX_SSE_EVENT_BYTES) -> None:
        if max_event_bytes < 1:
            raise ValueError("max_event_bytes must be positive")
        self._max_event_bytes = max_event_bytes
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._pending = ""
        self._data_lines: list[str] = []
        self._data_bytes = 0
        self._failed = False

    @property
    def buffered_bytes(self) -> int:
        """Return bytes retained for the current incomplete event."""
        return len(self._pending.encode("utf-8")) + self._data_bytes

    def feed(self, chunk: bytes) -> list[SseEvent]:
        """Consume one upstream chunk."""
        if self._failed:
            return []
        if not chunk:
            return []
        try:
            text = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            return self._fail("utf8")
        return self._consume(text)

    def finish(self) -> list[SseEvent]:
        """Reject a truncated frame when the upstream ends."""
        if self._failed:
            return []
        try:
            text = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return self._fail("utf8")
        events = self._consume(text)
        if self._failed:
            return events
        if self._pending or self._data_lines:
            return [*events, *self._fail("truncated")]
        return events

    def _consume(self, text: str) -> list[SseEvent]:
        events: list[SseEvent] = []
        offset = 0
        while offset < len(text):
            room = self._max_event_bytes - self.buffered_bytes
            if room <= 0:
                return [*events, *self._fail("oversized")]
            taken = _prefix_within(text[offset:], room)
            if taken == 0:
                return [*events, *self._fail("oversized")]
            self._pending += text[offset : offset + taken]
            offset += taken
            events.extend(self._drain_lines())
            if self._failed:
                return events
        return events

    def _drain_lines(self) -> list[SseEvent]:
        events: list[SseEvent] = []
        while True:
            line, separator = _next_line(self._pending)
            if separator is None or line is None:
                break
            raw_bytes = len(line.encode("utf-8")) + len(separator.encode("utf-8"))
            self._pending = self._pending[len(line) + len(separator) :]
            if line == "":
                event = self._close_event()
                if event is not None:
                    events.append(event)
                if self._failed:
                    break
                continue
            if line.startswith(":"):
                continue
            field, value = _split_field(line)
            if field == "data":
                self._data_lines.append(value)
                self._data_bytes += raw_bytes
        return events

    def _close_event(self) -> SseEvent | None:
        if not self._data_lines:
            return None
        payload = "\n".join(self._data_lines)
        self._data_lines = []
        self._data_bytes = 0
        if payload == "[DONE]":
            return SseDone()
        return SseData(payload)

    def _fail(self, reason: str) -> list[SseEvent]:
        self._failed = True
        self._pending = ""
        self._data_lines = []
        self._data_bytes = 0
        return [SseProtocolError(reason)]


def _prefix_within(text: str, room: int) -> int:
    """Return how many characters fit in ``room`` UTF-8 bytes."""
    used = 0
    count = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if used + size > room:
            break
        used += size
        count += 1
    return count


def _next_line(text: str) -> tuple[str | None, str | None]:
    for index, character in enumerate(text):
        if character == "\n":
            line = text[:index]
            if line.endswith("\r"):
                line = line[:-1]
            return line, text[len(line) : index + 1]
        if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
            return text[:index], "\r\n"
    return None, None


def _split_field(line: str) -> tuple[str, str]:
    name, separator, value = line.partition(":")
    if not separator:
        return name, ""
    if value.startswith(" "):
        value = value[1:]
    return name, value
