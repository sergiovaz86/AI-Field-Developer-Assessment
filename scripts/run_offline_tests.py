"""Run the offline pytest suite from the project root."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    """Delegate to pytest. This script does not open a network connection."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=ROOT,
        check=False,
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
