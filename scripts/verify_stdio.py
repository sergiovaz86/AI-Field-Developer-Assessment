"""Confirm the Task 1 process writes nothing to stdout.

Stdin is closed immediately. The server must exit without emitting a frame,
because there is no request to answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Run python -m task1_mcp_server and require empty stdout."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "task1_mcp_server"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if completed.stdout:
        sys.stderr.write("stdout is not empty\n")
        raise SystemExit(1)
    if completed.returncode != 0:
        sys.stderr.write(f"unexpected exit status {completed.returncode}\n")
        sys.stderr.buffer.write(completed.stderr)
        raise SystemExit(completed.returncode or 1)
    if b"Traceback" in completed.stderr:
        sys.stderr.write("stderr contains a traceback\n")
        sys.stderr.buffer.write(completed.stderr)
        raise SystemExit(1)
    sys.stderr.write("stdio isolation check passed\n")


if __name__ == "__main__":
    main()
