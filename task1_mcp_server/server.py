"""Task 1 composition root.

The official MCP SDK owns stdio framing, initialization, and tool dispatch.
The ``-32602`` argument boundary lives only in ``sdk_compat`` and is pinned
to MCP SDK 2.3.0.
"""

from __future__ import annotations

import logging
import sys

from task1_mcp_server.customer_store import CustomerStore
from task1_mcp_server.schemas import GetCustomerRecordArguments, TriggerRefundArguments
from task1_mcp_server.sdk_compat import (
    InvalidParamsDispatch,
    assert_pinned_sdk,
    install_strict_arguments,
)
from task1_mcp_server.tools.get_customer_record import build_get_customer_record
from task1_mcp_server.tools.trigger_refund import build_trigger_refund

LOGGER = logging.getLogger("task1_mcp_server")

_CONFIGURED = False


def configure_stdio_safe_logging(level: int = logging.INFO) -> None:
    """Send diagnostics to stderr and leave stdout untouched.

    Existing root handlers are removed first so an implicit handler cannot
    attach to stdout. Call this before constructing ``MCPServer``; the SDK's
    ``basicConfig`` then leaves the stderr handler in place.
    """
    global _CONFIGURED
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


class Task1Server(InvalidParamsDispatch):
    """Assessment server. Private SDK dispatch stays in ``sdk_compat``."""

    def __init__(self, store: CustomerStore | None = None) -> None:
        super().__init__(
            name="task1-mcp-server",
            version="0.1.0",
            instructions="Deterministic customer lookup and simulated refunds.",
            log_level="INFO",
        )
        self.store = store if store is not None else CustomerStore()
        self.add_tool(
            build_get_customer_record(self.store),
            name="get_customer_record",
            description="Return the deterministic in-memory record for a customer id.",
        )
        self.add_tool(
            build_trigger_refund(self.store),
            name="trigger_refund",
            description="Record a simulated refund for a known customer.",
        )
        install_strict_arguments(self, "get_customer_record", GetCustomerRecordArguments)
        install_strict_arguments(self, "trigger_refund", TriggerRefundArguments)


def create_server(store: CustomerStore | None = None) -> Task1Server:
    """Build a server with a private in-memory store."""
    return Task1Server(store)


def main() -> None:
    """Serve MCP on stdin and stdout until stdin closes."""
    configure_stdio_safe_logging()
    assert_pinned_sdk()
    LOGGER.info("stdio transport starting")
    create_server().run(transport="stdio")
