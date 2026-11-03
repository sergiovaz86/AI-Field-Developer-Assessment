"""MCP handshake, framing, and process lifecycle."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp.types import InitializeResult
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION
from tests.task1.conftest import open_task1_session

ROOT = Path(__file__).resolve().parents[2]


async def test_initialize_negotiates_protocol_and_lists_two_tools(tmp_path: Path) -> None:
    async with open_task1_session(tmp_path) as (session, initialized, _stderr):
        await _assert_initialize(session, initialized)


async def _assert_initialize(session: ClientSession, initialized: InitializeResult) -> None:
    assert initialized.protocol_version in HANDSHAKE_PROTOCOL_VERSIONS
    assert initialized.protocol_version == LATEST_HANDSHAKE_VERSION
    assert initialized.server_info.name == "task1-mcp-server"
    assert initialized.server_info.version == "0.1.0"
    assert initialized.capabilities.tools is not None
    listed = await session.list_tools()
    assert [tool.name for tool in listed.tools] == ["get_customer_record", "trigger_refund"]
    lookup = listed.tools[0].input_schema
    refund = listed.tools[1].input_schema
    assert lookup["additionalProperties"] is False
    assert lookup["properties"]["customer_id"]["pattern"] == r"^CUST-[0-9]{5}$"
    assert refund["properties"]["amount"] == {"type": "number", "exclusiveMinimum": 0}
    assert refund["properties"]["reason"]["minLength"] == 10


async def test_rejected_call_does_not_block_the_next_call(tmp_path: Path) -> None:
    async with open_task1_session(tmp_path) as (session, _initialized, stderr_path):
        await _assert_recovery(session, stderr_path)


async def _assert_recovery(session: ClientSession, stderr_path: Path) -> None:
    with pytest.raises(MCPError) as caught:
        await session.call_tool("get_customer_record", {"customer_id": "CUST-1", "extra": True})
    assert caught.value.code == -32602
    recovered = await session.call_tool("get_customer_record", {"customer_id": "CUST-10002"})
    assert recovered.is_error is False
    assert recovered.structured_content is not None
    assert recovered.structured_content["full_name"] == "Blake Ortiz"
    assert recovered.structured_content["refund_count"] == 0
    assert "Traceback" not in stderr_path.read_text(encoding="utf-8")


async def test_client_session_shuts_the_subprocess_down(tmp_path: Path) -> None:
    stderr = (tmp_path / "server.stderr").open("w", encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        with anyio.fail_after(8):
            async with stdio_client(params, errlog=stderr) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
    finally:
        stderr.close()


def test_wire_frames_preserve_ids_and_validation_errors(tmp_path: Path) -> None:
    proc, stderr_path = _spawn(tmp_path)
    assert proc.stdin is not None
    assert proc.stdout is not None
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "task1-wire-test", "version": "0.0.0"},
                },
            },
        )
        init = _read(proc)
        assert init["id"] == 1
        assert init["result"]["protocolVersion"] == LATEST_HANDSHAKE_VERSION
        assert init["result"]["serverInfo"]["name"] == "task1-mcp-server"
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _assert_unanswered(proc)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": "bad-1",
                "method": "tools/call",
                "params": {
                    "name": "trigger_refund",
                    "arguments": {
                        "customer_id": "CUST-10001",
                        "amount": True,
                        "reason": "1234567890",
                    },
                },
            },
        )
        rejected = _read(proc)
        assert rejected["id"] == "bad-1"
        assert rejected["error"] == {"code": -32602, "message": "Invalid params"}
        assert "result" not in rejected
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "get_customer_record",
                    "arguments": {"customer_id": "CUST-10001"},
                },
            },
        )
        fetched = _read(proc)
        assert fetched["id"] == 13
        assert fetched["result"]["isError"] is False
        assert fetched["result"]["structuredContent"]["refund_count"] == 0
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        assert proc.stdout.read() == b""
        assert b"Traceback" not in stderr_path.read_bytes()
    finally:
        _stop(proc)


def test_malformed_envelopes_stay_unanswered(tmp_path: Path) -> None:
    """Silence is not a JSON-RPC result.

    SDK 2.3.0 drops a missing method, non-JSON text, and a boolean id. This
    test bounds that wait. It does not read a later success as the malformed
    request's response.
    """
    proc, stderr_path = _spawn(tmp_path)
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_HANDSHAKE_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "task1-wire-test", "version": "0.0.0"},
                },
            },
        )
        assert _read(proc)["id"] == 1
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        for payload in (
            {"jsonrpc": "2.0", "id": 9},
            b"not-json\n",
            {"jsonrpc": "2.0", "id": True, "method": "tools/list"},
        ):
            _send(proc, payload)
            _assert_unanswered(proc)
        _send(proc, {"jsonrpc": "2.0", "id": 12, "method": "tools/list"})
        listed = _read(proc)
        assert listed["id"] == 12
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "get_customer_record",
            "trigger_refund",
        ]
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
        assert b"Traceback" not in stderr_path.read_bytes()
    finally:
        _stop(proc)


def _spawn(tmp_path: Path) -> tuple[subprocess.Popen[bytes], Path]:
    stderr_path = tmp_path / "server.stderr"
    stderr = stderr_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "task1_mcp_server"],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
    )
    stderr.close()
    return proc, stderr_path


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def _send(proc: subprocess.Popen[bytes], payload: dict[str, object] | bytes) -> None:
    assert proc.stdin is not None
    if isinstance(payload, bytes):
        proc.stdin.write(payload)
    else:
        proc.stdin.write(json.dumps(payload).encode() + b"\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen[bytes]) -> dict[str, object]:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 5)
    assert ready, "timed out waiting for a JSON-RPC response"
    line = proc.stdout.readline()
    assert line.endswith(b"\n")
    assert b"\n" not in line[:-1]
    body = json.loads(line)
    assert isinstance(body, dict)
    assert body["jsonrpc"] == "2.0"
    return body


def _assert_unanswered(proc: subprocess.Popen[bytes]) -> None:
    """Fail if stdout yields any byte within the silence bound.

    A blocking read is not used. An unanswered malformed message is not
    treated as a completed exchange.
    """
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 0.2)
    if not ready:
        return
    leaked = os.read(proc.stdout.fileno(), 4096)
    raise AssertionError(f"unanswered input produced stdout bytes: {leaked!r}")
