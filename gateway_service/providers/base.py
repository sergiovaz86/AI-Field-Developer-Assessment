"""Provider streaming boundary.

Adapters yield raw SSE bytes. The guardrail does not import a vendor SDK.
An adapter reports one attempt. Failover policy lives in Task 4 and is not
implemented inside an adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol


class UpstreamFailure(Exception):
    """Sanitized upstream failure.

    ``category`` is a stable token such as ``timeout``, ``network``,
    ``http``, ``http_429``, ``http_5xx``, ``protocol``, or ``unconfigured``.
    The message is that token and must not carry a URL, credential, or body.
    """

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category
        self.after_response_byte = False


class ChatUpstream(Protocol):
    """Source of OpenAI-compatible chat-completion SSE bytes."""

    def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[bytes]:
        """Yield upstream body chunks for one completion request."""

    async def aclose(self) -> None:
        """Release sockets or other resources held by the adapter."""
