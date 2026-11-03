"""HTTP socket for the existing downstream JSON-RPC mock.

``mock_response`` stays the protocol implementation. This module only reads a
POST body and writes that response. It does not read Authorization and it
does not attach to Task 1.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from gateway_service.mocks.mcp_downstream import mock_response

LOGGER = logging.getLogger("gateway_service.mocks.mcp_http")
_MAX_BODY_BYTES = 1_048_576


class DownstreamHandler(BaseHTTPRequestHandler):
    """Serve the mock catalog on one process-local HTTP port."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/health":
            self.send_error(404)
            return
        self._write(200, b'{"status":"ok"}')

    def do_POST(self) -> None:  # noqa: N802
        length_header = self.headers.get("Content-Length", "0")
        if not length_header.isdecimal():
            self.send_error(400)
            return
        length = int(length_header)
        if length > _MAX_BODY_BYTES:
            self.send_error(400)
            return
        raw = self.rfile.read(length)
        self._log_request(raw)
        response = mock_response(httpx.Request("POST", "http://downstream/mcp", content=raw))
        self._write(response.status_code, response.content)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the default access line, which includes the request body, off stderr."""
        return

    def _write(self, status: int, body: bytes) -> None:
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 204 and body:
            self.wfile.write(body)

    def _log_request(self, raw: bytes) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.info("downstream request method=unparsed")
            return
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            method, name = _operation(item)
            if name is None:
                LOGGER.info("downstream request method=%s", method)
            else:
                LOGGER.info("downstream request method=%s name=%s", method, name)


def bind(host: str, port: int) -> ThreadingHTTPServer:
    """Bind the mock without serving yet."""
    return ThreadingHTTPServer((host, port), DownstreamHandler)


def main() -> None:
    """Listen until the process is stopped. Defaults are the Compose address."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    host = os.environ.get("DOWNSTREAM_HOST", "0.0.0.0")
    raw_port = os.environ.get("DOWNSTREAM_PORT", "9000")
    if not raw_port.isdecimal():
        sys.stderr.write("DOWNSTREAM_PORT must be a canonical integer\n")
        raise SystemExit(1)
    server = bind(host, int(raw_port))
    LOGGER.info("downstream mock listening")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _operation(item: object) -> tuple[str, str | None]:
    if not isinstance(item, dict):
        return "invalid", None
    method = item.get("method")
    method_text = method if isinstance(method, str) else "invalid"
    params: Any = item.get("params")
    if not isinstance(params, dict):
        return method_text, None
    name = params.get("name")
    if not isinstance(name, str):
        return method_text, None
    return method_text, name


if __name__ == "__main__":
    main()
