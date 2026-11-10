import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from mcp_stdio_mux.install import (
    Context,
    apply,
    find_executable,
    install_skills,
    plan,
    undo,
)
from tests.helpers import SRC

EXE = "/opt/bin/mcp-stdio-mux"
CLAUDE = {
    "numStartups": 3,
    "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "gh"], "env": {"T": "x"}},
        "remote": {"type": "http", "url": "https://example.com/mcp"},
    },
}
CODEX = '# keep me\n[mcp_servers.tilth]\ncommand = "tilth"\nargs = ["--mcp"]\n'


class InstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # resolve(): macOS temp dirs live behind the /var -> /private/var link
        self.home = Path(directory.name).resolve()
        self.environ = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.context = Context(self.home, self.environ, EXE, "darwin")
        self.claude = self.write(".claude.json", json.dumps(CLAUDE, indent=2) + "\n")
        self.work = self.write(".claude-work/.claude.json", json.dumps(CLAUDE))
        self.codex = self.write(".codex/config.toml", CODEX)
        os.chmod(self.claude, 0o600)
        self.originals = {
            path: path.read_bytes() for path in (self.claude, self.work, self.codex)
        }

    def write(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def history(self):
        path = self.home / ".local/state/mcp-stdio-mux/history.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_plan_reports_changes_without_writing(self):
        planned = plan(self.context)

        self.assertEqual(
            [(p.config.app, p.config.profile, p.domain, p.changes) for p in planned],
            [
                ("Claude Code", "default", "default", 1),
                ("Claude Code", "work", "work", 1),
                ("Codex", "default", "default", 1),
            ],
        )
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_apply_wraps_backs_up_and_keeps_permissions(self):
        applied = apply(self.context)

        self.assertEqual([p.written for p in applied], [True, True, True])
        server = json.loads(self.claude.read_text())["mcpServers"]["github"]
        self.assertEqual(server["command"], EXE)
        self.assertIn("--domain", server["args"])
        self.assertEqual(
            tomllib.loads(self.codex.read_text())["mcp_servers"]["tilth"]["command"],
            EXE,
        )
        self.assertEqual(stat.S_IMODE(self.claude.stat().st_mode), 0o600)
        records = self.history()
        self.assertEqual(len(records), 3)
        for record in records:
            backup = Path(record["backup"])
            self.assertEqual(backup.read_bytes(), self.originals[Path(record["path"])])
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_second_apply_writes_nothing(self):
        apply(self.context)
        after_first = {path: path.read_bytes() for path in self.originals}

        applied = apply(self.context)

        self.assertEqual([p.written for p in applied], [False, False, False])
        self.assertEqual(len(self.history()), 3)
        for path, content in after_first.items():
            self.assertEqual(path.read_bytes(), content)

    def test_undo_restores_the_exact_original_bytes(self):
        apply(self.context)

        undone = undo(self.context)

        self.assertEqual({how for _, how in undone}, {"restored"})
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(undo(self.context), [])

    def test_undo_unwraps_in_place_when_the_app_changed_the_file(self):
        apply(self.context)
        document = json.loads(self.claude.read_text())
        document["numStartups"] = 4  # the app wrote after setup
        self.claude.write_text(json.dumps(document, indent=2))

        undone = dict((config.path, how) for config, how in undo(self.context))

        self.assertEqual(undone[self.claude], "unwrapped")
        restored = json.loads(self.claude.read_text())
        self.assertEqual(restored["numStartups"], 4)
        self.assertEqual(restored["mcpServers"], CLAUDE["mcpServers"])

    def test_profiles_mapped_to_one_domain_share(self):
        self.write(
            ".config/mcp-stdio-mux/config.toml", '[profiles]\nwork = "default"\n'
        )
        context = Context(
            self.home,
            {**self.environ, "XDG_CONFIG_HOME": str(self.home / ".config")},
            EXE,
            "darwin",
        )

        apply(context)

        args = json.loads(self.work.read_text())["mcpServers"]["github"]["args"]
        self.assertEqual(args[args.index("--domain") + 1], "default")

    def test_a_broken_file_is_reported_and_the_rest_still_applies(self):
        self.write(".cursor/mcp.json", "{ not json")

        applied = apply(self.context)

        broken = [p for p in applied if p.error]
        self.assertEqual([p.config.app for p in broken], ["Cursor"])
        self.assertEqual(sum(p.written for p in applied), 3)

    def test_symlinked_config_stays_a_symlink(self):
        real = self.write("dotfiles/claude.json", json.dumps(CLAUDE))
        self.claude.unlink()
        self.claude.symlink_to(real)

        apply(self.context)

        self.assertTrue(self.claude.is_symlink())
        self.assertIn(EXE, real.read_text())

    def test_skill_is_installed_once_per_real_skills_directory(self):
        shared = self.home / "shared-skills"
        shared.mkdir()
        (self.home / ".claude").mkdir(exist_ok=True)
        (self.home / ".claude/skills").symlink_to(shared)
        (self.home / ".claude-work/skills").symlink_to(shared)

        written = install_skills(plan(self.context), "---\nname: mcp-stdio-mux\n---\n")

        self.assertEqual(
            sorted(path.relative_to(self.home).as_posix() for path in written),
            [
                ".codex/skills/mcp-stdio-mux/SKILL.md",
                "shared-skills/mcp-stdio-mux/SKILL.md",
            ],
        )
        undo(self.context)
        self.assertFalse((shared / "mcp-stdio-mux").exists())


class FindExecutableTests(unittest.TestCase):
    def test_ephemeral_runner_paths_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in (".cache/uv/archive-v0/abc/bin", ".local/bin"):
                exe = root / folder / "mcp-stdio-mux"
                exe.parent.mkdir(parents=True)
                exe.write_text("#!/bin/sh\n")
                exe.chmod(0o755)
            path = os.pathsep.join(
                [str(root / ".cache/uv/archive-v0/abc/bin"), str(root / ".local/bin")]
            )

            self.assertEqual(
                find_executable({"PATH": path}), str(root / ".local/bin/mcp-stdio-mux")
            )
            self.assertIsNone(
                find_executable({"PATH": str(root / ".cache/uv/archive-v0/abc/bin")})
            )


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        config = self.home / ".cursor/mcp.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"mcpServers": {"a": {"command": "a-mcp"}}}))
        self.config = config
        self.original = config.read_bytes()

    def cli(self, *arguments):
        environment = {
            "HOME": str(self.home),
            "PATH": os.environ["PATH"],
            "PYTHONPATH": SRC,
            "MCP_STDIO_MUX_RUNTIME_DIR": str(self.home / "runtime"),
        }
        return subprocess.run(
            [sys.executable, "-m", "mcp_stdio_mux", *arguments],
            env=environment,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_setup_status_and_undo_from_the_command_line(self):
        dry = self.cli("setup", "--dry-run", "--exe", EXE)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("Cursor", dry.stdout)
        self.assertEqual(self.config.read_bytes(), self.original)

        refused = self.cli("setup", "--exe", EXE)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--yes", refused.stderr)
        self.assertEqual(self.config.read_bytes(), self.original)

        done = self.cli("setup", "--yes", "--exe", EXE)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(EXE, self.config.read_text())

        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("1 shared", status.stdout)
        self.assertIn("No shared backends are running", status.stdout)

        undone = self.cli("undo", "--yes")
        self.assertEqual(undone.returncode, 0, undone.stderr)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_version(self):
        from mcp_stdio_mux import __version__

        self.assertEqual(
            self.cli("--version").stdout.strip(), f"mcp-stdio-mux {__version__}"
        )


if __name__ == "__main__":
    unittest.main()
