"""Tool-call authorization boundary.

``tools/list`` and every method other than ``tools/call`` are forwarded for
every authenticated role. ``tools/call`` names that start with ``admin_``
require the admin role. Denied calls use the shared unauthorized-tool error
and are not forwarded. A role field inside the JSON-RPC body is ignored.
"""

from typing import Final, Literal

from gateway_service.shared.schemas.errors import (
    UNAUTHORIZED_TOOL_CALL_CODE,
    UNAUTHORIZED_TOOL_CALL_MESSAGE,
)
from gateway_service.task2_mcp_gateway.authentication import Role
from gateway_service.task2_mcp_gateway.jsonrpc_validation import Forwardable

ADMIN_TOOL_PREFIX: Final[str] = "admin_"
Decision = Literal["forward", "deny"]


def authorize(role: Role, call: Forwardable) -> Decision:
    """Decide whether one already validated call may be forwarded."""
    if call.method != "tools/call" or not isinstance(call.params, dict):
        return "forward"
    name = call.params.get("name")
    if isinstance(name, str) and name.startswith(ADMIN_TOOL_PREFIX) and role != "admin":
        return "deny"
    return "forward"


__all__ = [
    "ADMIN_TOOL_PREFIX",
    "UNAUTHORIZED_TOOL_CALL_CODE",
    "UNAUTHORIZED_TOOL_CALL_MESSAGE",
    "authorize",
]
