import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_stdio_mux import __version__
from mcp_stdio_mux.config import load_config
from mcp_stdio_mux.runtime import (
    env_fingerprint,
    is_cwd_bound,
    parse_run_args,
    resolve_launch,
    runtime_key,
    runtime_paths,
    stateful_tool,
)
from tests.helpers import (
    FAKE_SESSION,
    FIXTURE_BACKEND,
    LARGE_TOOLS,
    STATEFUL_TOOLS,
    TOOLS,
    initialize_params,
    isolated_environment,
    pid_alive,
    read_message,
    request,
    response,
    send_message,
    shim_command,
    start_shim,
    stop_process,
    wait_until,
)


class RuntimeKeyTests(unittest.TestCase):
    def test_signature_changes_with_domain_command_env_or_workspace(self):
        command = ("tilth", "--mcp")
        base = runtime_key("default", command, "env-a")

        variants = {
            runtime_key("work", command, "env-a"),
            runtime_key("default", ("tilth", "--mcp", "--edit"), "env-a"),
            runtime_key("default", command, "env-b"),
            runtime_key("default", command, "env-a", Path("/workspace/a")),
        }

        self.assertNotIn(base, variants)
        self.assertEqual(base, runtime_key("default", command, "env-a"))
        self.assertNotEqual(
            runtime_key("default", command, "env-a", Path("/workspace/a")),
            runtime_key("default", command, "env-a", Path("/workspace/b")),
        )

    def test_signature_changes_with_package_version(self):
        # A new release must never attach to a daemon speaking an older protocol.
        with patch("mcp_stdio_mux.runtime.__version__", "0.0.0-other"):
            other = runtime_key("default", ("tilth",), "")
        self.assertNotEqual(other, runtime_key("default", ("tilth",), ""))
        self.assertTrue(__version__)

    def test_env_fingerprint_hashes_only_declared_values(self):
        first = env_fingerprint(["TOKEN"], {"TOKEN": "secret-a", "TERM": "x"})

        self.assertEqual(first, env_fingerprint(["TOKEN"], {"TOKEN": "secret-a"}))
        self.assertNotEqual(first, env_fingerprint(["TOKEN"], {"TOKEN": "secret-b"}))
        self.assertNotIn("secret-a", first)

    def test_cwd_bound_detection_reads_flags_not_paths(self):
        self.assertTrue(
            is_cwd_bound(["serena", "start-mcp-server", "--project-from-cwd"])
        )
        self.assertTrue(is_cwd_bound(["server", "--root=/repo"]))
        self.assertTrue(is_cwd_bound(["server", "."]))
        self.assertFalse(
            is_cwd_bound(["npx", "-y", "@modelcontextprotocol/server-github"])
        )
        self.assertFalse(is_cwd_bound(["python", "/tmp/project-root/backend.py"]))

    def test_stateful_tools_are_detected_from_tool_names(self):
        browser = {"tools": [{"name": "browser_navigate"}, {"name": "browser_click"}]}
        camel = {"tools": [{"name": "openSession"}]}
        stateless = {"tools": [{"name": "create_issue"}, {"name": "search_code"}]}

        self.assertEqual(stateful_tool(browser), "browser_navigate")
        self.assertEqual(stateful_tool(camel), "openSession")
        self.assertIsNone(stateful_tool(stateless))
        self.assertIsNone(stateful_tool(None))

    def test_default_socket_path_fits_the_unix_socket_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            paths = runtime_paths("a" * 24)

        self.assertEqual(paths.root, Path("/tmp") / f"mcp-stdio-mux-{os.getuid()}")
        self.assertLess(len(os.fsencode(paths.socket)), 104)

    def test_xdg_runtime_dir_is_preferred_when_set(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}, clear=True):
            paths = runtime_paths("a" * 24)

        self.assertEqual(paths.root, Path("/run/user/1000/mcp-stdio-mux"))


