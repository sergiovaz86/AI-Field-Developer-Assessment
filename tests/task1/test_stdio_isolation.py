"""Stdio isolation checks for the Task 1 process."""

from __future__ import annotations

import ast
import importlib.metadata
import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TASK1_ROOT = PROJECT_ROOT / "task1_mcp_server"


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(PROJECT_ROOT), env.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_entry_point_writes_no_stdout_and_exits_when_stdin_closes() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "task1_mcp_server"],
        cwd=PROJECT_ROOT,
        env=_isolated_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.stdout == b""
    assert completed.returncode == 0
    assert b"Traceback" not in completed.stderr


def test_private_sdk_override_is_isolated_and_pinned() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mcp==2.3.0" in pyproject["project"]["dependencies"]
    assert importlib.metadata.version("mcp") == "2.3.0"
    private_names = {"_handle_call_tool", "_tool_manager", "_subscriptions"}
    for path in TASK1_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in private_names:
                    assert path.name == "sdk_compat.py", path
            if isinstance(node, ast.Attribute) and node.attr in private_names:
                assert path.name == "sdk_compat.py", path


def test_task1_sources_do_not_touch_stdout_or_gateway() -> None:
    for path in TASK1_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    assert func.id not in {"print", "basicConfig"}, path
                if isinstance(func, ast.Attribute):
                    assert func.attr != "basicConfig", path
            if isinstance(node, ast.Attribute):
                assert node.attr != "stdout", path
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert not node.module.startswith("gateway_service"), path
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
                assert "gateway_service" not in imported
                assert not any(name.startswith("gateway_service.") for name in imported)
