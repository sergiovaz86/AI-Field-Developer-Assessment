"""get_customer_record tool."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError

from task1_mcp_server.customer_store import CustomerStore
from task1_mcp_server.errors import UNKNOWN_CUSTOMER_MESSAGE
from task1_mcp_server.schemas import CustomerRecord


def build_get_customer_record(store: CustomerStore) -> Callable[[str], CustomerRecord]:
    """Bind the tool to one process store."""

    def get_customer_record(customer_id: str) -> CustomerRecord:
        """Return the deterministic in-memory record for a customer id."""
        record = store.get_customer(customer_id)
        if record is None:
            raise ToolError(UNKNOWN_CUSTOMER_MESSAGE)
        return record

    return get_customer_record
