"""Command line: apps call `run` (and it calls `daemon`); people call the rest."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from mcp_stdio_mux import __version__


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else ""
    try:
        if command == "run":
            from mcp_stdio_mux.runtime import run

            return run(arguments[1:])
        if command == "daemon":
            from mcp_stdio_mux.daemon import run_daemon

            return run_daemon(arguments[1:])
    except (OSError, RuntimeError, ValueError) as error:
        print(f"mcp-stdio-mux: {error}", file=sys.stderr)
        return 1
    if command in ("--version", "-V"):
        print(f"mcp-stdio-mux {__version__}")
        return 0
    print("usage: mcp-stdio-mux {run,daemon,--version} ...", file=sys.stderr)
    return 2
