"""trigger_refund tool."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError

from task1_mcp_server.customer_store import CustomerStore, UnknownCustomer
from task1_mcp_server.errors import UNKNOWN_CUSTOMER_MESSAGE
from task1_mcp_server.schemas import RefundRecord


def build_trigger_refund(store: CustomerStore) -> Callable[[str, float, str], RefundRecord]:
    """Bind the tool to one process store."""

    def trigger_refund(customer_id: str, amount: float, reason: str) -> RefundRecord:
        """Record a simulated refund for a known customer."""
        try:
            return store.trigger_refund(customer_id, amount, reason)
        except UnknownCustomer:
            raise ToolError(UNKNOWN_CUSTOMER_MESSAGE) from None

    return trigger_refund