class ScopeOverrideTests(unittest.TestCase):
    def test_builtin_override_applies_without_a_config_file(self):
        with tempfile.TemporaryDirectory() as config_home:
            arguments = parse_run_args(
                ["--domain", "default", "--server", "x", "--", "/bin/tilth", "--mcp"]
            )
            launch = resolve_launch(
                arguments, {"XDG_CONFIG_HOME": config_home}, Path(config_home)
            )

        self.assertEqual(launch.scope, "workspace")
        self.assertIsNone(launch.learn)

    def test_user_config_overrides_by_server_name(self):
        with tempfile.TemporaryDirectory() as config_home:
            config = Path(config_home) / "mcp-stdio-mux" / "config.toml"
            config.parent.mkdir()
            config.write_text('[servers]\nbrowser-tools = "global"\n')
            arguments = parse_run_args(
                [
                    "--domain",
                    "default",
                    "--server",
                    "browser-tools",
                    "--",
                    "npx",
                    "-y",
                    "rea",
                ]
            )
            launch = resolve_launch(
                arguments, {"XDG_CONFIG_HOME": config_home}, Path(config_home)
            )

        self.assertEqual(launch.scope, "global")
        self.assertEqual(
            launch.key,
            runtime_key("default", arguments.command, env_fingerprint([], {})),
        )
        self.assertIsNone(launch.learn)

    def test_invalid_config_scope_is_rejected_with_its_path(self):
        with tempfile.TemporaryDirectory() as config_home:
            config = Path(config_home) / "mcp-stdio-mux" / "config.toml"
            config.parent.mkdir()
            config.write_text('[servers]\ntilth = "sometimes"\n')

            with self.assertRaisesRegex(ValueError, "config.toml"):
                load_config({"XDG_CONFIG_HOME": config_home})


class SharedRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.runtime_root = self.root / "runtime"
        self.counter = self.root / "starts.txt"
        self.backend = self.root / "fixture_backend.py"
        self.backend.write_text(FIXTURE_BACKEND)
        self.environment = isolated_environment(self.root)

    def command(self, *mode):
        return (sys.executable, str(self.backend), str(self.counter), *mode)

    def shim(self, workspace=None, *mode):
        process = start_shim(
            workspace or self.root, self.environment, self.command(*mode)
        )
        self.addCleanup(stop_process, process)
        return process

    def initialize(self, process):
        send_message(process, request(1, "initialize", initialize_params({})))
        self.assertEqual(read_message(process)["id"], 1)

    def test_two_shims_share_one_backend_and_idle_shutdown_removes_socket(self):
        first = self.shim()
        self.initialize(first)

        # The second client starts before the first lists tools: it must wait
        # for the classification instead of attaching blindly.
        other = self.root / "other-workspace"
        other.mkdir()
        second = self.shim(other)
        send_message(second, request(1, "initialize", initialize_params({})))
        send_message(first, request(2, "tools/list"))
        self.assertEqual(read_message(first), response(2, {"tools": TOOLS}))
        self.assertEqual(read_message(second)["id"], 1)
        send_message(second, request(2, "tools/list"))
        self.assertEqual(read_message(second), response(2, {"tools": TOOLS}))
        self.assertEqual(self.counter.read_text().splitlines(), ["start"])

        stop_process(first)
        stop_process(second)
        wait_until(
            lambda: not list(self.runtime_root.glob("*.sock")), timeout_seconds=3
        )

    def test_stateful_server_gets_one_backend_per_session(self):
        first = self.shim(None, "stateful")
        self.initialize(first)
        second = self.shim(None, "stateful")
        send_message(first, request(2, "tools/list"))
        self.assertEqual(read_message(first)["result"]["tools"], STATEFUL_TOOLS)
        self.initialize(second)

        self.assertEqual(self.counter.read_text().splitlines(), ["start", "start"])
        self.assertEqual(
            [path.read_text() for path in self.runtime_root.glob("*.scope")],
            ["session\n"],
        )
        third = self.shim(None, "stateful")
        self.initialize(third)
        self.assertEqual(len(self.counter.read_text().splitlines()), 3)

    def test_backend_reply_longer_than_asyncio_default_line_limit_is_delivered(self):
        # Some servers answer tools/list with megabytes on one line; asyncio's
        # default 64 KiB stream limit used to kill the backend reader silently.
        shim = self.shim(None, "large")
        self.initialize(shim)
        send_message(shim, request(2, "tools/list"))
        self.assertEqual(read_message(shim), response(2, {"tools": LARGE_TOOLS}))

    def test_backend_process_group_is_reaped_when_last_client_leaves(self):
        # Like `npm exec`: the launcher spawns a child that ignores stdin EOF.
        shim = self.shim(None, "spawn-child")
        self.initialize(shim)
        child_pid = int(self.counter.with_suffix(".child").read_text())
        self.assertTrue(pid_alive(child_pid))

        stop_process(shim)
        wait_until(lambda: not pid_alive(child_pid), timeout_seconds=5)

    def test_shim_exits_when_its_parent_session_dies(self):
        pid_file = self.root / "shim.pid"
        # The fake session keeps the shim's stdin open after it dies, the way a
        # leaked descriptor would, so only the parent check can fire.
        session = subprocess.Popen(
            [
                sys.executable,
                "-c",
                FAKE_SESSION,
                str(pid_file),
                *shim_command(self.command()),
            ],
            cwd=self.root,
            env=self.environment,
        )
        self.addCleanup(session.kill)
        wait_until(lambda: list(self.runtime_root.glob("*.sock")), timeout_seconds=5)
        wait_until(pid_file.exists, timeout_seconds=5)
        shim_pid = int(pid_file.read_text())
        self.assertTrue(pid_alive(shim_pid))

        session.kill()
        session.wait()
        wait_until(lambda: not pid_alive(shim_pid), timeout_seconds=5)

    def test_connected_shim_becomes_a_tiny_relay_named_after_its_server(self):
        shim = self.shim()
        self.initialize(shim)

        rss_kb, args = subprocess.run(
            ["ps", "-o", "rss=,args=", "-p", str(shim.pid)],
            capture_output=True,
            text=True,
        ).stdout.split(maxsplit=1)

        # Only the exec'd relay carries this argv[0], and no Python interpreter
        # fits in 6 MB: the shim is gone, its pid lives on as `cat`.
        self.assertEqual(args.strip(), "mcp-stdio-mux: fixture")
        self.assertLess(int(rss_kb), 6 * 1024)

    def test_killed_shim_releases_its_backend(self):
        shim = self.shim()
        self.initialize(shim)
        self.assertEqual(len(list(self.runtime_root.glob("*.sock"))), 1)

        shim.kill()
        shim.wait()
        wait_until(
            lambda: not list(self.runtime_root.glob("*.sock")), timeout_seconds=5
        )

    def test_explicit_workspace_is_the_backend_directory(self):
        workspace = self.root / "configured"
        workspace.mkdir()
        process = subprocess.Popen(
            [
                *shim_command(self.command())[:-4],
                "--scope",
                "workspace",
                "--workspace",
                str(workspace),
                "--",
                *self.command(),
            ],
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(stop_process, process)
        self.initialize(process)

        send_message(process, request(2, "cwd"))

        self.assertEqual(
            read_message(process)["result"]["cwd"], str(workspace.resolve())
        )

    def test_running_backend_publishes_status_metadata(self):
        shim = self.shim()
        self.initialize(shim)

        wait_until(lambda: list(self.runtime_root.glob("*.json")), timeout_seconds=3)
        metadata = json.loads(next(self.runtime_root.glob("*.json")).read_text())

        self.assertEqual(metadata["server"], "fixture")
        self.assertEqual(metadata["domain"], "default")
        self.assertEqual(metadata["clients"], 1)
        self.assertTrue(pid_alive(metadata["backend_pid"]))


if __name__ == "__main__":
    unittest.main()
