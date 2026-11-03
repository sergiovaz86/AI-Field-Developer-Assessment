"""Public error identifiers owned by the gateway.

Task 2 uses the unauthorized-tool code below. Completion responses use the
OpenAI-compatible codes. Internal details stay out of client responses.
"""

from typing import Final

UNAUTHORIZED_TOOL_CALL_CODE: Final[int] = -32001
UNAUTHORIZED_TOOL_CALL_MESSAGE: Final[str] = "Unauthorized Tool Call"

INVALID_API_KEY_CODE: Final[str] = "invalid_api_key"
INVALID_API_KEY_MESSAGE: Final[str] = "Authentication required"
QUOTA_EXCEEDED_CODE: Final[str] = "quota_exceeded"
QUOTA_EXCEEDED_MESSAGE: Final[str] = "The token quota for this tenant has been reached."
DATABASE_UNAVAILABLE_CODE: Final[str] = "database_unavailable"
DATABASE_UNAVAILABLE_MESSAGE: Final[str] = "The service is temporarily unavailable."
