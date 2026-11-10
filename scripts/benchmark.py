"""Measure what N agent sessions of one MCP server cost, with and without a mux.

    python scripts/benchmark.py --sessions 4 -- tilth --mcp
    python scripts/benchmark.py --sessions 4 \\
        --prefix "mcp-stdio-mux run --domain bench --server tilth" -- tilth --mcp

Each session runs initialize + tools/list. After a settle delay, everything the
sessions caused (every descendant process, daemons and backends included) is
measured: summed RSS everywhere, summed phys_footprint on macOS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping

UNITS = {"B": 1 / 1024, "KB": 1, "MB": 1024, "GB": 1024 * 1024}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--prefix", default="", help="mux command placed before --")
    parser.add_argument("--settle", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    backend = arguments.command
    backend = backend[1:] if backend[:1] == ["--"] else backend
    prefix = shlex.split(arguments.prefix)
    command = [*prefix, "--", *backend] if prefix else backend
    runtime = tempfile.mkdtemp(prefix="mux-bench-", dir="/tmp")
    environment = {**os.environ, "MCP_STDIO_MUX_RUNTIME_DIR": runtime}
    sessions: list[subprocess.Popen] = []
    try:
        for _ in range(arguments.sessions):
            session = subprocess.Popen(
                command,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            sessions.append(session)
            handshake(session)
        time.sleep(arguments.settle)
        report(prefix, backend, sessions)
    finally:
        for session in sessions:
            session.stdin.close()
            try:
                session.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.kill()
    return 0


def handshake(session: subprocess.Popen) -> None:
    client = {"name": "benchmark", "version": "1"}
    params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client}
    send(session, {"id": 1, "method": "initialize", "params": params})
    receive(session, 1)
    send(session, {"method": "notifications/initialized"})
    send(session, {"id": 2, "method": "tools/list", "params": {}})
    receive(session, 2)


def send(session: subprocess.Popen, message: dict) -> None:
    session.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
    session.stdin.flush()


def receive(session: subprocess.Popen, message_id: int, timeout: float = 90) -> None:
    selector = selectors.DefaultSelector()
    selector.register(session.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    while selector.select(max(deadline - time.monotonic(), 0)):
        line = session.stdout.readline()
        if not line:
            break
        if json.loads(line).get("id") == message_id:
            return
    raise SystemExit(f"no reply to request {message_id}")


def report(prefix: list[str], backend: list[str], sessions: list) -> None:
    table = processes()
    roots = [session.pid for session in sessions]
    caused = descendants({pid: row[0] for pid, row in table.items()}, roots)
    mode = " ".join(prefix[:1]) or "no mux"
    print(f"mode: {mode}; backend: {' '.join(backend)}")
    print(f"sessions: {len(sessions)}; processes: {len(caused)}")
    print(f"total RSS: {sum(table[pid][1] for pid in caused) / 1024:.1f} MB")
    if shutil.which("footprint"):
        total = sum(footprint(pid) for pid in caused)
        per_session = sum(footprint(pid) for pid in roots) / len(roots)
        print(f"total phys_footprint: {total / 1024:.1f} MB")
        print(f"per-session process phys_footprint: {per_session / 1024:.1f} MB")


def descendants(parents: Mapping[int, int], roots: Iterable[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        children.setdefault(parent, []).append(pid)
    found: set[int] = set()
    stack = [pid for pid in roots if pid in parents]
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack.extend(children.get(pid, []))
    return found


def processes() -> dict[int, tuple[int, int, str]]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,rss=,args="], capture_output=True, text=True
    ).stdout
    table = {}
    for line in output.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4:
            table[int(fields[0])] = (int(fields[1]), int(fields[2]), fields[3])
    return table


def footprint(pid: int) -> float:
    output = subprocess.run(
        ["footprint", "-p", str(pid)], capture_output=True, text=True
    ).stdout
    return parse_footprint(output)


def parse_footprint(text: str) -> float:
    """phys_footprint in KB from `footprint` output; 0 when absent."""
    match = re.search(r"phys_footprint:\s+([\d.]+)\s+(B|KB|MB|GB)\b", text)
    return float(match.group(1)) * UNITS[match.group(2)] if match else 0.0


if __name__ == "__main__":
    sys.exit(main())
