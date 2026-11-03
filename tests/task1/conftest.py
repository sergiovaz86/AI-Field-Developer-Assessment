"""Stdio client helper for Task 1.

The session is an async context manager, not a pytest fixture. AnyIO cancel
scopes must be entered and exited in the same task, and a yielded async
fixture is finalized on another task.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import InitializeResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def open_task1_session(
    tmp_path: Path,
) -> AsyncIterator[tuple[ClientSession, InitializeResult, Path]]:
    """Start one Task 1 subprocess and initialize an official MCP session."""
    stderr_path = tmp_path / "server.stderr"
    stderr = stderr_path.open("w", encoding="utf-8", buffering=1)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        async with stdio_client(params, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                yield session, initialized, stderr_path
    finally:
        stderr.close()
