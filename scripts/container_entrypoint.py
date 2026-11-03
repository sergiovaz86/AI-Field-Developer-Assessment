"""Prepare the ledger directory, then run the container command as ``gateway``.

The image starts as root so a fresh named volume can be given to that user.
The long-running process is the dropped-privilege command. This script does
not migrate the database and does not write diagnostics that include
configuration values.
"""

from __future__ import annotations

import os
import pwd
import sys

_RUNTIME_USER = "gateway"


def main(argv: list[str] | None = None) -> None:
    """Chown the ledger directory when root, then exec the requested command."""
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        command = [sys.executable, "-m", "gateway_service"]
    if os.geteuid() == 0:
        account = _runtime_account()
        _prepare_runtime_directory(account)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.execvp(command[0], command)


def _runtime_account() -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(_RUNTIME_USER)
    except KeyError:
        sys.stderr.write("runtime user is missing\n")
        raise SystemExit(1) from None


def _prepare_runtime_directory(account: pwd.struct_passwd) -> None:
    configured = os.environ.get("GATEWAY_DATABASE_PATH", ".runtime/gateway.db")
    parent = os.path.dirname(configured) if configured.strip() else ""
    directory = parent if parent else ".runtime"
    os.makedirs(directory, mode=0o750, exist_ok=True)
    os.chown(directory, account.pw_uid, account.pw_gid, follow_symlinks=False)
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(current, name)
            if os.path.islink(path):
                continue
            os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)


if __name__ == "__main__":
    main()
