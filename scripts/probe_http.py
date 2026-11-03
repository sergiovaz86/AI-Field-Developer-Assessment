"""Exit 0 when one URL returns an HTTP response.

The Compose health check calls this with the URL as one argument. A shell
that splits an unquoted ``python -c`` string cannot do that reliably.
"""

from __future__ import annotations

import sys
import urllib.request


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: probe_http.py URL\n")
        raise SystemExit(2)
    urllib.request.urlopen(args[0], timeout=2).read()


if __name__ == "__main__":
    main()
