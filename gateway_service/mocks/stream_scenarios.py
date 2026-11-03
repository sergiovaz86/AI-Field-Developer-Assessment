"""Deterministic upstream SSE scripts for the mock chat provider.

Model names select the script. The scripts are the only completion text the
offline mock emits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def content_deltas(model: str, messages: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return content fragments for a named mock model."""
    if model == "mock-chars":
        return list(_last_user_text(messages))
    scripted = _SCRIPTS.get(model)
    if scripted is not None:
        return list(scripted)
    return ["Hello from the mock provider."]


def render_completion(payload: Mapping[str, Any], *, line_ending: str = "\n") -> list[bytes]:
    """Render one completion as SSE frames, including the terminal marker."""
    model = str(payload.get("model", "mock"))
    if model == "mock-tool":
        return _tool_frames(model, line_ending)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    deltas = content_deltas(model, messages)
    frames = [_frame(_chunk(model, {"role": "assistant"}), line_ending)]
    for delta in deltas:
        frames.append(_frame(_chunk(model, {"content": delta}), line_ending))
    frames.append(
        _frame(
            {
                **_chunk(model, {}),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": len(deltas),
                    "total_tokens": 1 + len(deltas),
                },
            },
            line_ending,
        )
    )
    ending = "\r\n" if line_ending == "\r\n" else "\n"
    frames.append(f"data: [DONE]{ending}{ending}".encode())
    return frames


def _chunk(model: str, delta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def _frame(payload: dict[str, Any], line_ending: str) -> bytes:
    ending = "\r\n" if line_ending == "\r\n" else "\n"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"data: {encoded}{ending}{ending}".encode()


def _last_user_text(messages: Sequence[Mapping[str, Any]]) -> str:
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            return content
    return ""


_SCRIPTS: dict[str, tuple[str, ...]] = {
    "mock-email-parts": ("alex", "@", "example", ".", "com"),
    "mock-email": ("Reach me at alex@example.com today",),
    "mock-ssn": ("SSN 123-45-6789 is on file",),
    "mock-ssn-invalid": ("SSN 000-12-3456 stays",),
    "mock-card": ("4111111111111111",),
    "mock-card-bad": ("4111111111111112",),
    "mock-card-spaces": ("4111 1111 1111 1111",),
    "mock-card-hyphens": ("4111-1111-1111-1111",),
    "mock-multi": ("alex@example.com and 123-45-6789 and 4111111111111111.",),
    "mock-adjacent": ("alex@example.com123-45-6789",),
    "mock-prefix-end": ("see alex@example.com",),
    "mock-utf8": ("café ",),
}


def _tool_frames(model: str, line_ending: str) -> list[bytes]:
    """Stream a tool-call argument one character at a time."""
    argument = '{"email":"alex@example.com"}'
    frames = [
        _frame(_chunk(model, {"role": "assistant"}), line_ending),
        _frame(
            _chunk(
                model,
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_mock",
                            "type": "function",
                            "function": {"name": "lookup"},
                        }
                    ]
                },
            ),
            line_ending,
        ),
    ]
    for character in argument:
        frames.append(
            _frame(
                _chunk(
                    model,
                    {"tool_calls": [{"index": 0, "function": {"arguments": character}}]},
                ),
                line_ending,
            )
        )
    frames.append(
        _frame(
            {
                **_chunk(model, {}),
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            },
            line_ending,
        )
    )
    ending = "\r\n" if line_ending == "\r\n" else "\n"
    frames.append(f"data: [DONE]{ending}{ending}".encode())
    return frames
