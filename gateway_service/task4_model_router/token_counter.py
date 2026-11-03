"""Input estimates and the enforced output-token budget.

Estimates use tiktoken ``cl100k_base``. The output budget is the configured
default when the client omits a limit, and it is clamped to the configured
ceiling when the client asks for more. The same budget is reserved and sent
upstream as ``max_tokens``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tiktoken
from tiktoken.load import load_tiktoken_bpe

_ENCODER: tiktoken.Encoding | None = None
_BPE_PATH = Path(__file__).with_name("data") / "cl100k_base.tiktoken"
_BPE_HASH = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
_PATTERN = (
    r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+|"""
    r""" ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
)


class OutputBudgetError(Exception):
    """The client supplied an output limit that cannot be reserved."""


def estimate_input_tokens(payload: Mapping[str, Any]) -> int:
    """Count model, role, and message text with ``cl100k_base``."""
    parts: list[str] = []
    model = payload.get("model")
    if isinstance(model, str):
        parts.append(model)
    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if isinstance(role, str):
                parts.append(role)
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif content is not None:
                parts.append(
                    json.dumps(
                        content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
    text = "\n".join(parts)
    if text == "":
        return 0
    return len(_encoder().encode(text))


def output_budget(payload: Mapping[str, Any], *, default: int, ceiling: int) -> int:
    """Return the output budget that will be reserved and sent upstream."""
    if "max_completion_tokens" in payload:
        raw = payload.get("max_completion_tokens")
    else:
        raw = payload.get("max_tokens")
    if raw is None:
        chosen = default
    elif isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise OutputBudgetError
    else:
        chosen = raw
    if chosen > ceiling:
        return ceiling
    return chosen


def _encoder() -> tiktoken.Encoding:
    """Load the vendored cl100k_base ranks. Tests do not fetch them."""
    global _ENCODER
    if _ENCODER is None:
        ranks = load_tiktoken_bpe(str(_BPE_PATH), expected_hash=_BPE_HASH)
        _ENCODER = tiktoken.Encoding(
            name="cl100k_base",
            pat_str=_PATTERN,
            mergeable_ranks=ranks,
            special_tokens={
                "<|endoftext|>": 100257,
                "<|fim_prefix|>": 100258,
                "<|fim_middle|>": 100259,
                "<|fim_suffix|>": 100260,
                "<|endofprompt|>": 100276,
            },
        )
    return _ENCODER
