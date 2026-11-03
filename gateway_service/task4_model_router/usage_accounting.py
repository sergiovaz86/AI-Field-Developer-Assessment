"""Read provider usage from SSE frames without changing those frames.

A missing usage object keeps the original reservation. That conservative
fallback is applied by the ledger, not by inventing token counts here.

When ``prompt_tokens`` and ``completion_tokens`` are both non-negative
integers, both are retained. A usage object that lacks that pair is recorded
as incomplete and does not replace the reservation. Boolean values are not
treated as integers.
"""

from __future__ import annotations

import json

from gateway_service.task3_stream_guardrail.sse_parser import MAX_SSE_EVENT_BYTES


class UsageSniffer:
    """Bounded scan of upstream SSE data lines for a usage object."""

    def __init__(self) -> None:
        self._pending = ""
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.saw_usage = False

    def feed(self, chunk: bytes) -> None:
        if len(self._pending) > MAX_SSE_EVENT_BYTES:
            self._pending = ""
            return
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            self._pending = ""
            return
        self._pending += text
        if len(self._pending) > MAX_SSE_EVENT_BYTES:
            self._pending = ""
            return
        while "\n\n" in self._pending:
            block, _, rest = self._pending.partition("\n\n")
            self._pending = rest
            self._read_block(block.replace("\r\n", "\n").replace("\r", "\n"))

    def _read_block(self, block: str) -> None:
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            return
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        if _non_negative_int(prompt) and _non_negative_int(completion):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.saw_usage = True


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
