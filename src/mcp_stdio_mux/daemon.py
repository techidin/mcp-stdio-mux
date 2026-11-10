"""The shared backend: one MCP server process, many sessions handed over by shims.

One session must never hurt another: every backend line is handled on its own,
each session has its own outbound queue, and a reply goes only to the session
that asked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import socket
import stat
import time
import uuid
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mcp_stdio_mux import __version__
from mcp_stdio_mux.router import (
    MAX_MESSAGE_BYTES,
    Delivery,
    MessageRouter,
    encode,
    error_response,
    parse_message,
)
from mcp_stdio_mux.runtime import (
    KEY_ENV,
    LEARN_ENV,
    SESSION_IDLE_SECONDS,
    record_scope,
    runtime_paths,
    stateful_tool,
    write_private,
)

AGENT_POLL_SECONDS = 1.0
HELLO_TIMEOUT_SECONDS = 5.0
LOG_LIMIT_BYTES = 1024 * 1024
OUTBOX_LIMIT_BYTES = 64 * 1024 * 1024
PROBE_ID = "mux:tools/list"


@dataclass
class Session:
    connection: socket.socket
    read_transport: asyncio.BaseTransport
    writer: asyncio.StreamWriter
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    queued_bytes: int = 0
    sender: asyncio.Task | None = None

    def close(self) -> None:
        if self.sender:
            self.sender.cancel()
        self.read_transport.close()
        self.writer.close()
        self.connection.close()  # the shim's `cat` sees EOF and exits


def run_daemon(argv: Sequence[str]) -> int:
    arguments = parse_daemon_args(argv)
    asyncio.run(SharedDaemon(arguments).run())
    return 0


def parse_daemon_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcp-stdio-mux daemon")
    for name in ("--domain", "--server", "--scope"):
        parser.add_argument(name, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args(argv)
    if arguments.command and arguments.command[0] == "--":
        arguments.command = arguments.command[1:]
    if not arguments.command:
        parser.error("a backend command is required after --")
    # Identity comes from the environment and is not passed on to the backend.
    arguments.key = os.environ.pop(KEY_ENV, "")
    arguments.learn = os.environ.pop(LEARN_ENV, None)
    if not arguments.key:
        parser.error(f"{KEY_ENV} is required")
    return arguments


class SharedDaemon:
    def __init__(self, arguments: argparse.Namespace) -> None:
        self.arguments = arguments
        self.launch_scope = arguments.scope
        self.paths = runtime_paths(arguments.key)
        self.command = tuple(arguments.command)
        self.idle_seconds = arguments.idle_seconds
        self.learn: str | None = arguments.learn
        self.probed = False
        self.tools_request_ids: set[str] = set()
        self.router = MessageRouter()
        self.sessions: dict[str, Session] = {}
        self.tasks: set[asyncio.Task] = set()
        self.accepting = True
        self.socket_released = False
        self.backend: asyncio.subprocess.Process | None = None
        self.backend_write_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.last_empty_at = time.monotonic()
        self.started_at = time.time()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            loop.add_signal_handler(signal_number, self.stop_event.set)
        # Bind before the backend exists, so a bad socket path cannot orphan it;
        # listen only after it started, so shims never attach to a failed one.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.paths.socket.exists():
            self.paths.socket.unlink()
        listener.bind(str(self.paths.socket))
        os.chmod(self.paths.socket, 0o600)
        workers: list[asyncio.Task] = []
        try:
            self.backend = await self.start_backend()
            listener.listen(64)
            listener.setblocking(False)
            workers = [
                asyncio.create_task(coroutine)
                for coroutine in (
                    self.accept(listener),
                    self.read_backend(),
                    self.read_backend_stderr(),
                    self.watch_idle(),
                    self.watch_backend(),
                )
            ]
            self.publish()
            await self.stop_event.wait()
        finally:
            listener.close()
            self.release_socket()
            for session_id in list(self.sessions):
                self.drop(session_id)
            await self.stop_backend()
            for task in [*workers, *self.tasks]:
                task.cancel()
            await asyncio.gather(*workers, *self.tasks, return_exceptions=True)
            _unlink(self.paths.meta)
            if self.launch_scope == "session":  # a one-off key nobody reuses
                _unlink(self.paths.lock)
                _unlink(self.paths.log)

    async def start_backend(self) -> asyncio.subprocess.Process:
        # Own process group, so stopping reaps launchers' children too
        # (`npm exec` -> `node`), not only the direct child. The stream limit
        # covers servers that answer tools/list with megabytes on one line.
        try:
            return await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.arguments.workspace),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_MESSAGE_BYTES + 1,
            )
        except OSError as error:
            self.log(f"cannot start backend {self.command[0]}: {error}\n")
            raise

    async def accept(self, listener: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        while True:
            connection, _ = await loop.sock_accept(listener)
            self.spawn(self.serve(connection))

    async def serve(self, connection: socket.socket) -> None:
        connection.setblocking(False)
        descriptors: list[int] = []
        try:
            hello, descriptors = await asyncio.wait_for(
                _receive_hello(connection), HELLO_TIMEOUT_SECONDS
            )
            agent_pid = int(json.loads(hello)["agent_pid"])
        except (TimeoutError, OSError, ValueError, KeyError, TypeError):
            # A liveness probe connects and closes without a word; that is fine.
            _close_all(descriptors)
            connection.close()
            self.touch()
            return
        if not (self.accepting and len(descriptors) == 2 and _streams(descriptors)):
            _close_all(descriptors)
            await _send_quietly(connection, b"no\n")
            connection.close()
            return
        session_id = uuid.uuid4().hex
        reader, read_transport, writer = await _open_stdio(descriptors)
        session = Session(connection, read_transport, writer)
        session.sender = self.spawn(self.send_loop(session_id, session))
        self.sessions[session_id] = session
        self.router.add_client(session_id)
        self.publish()
        loop = asyncio.get_running_loop()
        ends = [
            asyncio.create_task(self.read_session(session_id, reader)),
            asyncio.create_task(loop.sock_recv(connection, 1)),
        ]
        if agent_pid > 1:  # an app running as PID 1 (a container) never "exits"
            ends.append(asyncio.create_task(_agent_exit(agent_pid)))
        try:
            await _send_quietly(connection, b"ok\n")
            await asyncio.wait(ends, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in ends:
                task.cancel()
            self.drop(session_id)

    async def read_session(self, session_id: str, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                payload = await reader.readline()
            except ValueError:
                self.log(f"session dropped: message over {MAX_MESSAGE_BYTES} bytes\n")
                return
            if not payload:
                return
            try:
                message = parse_message(payload)
                deliveries = self.router.route_client(session_id, message)
            except (ValueError, TypeError, KeyError) as error:
                self.queue(session_id, error_response(None, str(error), -32700))
                continue
            await self.dispatch(deliveries)

    async def send_loop(self, session_id: str, session: Session) -> None:
        try:
            while True:
                payload = await session.outbox.get()
                session.writer.write(payload)
                await session.writer.drain()
                session.queued_bytes -= len(payload)
        except ConnectionError:
            self.drop(session_id)  # the app closed its end; others carry on

    def queue(self, session_id: str, message: Mapping[str, object]) -> None:
        """Hand a message to one session without waiting for it to read."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        payload = encode(message)
        session.queued_bytes += len(payload)
        if session.queued_bytes > OUTBOX_LIMIT_BYTES:
            self.log("session dropped: it stopped reading its replies\n")
            self.drop(session_id)
            return
        session.outbox.put_nowait(payload)

    def drop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        answers = self.router.remove_client(session_id)
        session.close()
        if answers:
            self.spawn(self.dispatch(answers))
        if not self.sessions:
            self.touch()
        self.publish()

    def touch(self) -> None:
        if not self.sessions:
            self.last_empty_at = time.monotonic()

    async def read_backend(self) -> None:
        assert self.backend and self.backend.stdout
        while True:
            try:
                payload = await self.backend.stdout.readline()
            except ValueError:
                # Over the limit: asyncio drops the buffered part; the rest of
                # the line arrives next and fails to parse. Nobody else waits.
                self.log(f"backend message over {MAX_MESSAGE_BYTES} bytes dropped\n")
                continue
            if not payload:
                return
            try:
                await self.handle_backend(parse_message(payload))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                self.log(f"backend message ignored: {error}\n")

    async def handle_backend(self, message: Mapping[str, object]) -> None:
        message_id = message.get("id")
        if message_id == PROBE_ID:  # our own classification request
            self.learn_scope(message.get("result", {"tools": []}))
            return
        if isinstance(message_id, str) and message_id in self.tools_request_ids:
            if "result" in message:
                self.learn_scope(message["result"])
        await self.dispatch(self.router.route_backend(message))
        result = self.router.initialize_result
        if self.learn and isinstance(result, dict):
            capabilities = result.get("capabilities")
            if not (isinstance(capabilities, dict) and "tools" in capabilities):
                self.learn_scope({"tools": []})  # no tools, nothing stateful to see

    def learn_scope(self, result: object) -> None:
        """Record whether this launch signature may be shared, from its tools."""
        if not self.learn:
            return
        self.tools_request_ids.clear()
        tool = stateful_tool(result)
        record_scope(self.learn, "session" if tool else "shared")
        self.learn = None
        if tool:
            self.log(f"per-session scope: tool {tool} implies client state\n")
            self.accepting = False
            self.release_socket()
            self.idle_seconds = SESSION_IDLE_SECONDS
            self.arguments.scope = "session"
            self.publish()

    async def read_backend_stderr(self) -> None:
        assert self.backend and self.backend.stderr
        while True:
            try:
                payload = await self.backend.stderr.readline()
            except ValueError:
                continue  # one oversized stderr line; keep reading
            if not payload:
                return
            self.log(payload.decode(errors="replace"))

    async def watch_backend(self) -> None:
        assert self.backend
        return_code = await self.backend.wait()
        if not self.stop_event.is_set():
            self.log(f"backend exited with status {return_code}\n")
            await self.dispatch(
                self.router.fail_backend(
                    f"Shared MCP backend exited with status {return_code}"
                )
            )
            self.stop_event.set()

    async def watch_idle(self) -> None:
        while True:
            await asyncio.sleep(min(max(self.idle_seconds / 2, 0.05), 1.0))
            idle_for = time.monotonic() - self.last_empty_at
            if not self.sessions and idle_for >= self.idle_seconds:
                self.stop_event.set()
                return

    async def dispatch(self, deliveries: Sequence[Delivery]) -> None:
        for destination, message in deliveries:
            if destination != "backend":
                self.queue(destination, message)
                continue
            method = message.get("method")
            if self.learn and method == "tools/list":
                self.tools_request_ids.add(str(message.get("id")))
            await self.write_backend(message)
            if self.learn and method == "notifications/initialized" and not self.probed:
                # Classify now instead of waiting for a session to list tools.
                self.probed = True
                probe = {"jsonrpc": "2.0", "id": PROBE_ID, "method": "tools/list"}
                await self.write_backend({**probe, "params": {}})

    async def write_backend(self, message: Mapping[str, object]) -> None:
        if not self.backend or not self.backend.stdin:
            return
        async with self.backend_write_lock:
            try:
                self.backend.stdin.write(encode(message))
                await self.backend.stdin.drain()
            except ConnectionError:
                pass  # watch_backend reports the exit to every waiting session

    async def stop_backend(self) -> None:
        if not self.backend:
            return
        if self.backend.stdin:
            self.backend.stdin.close()
        if self.backend.returncode is None:
            _signal_group(self.backend.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.backend.wait(), timeout=2)
            except TimeoutError:
                pass
        # The leader may be gone while its children (the real server) live on.
        _signal_group(self.backend.pid, signal.SIGKILL)
        if self.backend.returncode is None:
            await self.backend.wait()

    def release_socket(self) -> None:
        """Stop accepting new sessions; the path may later belong to a new daemon."""
        if not self.socket_released:
            self.socket_released = True
            _unlink(self.paths.socket)

    def spawn(self, coroutine: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def publish(self) -> None:
        """Describe this daemon for `mcp-stdio-mux status`."""
        write_private(
            self.paths.meta,
            json.dumps(
                {
                    "version": __version__,
                    "key": self.arguments.key,
                    "server": self.arguments.server,
                    "domain": self.arguments.domain,
                    "scope": self.arguments.scope,
                    "workspace": str(self.arguments.workspace),
                    "pid": os.getpid(),
                    "backend_pid": self.backend.pid if self.backend else None,
                    "clients": len(self.sessions),
                    "started": self.started_at,
                }
            )
            + "\n",
        )

    def log(self, text: str) -> None:
        try:
            if self.paths.log.stat().st_size > LOG_LIMIT_BYTES:
                os.truncate(self.paths.log, 0)
        except FileNotFoundError:
            pass
        descriptor = os.open(
            self.paths.log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
        )
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(text)


async def _receive_hello(connection: socket.socket) -> tuple[bytes, list[int]]:
    loop = asyncio.get_running_loop()
    readable = loop.create_future()
    loop.add_reader(
        connection.fileno(), lambda: readable.done() or readable.set_result(None)
    )
    try:
        await readable
    finally:
        loop.remove_reader(connection.fileno())
    message, descriptors, _, _ = socket.recv_fds(connection, 4096, 2)
    return message, list(descriptors)


def _streams(descriptors: Sequence[int]) -> bool:
    """Only pipes, sockets and terminals can carry a session."""
    for descriptor in descriptors:
        mode = os.fstat(descriptor).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode)):
            return False
    return True


async def _open_stdio(
    descriptors: Sequence[int],
) -> tuple[asyncio.StreamReader, asyncio.BaseTransport, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
    read_transport, _ = await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader),
        os.fdopen(descriptors[0], "rb", buffering=0),
    )
    write_transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin,
        os.fdopen(descriptors[1], "wb", buffering=0),
    )
    writer = asyncio.StreamWriter(write_transport, protocol, None, loop)
    return reader, read_transport, writer


async def _agent_exit(pid: int) -> None:
    # ponytail: kill(0) treats an unreaped zombie app as alive; a parent that
    # never reaps it keeps the session until the shim's `cat` is killed.
    while True:
        await asyncio.sleep(AGENT_POLL_SECONDS)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            continue


async def _send_quietly(connection: socket.socket, payload: bytes) -> None:
    try:
        await asyncio.get_running_loop().sock_sendall(connection, payload)
    except OSError:
        pass


def _close_all(descriptors: Sequence[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _signal_group(process_group: int, signal_number: int) -> None:
    try:
        os.killpg(process_group, signal_number)
    except (ProcessLookupError, PermissionError):
        pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
