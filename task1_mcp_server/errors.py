"""Task 1 protocol and tool error identifiers.

SDK 2.3.0 ``MCPServer._handle_call_tool`` turns a tool-argument
``ValidationError`` into ``CallToolResult(isError=true)``. That is not the
assessment contract. ``sdk_compat.InvalidParamsDispatch`` is the only override
of that private method. It raises ``MCPError`` with code ``-32602`` and
message ``Invalid params``, and leaves business failures on the SDK's
``isError=true`` path.

``Invalid params`` is the JSON-RPC 2.0 message for this code. The architecture
label "Invalid Params" names the same code.
"""

from typing import Final

INVALID_PARAMS_CODE: Final[int] = -32602
INVALID_PARAMS_MESSAGE: Final[str] = "Invalid params"
UNKNOWN_CUSTOMER_MESSAGE: Final[str] = "Unknown customer_id"
