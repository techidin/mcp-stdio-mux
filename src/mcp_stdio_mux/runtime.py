"""Launch side: pick the backend a session belongs to, then hand the session over.

The shim an app starts lives only long enough to find or start the shared
daemon. It passes its stdin and stdout to the daemon over the Unix socket
(SCM_RIGHTS) and replaces itself with `/bin/cat` reading the daemon socket,
so each idle session costs one tiny process instead of a Python interpreter.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NamedTuple, NoReturn

from mcp_stdio_mux import __version__
from mcp_stdio_mux.config import Config, config_path, load_config

DEFAULT_IDLE_SECONDS = 300.0
SESSION_IDLE_SECONDS = 5.0
CLASSIFY_WAIT_SECONDS = 20.0
START_TIMEOUT_SECONDS = 10.0
HANDOFF_TIMEOUT_SECONDS = 5.0
RUN_SCOPES = ("auto", "global", "workspace", "session")
RELAY = "/bin/cat"
RELAY_PREFIX = "mcp-stdio-mux: "
# Tool-name words that imply per-client state (an open browser, a selected tab,
# a login). One match makes the backend per-session instead of shared.
STATEFUL_TOOL_WORDS = frozenset(
    {
        "browser",
        "tab",
        "tabs",
        "page",
        "navigate",
        "click",
        "screenshot",
        "session",
        "sessions",
        "login",
        "logout",
    }
)
# Flag-name words that mean the server is bound to the directory it starts in.
CWD_FLAG_WORDS = ("cwd", "project", "workspace", "root")


class RuntimePaths(NamedTuple):
    root: Path
    socket: Path
    lock: Path
    log: Path
    meta: Path


class Launch(NamedTuple):
    scope: str
    key: str
    workspace: Path
    learn: str | None  # signature whose shareability is not known yet


def run(argv: Sequence[str]) -> int:
    parent = os.getppid()
    arguments = parse_run_args(argv)
    if parent == 1:
        return 0  # the session died before we started; nobody would read us
    prepare_runtime_root()
    for _ in range(3):
        launch = resolve_launch(arguments)
        paths = runtime_paths(launch.key)
        if ensure_daemon(paths, launch, daemon_arguments(arguments, launch)):
            hand_over(paths, arguments.server, parent)  # returns only on refusal
    raise RuntimeError(f"could not attach to a backend; inspect {paths.log}")


def parse_run_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mcp-stdio-mux run",
        description="Serve this session from a shared MCP backend.",
    )
    parser.add_argument("--domain", required=True, help="sharing boundary")
    parser.add_argument("--server", required=True, help="label for logs and ps")
    parser.add_argument("--scope", choices=RUN_SCOPES, default="auto")
    parser.add_argument(
        "--env-keys",
        default="",
        help="comma-separated names of the declared env; their values are hashed",
    )
    parser.add_argument("--workspace", type=Path, help="backend directory")
    parser.add_argument("--idle-seconds", type=float, default=DEFAULT_IDLE_SECONDS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("a backend command is required after --")
    if arguments.idle_seconds <= 0:
        parser.error("--idle-seconds must be positive")
    return arguments


def resolve_launch(
    arguments: argparse.Namespace,
    environ: Mapping[str, str] = os.environ,
    home: Path | None = None,
) -> Launch:
    keys = [key for key in arguments.env_keys.split(",") if key]
    fingerprint = env_fingerprint(keys, environ)
    signature = runtime_key(arguments.domain, arguments.command, fingerprint)
    scope = arguments.scope
    if scope == "auto":
        override = _config(environ).scope(arguments.command, arguments.server)
        scope = override if override in RUN_SCOPES else "auto"
    learn = None
    if scope == "auto":
        learned = learned_scope(signature)
        learn = signature if learned is None else None
        if learned == "session":
            scope = "session"
        elif arguments.workspace or is_cwd_bound(arguments.command):
            scope = "workspace"
        else:
            scope = "global"
    if scope == "global":
        return Launch(scope, signature, home or Path.home(), learn)
    workspace = (
        arguments.workspace.resolve()
        if arguments.workspace
        else canonical_workspace(Path.cwd())
    )
    if scope == "workspace":
        key = runtime_key(arguments.domain, arguments.command, fingerprint, workspace)
        return Launch(scope, key, workspace, learn)
    return Launch(scope, f"{signature}-{uuid.uuid4().hex[:8]}", workspace, None)


def runtime_key(
    domain: str,
    command: Sequence[str],
    env_fingerprint: str = "",
    workspace: Path | None = None,
) -> str:
    """Stable, opaque launch signature; secrets never appear in it."""
    identity = json.dumps(
        [
            __version__,
            domain,
            list(command),
            env_fingerprint,
            str(workspace.resolve()) if workspace else None,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def env_fingerprint(keys: Sequence[str], environ: Mapping[str, str]) -> str:
    """Hash the declared environment so differing credentials never share."""
    values = [[key, environ.get(key)] for key in sorted(set(keys))]
    encoded = json.dumps(values, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def is_cwd_bound(command: Sequence[str]) -> bool:
    for argument in command[1:]:
        if argument == ".":
            return True
        if argument.startswith("-"):
            flag = argument.split("=", 1)[0].lower()
            if any(word in flag for word in CWD_FLAG_WORDS):
                return True
    return False


def stateful_tool(result: object) -> str | None:
    """Return the first tool name that implies per-session state, if any."""
    tools = result.get("tools") if isinstance(result, dict) else None
    for tool in tools if isinstance(tools, list) else []:
        name = tool.get("name") if isinstance(tool, dict) else None
        if not isinstance(name, str):
            continue
        words = re.split(
            r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()
        )
        if STATEFUL_TOOL_WORDS.intersection(words):
            return name
    return None


def canonical_workspace(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def runtime_root(environ: Mapping[str, str] = os.environ) -> Path:
    configured = environ.get("MCP_STDIO_MUX_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    if environ.get("XDG_RUNTIME_DIR"):
        return Path(environ["XDG_RUNTIME_DIR"]) / "mcp-stdio-mux"
    # Short on purpose: macOS caps a socket path at 104 bytes and its per-user
    # temporary directory alone is about 50.
    return Path("/tmp") / f"mcp-stdio-mux-{os.getuid()}"


def runtime_paths(key: str) -> RuntimePaths:
    root = runtime_root()
    return RuntimePaths(
        root,
        root / f"{key}.sock",
        root / f"{key}.lock",
        root / f"{key}.log",
        root / f"{key}.json",
    )


def learned_scope(signature: str) -> str | None:
    try:
        value = (runtime_root() / f"{signature}.scope").read_text().strip()
    except OSError:
        return None
    return value if value in ("shared", "session") else None


def record_scope(signature: str, scope: str) -> None:
    write_private(runtime_root() / f"{signature}.scope", scope + "\n")


def write_private(target: Path, text: str) -> None:
    """Replace a runtime file atomically with owner-only permissions."""
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    temporary.replace(target)


def prepare_runtime_root() -> Path:
    """Create the private socket directory; refuse one another user owns."""
    root = runtime_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid():
        raise RuntimeError(f"{root} is not owned by this user")
    os.chmod(root, 0o700)
    return root


def daemon_arguments(arguments: argparse.Namespace, launch: Launch) -> list:
    idle = SESSION_IDLE_SECONDS if launch.scope == "session" else arguments.idle_seconds
    return [
        "--key",
        launch.key,
        "--domain",
        arguments.domain,
        "--server",
        arguments.server,
        "--scope",
        launch.scope,
        "--workspace",
        str(launch.workspace),
        "--idle-seconds",
        str(idle),
        *(["--learn", launch.learn] if launch.learn else []),
        "--",
        *arguments.command,
    ]


def ensure_daemon(
    paths: RuntimePaths, launch: Launch, arguments: Sequence[str]
) -> bool:
    """Start or reuse the daemon; False means the signature turned per-session."""
    lock = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _socket_is_live(paths.socket):
            return launch.learn is None or _await_learned_scope(launch.learn)
        if paths.socket.exists():
            paths.socket.unlink()
        log = os.open(paths.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            # -P keeps the backend's directory off sys.path: a workspace must not
            # be able to shadow this package.
            subprocess.Popen(
                [sys.executable, "-P", "-m", "mcp_stdio_mux", "daemon", *arguments],
                cwd=str(launch.workspace),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(log)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _socket_is_live(paths.socket):
                return True
            time.sleep(0.05)
        raise RuntimeError(f"daemon did not start; inspect {paths.log}")
    finally:
        os.close(lock)


def hand_over(paths: RuntimePaths, server: str, parent: int) -> None:
    """Give this session's stdio to the daemon, then become a tiny relay.

    Returns only when the daemon refused the session, so the caller can retry.
    """
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(HANDOFF_TIMEOUT_SECONDS)
    try:
        connection.connect(str(paths.socket))
        hello = json.dumps({"agent_pid": parent}).encode()
        socket.send_fds(connection, [hello], [0, 1])
        answer = connection.recv(16)
    except (TimeoutError, FileNotFoundError, ConnectionError):
        connection.close()
        return
    if answer != b"ok\n":
        connection.close()
        return
    _become_relay(connection, server)


def _become_relay(connection: socket.socket, server: str) -> NoReturn:
    # The daemon owns our stdin/stdout now. Keep a process the app can watch and
    # kill: `cat` blocks on the daemon socket and exits when the daemon drops
    # this session; the daemon drops it when `cat` dies.
    connection.setblocking(True)  # a timeout would leave O_NONBLOCK on the fd
    os.dup2(connection.fileno(), 0)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.execv(RELAY, [RELAY_PREFIX + server])


def _await_learned_scope(signature: str) -> bool:
    """Hold a second client until the first one's tools/list classifies the server.

    Holding the lock here queues every later client behind the same answer, so
    no session can attach to a stateful backend before it is known to be one.
    """
    deadline = time.monotonic() + CLASSIFY_WAIT_SECONDS
    while time.monotonic() < deadline:
        scope = learned_scope(signature)
        if scope is not None:
            return scope == "shared"
        time.sleep(0.05)
    return True


def _socket_is_live(socket_path: Path) -> bool:
    if not socket_path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.1)
    try:
        probe.connect(str(socket_path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _config(environ: Mapping[str, str]) -> Config:
    # A broken config file must not take every MCP server down with it.
    try:
        return load_config(environ)
    except ValueError as error:
        print(f"mcp-stdio-mux: ignoring config: {error}", file=sys.stderr)
        return Config(config_path(environ))
