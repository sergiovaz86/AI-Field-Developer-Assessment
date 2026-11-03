"""Deterministic OpenAI-compatible chat mocks for deployment acceptance.

``primary`` answers HTTP 429 and does not stream a body the client can see.
``backup`` streams the existing mock-model scripts, one SSE frame at a time.
Neither role reads the bearer or calls a real provider.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gateway_service.mocks.stream_scenarios import render_completion

LOGGER = logging.getLogger("gateway_service.mocks.llm_http")
_MAX_BODY_BYTES = 1_048_576


class LlmMockHandler(BaseHTTPRequestHandler):
    """One role, selected by the ``role`` class attribute."""

    protocol_version = "HTTP/1.1"
    role = "backup"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        self._send(200, b'{"status":"ok"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.split("?", 1)[0].endswith("/chat/completions"):
            self.send_error(404)
            return
        raw = self._read_body()
        if raw is None:
            return
        if self.role == "primary":
            LOGGER.info("llm mock role=primary status=429")
            self._send(429, b"{}", "application/json")
            return
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            self._send(400, b"{}", "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b"{}", "application/json")
            return
        frames = render_completion(payload)
        LOGGER.info("llm mock role=backup status=200")
        self._send(200, b"".join(frames), "text/event-stream", frames=frames)

    def log_message(self, format: str, *args: object) -> None:
        """Omit the default access line so request bodies stay off stderr."""
        return

    def _read_body(self) -> bytes | None:
        length_header = self.headers.get("Content-Length", "0")
        if not length_header.isdecimal():
            self.send_error(400)
            return None
        length = int(length_header)
        if length > _MAX_BODY_BYTES:
            self.send_error(400)
            return None
        return self.rfile.read(length)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        frames: list[bytes] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        pieces = frames if frames is not None else [body]
        for piece in pieces:
            self.wfile.write(piece)
            self.wfile.flush()


def bind(host: str, port: int, role: str) -> ThreadingHTTPServer:
    """Bind one mock role without serving yet."""
    if role not in {"primary", "backup"}:
        raise ValueError("role must be primary or backup")
    handler = type("BoundLlmMockHandler", (LlmMockHandler,), {"role": role})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    """Listen until the process is stopped."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    role = os.environ.get("LLM_MOCK_ROLE", "")
    if role not in {"primary", "backup"}:
        sys.stderr.write("LLM_MOCK_ROLE must be primary or backup\n")
        raise SystemExit(1)
    raw_port = os.environ.get("LLM_MOCK_PORT", "9100")
    if not raw_port.isdecimal():
        sys.stderr.write("LLM_MOCK_PORT must be a canonical integer\n")
        raise SystemExit(1)
    host = os.environ.get("LLM_MOCK_HOST", "0.0.0.0")
    server = bind(host, int(raw_port), role)
    LOGGER.info("llm mock role=%s listening", role)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
