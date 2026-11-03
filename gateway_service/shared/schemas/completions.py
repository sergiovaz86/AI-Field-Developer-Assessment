"""OpenAI-compatible chat-completion request boundary.

``stream`` must be true. Non-streaming requests are rejected so generated
text cannot skip the streaming redactor. Extra provider fields are accepted
and forwarded to the selected upstream.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """One chat message supplied by the client."""

    model_config = ConfigDict(extra="allow", strict=True)

    role: str = Field(min_length=1, max_length=64)
    content: Any = None


class ChatCompletionRequest(BaseModel):
    """Client body for ``POST /v1/chat/completions``."""

    model_config = ConfigDict(extra="allow", strict=True)

    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=128)
    stream: bool | None = None
