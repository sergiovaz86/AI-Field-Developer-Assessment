"""JSON-RPC envelope constants for the gateway.

``POST /mcp`` carries JSON-RPC 2.0. Protocol failures use these codes.
HTTP 401 is reserved for authentication and is not a JSON-RPC body.
"""

from typing import Final

PARSE_ERROR_CODE: Final[int] = -32700
PARSE_ERROR_MESSAGE: Final[str] = "Parse error"
INVALID_REQUEST_CODE: Final[int] = -32600
INVALID_REQUEST_MESSAGE: Final[str] = "Invalid Request"
INVALID_PARAMS_CODE: Final[int] = -32602
INVALID_PARAMS_MESSAGE: Final[str] = "Invalid params"
INTERNAL_ERROR_CODE: Final[int] = -32603
INTERNAL_ERROR_MESSAGE: Final[str] = "Internal error"
