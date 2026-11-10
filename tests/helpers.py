"""Shared fixtures: JSON-RPC builders, a fake MCP backend, and process helpers."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def request(message_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def response(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def notification(method, params=None):
    message = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialize_params(capabilities):
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": capabilities,
        "clientInfo": {"name": "test", "version": "1"},
    }


def initialize_result():
    return {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "fixture", "version": "1"},
    }


def isolated_environment(root):
    """Environment that keeps the runtime and config away from the real user."""
    return {
        **os.environ,
        "PYTHONPATH": SRC,
        "MCP_STDIO_MUX_RUNTIME_DIR": str(Path(root) / "runtime"),
        "XDG_CONFIG_HOME": str(Path(root) / "config"),
    }


def shim_command(backend_command, idle_seconds="0.2"):
    return [
        sys.executable,
        "-m",
        "mcp_stdio_mux",
        "run",
        "--domain",
        "default",
        "--server",
        "fixture",
        "--idle-seconds",
        idle_seconds,
        "--",
        *backend_command,
    ]


def start_shim(workspace, environment, backend_command):
    return subprocess.Popen(
        shim_command(backend_command),
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def send_message(process, message):
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def read_message(process, timeout_seconds=5):
    # A reader thread, not select(): select() on the fd cannot see lines that
    # readline() already pulled into Python's buffer, so two replies arriving
    # together would hide the second one.
    try:
        line = _lines(process).get(timeout=timeout_seconds)
    except queue.Empty:
        stderr = process.stderr.read() if process.poll() is not None else ""
        raise AssertionError(f"timed out waiting for MCP response: {stderr}") from None
    if line is None:
        raise AssertionError(
            f"MCP shim closed before responding: {process.stderr.read()}"
        )
    return json.loads(line)


def _lines(process):
    if not hasattr(process, "lines"):
        process.lines = queue.Queue()

        def pump():
            try:
                for line in process.stdout:
                    process.lines.put(line)
            except (OSError, ValueError):
                pass  # the test closed the stream
            process.lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
    return process.lines


def stop_process(process):
    if process.poll() is None:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=2)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream and not stream.closed:
            stream.close()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A reaped-but-unwaited zombie still answers kill(0); it is not running.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def wait_until(predicate, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition was not met within {timeout_seconds} seconds")


TOOLS = [{"name": "create_issue"}]
STATEFUL_TOOLS = [{"name": "browser_navigate"}]
LARGE_TOOLS = [{"name": "create_issue", "description": "x" * 200_000}]

FAKE_SESSION = """\
import os, subprocess, sys
read_end, write_end = os.pipe()
shim = subprocess.Popen(sys.argv[2:], stdin=read_end, stdout=subprocess.DEVNULL)
open(sys.argv[1], "w").write(str(shim.pid))
# Leak the write end into a sibling that outlives this session.
subprocess.Popen(["sleep", "30"], pass_fds=[write_end], start_new_session=True)
shim.wait()
"""

FIXTURE_BACKEND = """\
import json
import os
import subprocess
import sys
from pathlib import Path

counter_path = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else ""
with counter_path.open("a") as counter:
    counter.write("start\\n")
if mode == "spawn-child":
    child = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
    counter_path.with_suffix(".child").write_text(str(child.pid))
tools = [{"name": "create_issue"}]
if mode == "stateful":
    tools = [{"name": "browser_navigate"}]
if mode == "large":
    tools[0]["description"] = "x" * 200_000

def emit(message):
    print(json.dumps(message), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {} if mode == "no-tools" else {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "tools/list":
        with counter_path.with_suffix(".lists").open("a") as lists:
            lists.write("list\\n")
        result = {"tools": tools}
    elif method == "cwd":
        result = {"cwd": os.getcwd()}
    elif method == "huge":
        # One line over the 16 MiB limit, then nothing for this request.
        emit({"jsonrpc": "2.0", "id": message["id"], "result": "x" * (17 << 20)})
        continue
    elif method == "bad-id":
        emit({"jsonrpc": "2.0", "id": [1], "result": {}})
        emit({"jsonrpc": "2.0", "result": {}})
        emit({"jsonrpc": "2.0", "id": {"a": 1}, "method": "ping"})
        result = {"ok": True}
    elif method == "flood":
        log = {"jsonrpc": "2.0", "method": "notifications/message"}
        for _ in range(12):
            emit({**log, "params": {"level": "info", "data": "y" * (1 << 20)}})
        result = {"ok": True}
    else:
        continue
    emit({"jsonrpc": "2.0", "id": message["id"], "result": result})
"""


def read_reply(process, message_id, timeout_seconds=10):
    """The response to `message_id`, skipping notifications broadcast meanwhile."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        message = read_message(process, max(deadline - time.monotonic(), 0.1))
        if message.get("id") == message_id and "method" not in message:
            return message
    raise AssertionError(f"no reply to request {message_id}")
