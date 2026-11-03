"""SDK 2.3.0 compatibility boundary.

``MCPServer._handle_call_tool`` and ``MCPServer._tool_manager`` are private.
This module is the only Task 1 code that uses them. ``pyproject.toml`` pins
``mcp==2.3.0``. Wire tests assert the frames this boundary produces.

Do not copy this override into another module, and do not replace the SDK
stdio transport to change it.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolRequestParams, CallToolResult, TextContent
from pydantic import ValidationError

from task1_mcp_server.errors import INVALID_PARAMS_CODE, INVALID_PARAMS_MESSAGE
from task1_mcp_server.schemas import StrictArguments

PINNED_MCP_VERSION = "2.3.0"
LOGGER = logging.getLogger("task1_mcp_server.sdk_compat")


def assert_pinned_sdk() -> None:
    """Fail closed when the installed SDK is not the pinned release."""
    installed = importlib.metadata.version("mcp")
    if installed != PINNED_MCP_VERSION:
        raise RuntimeError(
            f"task1_mcp_server requires mcp=={PINNED_MCP_VERSION}; found mcp=={installed}"
        )
    if INVALID_PARAMS_CODE != INVALID_PARAMS:
        raise RuntimeError("local invalid-params code drifted from the SDK")


class InvalidParamsDispatch(MCPServer[Any]):
    """Map tool-argument validation failures to JSON-RPC ``-32602``.

    SDK 2.3.0 would otherwise return ``CallToolResult(isError=true)``.
    Business ``ToolError`` values stay on that ``isError`` path.
    """

    async def _handle_call_tool(self, ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        from mcp.server.mcpserver.context import Context

        context = Context(
            request_context=ctx,
            mcp_server=self,
            input_params=params,
            subscriptions=self._subscriptions,
        )
        try:
            result = await self.call_tool(params.name, params.arguments or {}, context)
        except MCPError:
            raise
        except Exception as exc:
            validation = _validation_cause(exc)
            if validation is not None:
                fields = sorted(
                    ".".join(str(part) for part in error["loc"]) for error in validation.errors()
                )
                LOGGER.info("tool %s rejected arguments fields=%s", params.name, fields)
                raise MCPError(code=INVALID_PARAMS, message=INVALID_PARAMS_MESSAGE) from None
            if isinstance(exc, UnexpectedToolError):
                LOGGER.exception("tool %s failed unexpectedly", params.name)
                return CallToolResult(
                    content=[TextContent(type="text", text=str(exc))],
                    is_error=True,
                )
            if isinstance(exc, ToolError):
                LOGGER.info("tool %s failed: %s", params.name, exc)
                return CallToolResult(
                    content=[TextContent(type="text", text=str(exc))],
                    is_error=True,
                )
            LOGGER.exception("tool %s failed unexpectedly", params.name)
            return CallToolResult(
                content=[TextContent(type="text", text="Error executing tool")],
                is_error=True,
            )
        if not isinstance(result, CallToolResult):
            raise RuntimeError("tool result is not a CallToolResult")
        return result


def install_strict_arguments(
    server: MCPServer[Any],
    tool_name: str,
    model: type[StrictArguments],
) -> None:
    """Replace the SDK's loose argument model for one registered tool."""
    from task1_mcp_server.schemas import argument_json_schema

    tool = server._tool_manager.get_tool(tool_name)
    if tool is None:
        raise RuntimeError(f"tool {tool_name} is not registered")
    tool.fn_metadata.arg_model = model
    tool.parameters = argument_json_schema(model)


def _validation_cause(exc: BaseException) -> ValidationError | None:
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            return None
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__
    return None
