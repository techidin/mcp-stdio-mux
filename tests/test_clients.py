import json
import tempfile
import unittest
from pathlib import Path

from mcp_stdio_mux.clients import (
    ConfigFile,
    EditError,
    detect,
    edit,
    is_wrapped,
    unwrap_command,
    wrap_command,
)
from mcp_stdio_mux.config import Config

EXE = "/home/u/.local/bin/mcp-stdio-mux"
CONFIG = Config(Path("/nonexistent/config.toml"))


def wrapper(domain="default"):
    def transform(server):
        return wrap_command(EXE, domain, server, CONFIG)

    return transform


def unwrapper(server):
    return unwrap_command(server.command)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class DetectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)

    def found(self, environ=None, platform="darwin"):
        return [
            (config.app, config.profile, config.path.relative_to(self.home).as_posix())
            for config in detect(self.home, environ or {}, platform)
        ]

    def test_every_app_and_profile_is_found(self):
        for relative in (
            ".claude.json",
            ".claude/.claude.json",
            ".claude-work/.claude.json",
            "Library/Application Support/Claude/claude_desktop_config.json",
            ".codex/config.toml",
            ".codex-work/config.toml",
            ".gemini/settings.json",
            ".cursor/mcp.json",
            ".codeium/windsurf/mcp_config.json",
            ".config/opencode/opencode.json",
        ):
            write(self.home / relative, "{}")

        self.assertEqual(
            self.found(),
            [
                ("Claude Code", "default", ".claude.json"),
                ("Claude Code", "default", ".claude/.claude.json"),
                ("Claude Code", "work", ".claude-work/.claude.json"),
                (
                    "Claude Desktop",
                    "default",
                    "Library/Application Support/Claude/claude_desktop_config.json",
                ),
                ("Codex", "default", ".codex/config.toml"),
                ("Codex", "work", ".codex-work/config.toml"),
                ("Gemini CLI", "default", ".gemini/settings.json"),
                ("Cursor", "default", ".cursor/mcp.json"),
                ("Windsurf", "default", ".codeium/windsurf/mcp_config.json"),
                ("OpenCode", "default", ".config/opencode/opencode.json"),
            ],
        )

    def test_backups_and_missing_files_are_ignored(self):
        write(self.home / ".claude.bak/.claude.json", "{}")
        write(self.home / ".claude-backup-2026/.claude.json", "{}")
        (self.home / ".codex-empty").mkdir()

        self.assertEqual(self.found(), [])

    def test_profile_dirs_from_the_environment_are_included_once(self):
        write(self.home / ".claude-work/.claude.json", "{}")
        write(self.home / "elsewhere/claude-alt/.claude.json", "{}")
        write(self.home / "elsewhere/codex-alt/config.toml", "")
        environ = {
            "CLAUDE_CONFIG_DIR": str(self.home / "elsewhere/claude-alt"),
            "CODEX_HOME": str(self.home / "elsewhere/codex-alt"),
        }
        found = self.found(environ)

        self.assertIn(
            ("Claude Code", "alt", "elsewhere/claude-alt/.claude.json"), found
        )
        self.assertIn(("Codex", "alt", "elsewhere/codex-alt/config.toml"), found)
        self.assertEqual(len(found), 3)
        same = self.found({"CLAUDE_CONFIG_DIR": str(self.home / ".claude-work")})
        self.assertEqual(same, [("Claude Code", "work", ".claude-work/.claude.json")])

    def test_claude_desktop_on_linux_lives_under_xdg_config(self):
        write(self.home / ".config/Claude/claude_desktop_config.json", "{}")

        self.assertEqual(
            self.found(platform="linux"),
            [
                (
                    "Claude Desktop",
                    "default",
                    ".config/Claude/claude_desktop_config.json",
                )
            ],
        )

    def test_skill_directories_follow_the_profile(self):
        write(self.home / ".claude-work/.claude.json", "{}")
        write(self.home / ".codex/config.toml", "")
        write(self.home / ".cursor/mcp.json", "{}")

        skills = {c.app: c.skills for c in detect(self.home, {}, "darwin")}

        self.assertEqual(skills["Claude Code"], self.home / ".claude-work/skills")
        self.assertEqual(skills["Codex"], self.home / ".codex/skills")
        self.assertIsNone(skills["Cursor"])


class CommandTests(unittest.TestCase):
    def test_wrap_then_unwrap_round_trips(self):
        from mcp_stdio_mux.clients import Server

        server = Server("mcpServers", "github", ["npx", "-y", "gh"], ("TOKEN",), None)
        wrapped = wrap_command(EXE, "work", server, CONFIG)

        self.assertEqual(
            wrapped,
            [
                EXE,
                "run",
                "--domain",
                "work",
                "--server",
                "github",
                "--env-keys",
                "TOKEN",
                "--",
                "npx",
                "-y",
                "gh",
            ],
        )
        self.assertTrue(is_wrapped(wrapped))
        self.assertEqual(unwrap_command(wrapped), ["npx", "-y", "gh"])
        rewrapped = wrap_command(EXE, "work", server._replace(command=wrapped), CONFIG)
        self.assertEqual(rewrapped, wrapped)

    def test_native_servers_stay_unwrapped_and_get_unwrapped(self):
        from mcp_stdio_mux.clients import Server

        native = ["/usr/local/bin/codebase-memory-mcp"]
        server = Server("mcpServers", "cbm", [EXE, "run", "--", *native], (), None)

        self.assertEqual(wrap_command(EXE, "default", server, CONFIG), native)

    def test_configured_cwd_becomes_the_backend_workspace(self):
        from mcp_stdio_mux.clients import Server

        server = Server("mcpServers", "docs", ["docs-mcp"], (), "/srv/docs")

        self.assertEqual(
            wrap_command(EXE, "default", server, CONFIG)[:8],
            [
                EXE,
                "run",
                "--domain",
                "default",
                "--server",
                "docs",
                "--workspace",
                "/srv/docs",
            ],
        )


class JsonEditTests(unittest.TestCase):
    def config(self, kind="json"):
        return ConfigFile("Cursor", "default", Path("/x/mcp.json"), kind, None)

    def test_stdio_servers_are_wrapped_and_remote_ones_left_alone(self):
        text = json.dumps(
            {
                "theme": "dark",
                "mcpServers": {
                    "github": {
                        "command": "npx",
                        "args": ["-y", "gh"],
                        "env": {"GITHUB_TOKEN": "secret"},
                    },
                    "remote": {"url": "https://example.com/mcp"},
                    "bare": {"command": "bare-mcp"},
                },
            },
            indent=2,
        )

        result = edit(self.config(), text, wrapper())
        document = json.loads(result.text)

        github = document["mcpServers"]["github"]
        self.assertEqual(github["command"], EXE)
        self.assertEqual(
            github["args"],
            [
                "run",
                "--domain",
                "default",
                "--server",
                "github",
                "--env-keys",
                "GITHUB_TOKEN",
                "--",
                "npx",
                "-y",
                "gh",
            ],
        )
        self.assertEqual(github["env"], {"GITHUB_TOKEN": "secret"})
        self.assertEqual(
            document["mcpServers"]["remote"], {"url": "https://example.com/mcp"}
        )
        self.assertEqual(
            document["mcpServers"]["bare"]["args"][-2:], ["--", "bare-mcp"]
        )
        self.assertEqual(document["theme"], "dark")
        self.assertEqual(
            [(r.name, r.status) for r in result.results],
            [("github", "changed"), ("remote", "remote"), ("bare", "changed")],
        )

    def test_second_wrap_changes_nothing_and_unwrap_restores_the_document(self):
        original = {"mcpServers": {"a": {"command": "a-mcp", "args": ["--x"]}}}
        text = json.dumps(original, indent=2) + "\n"

        once = edit(self.config(), text, wrapper())
        twice = edit(self.config(), once.text, wrapper())
        undone = edit(self.config(), once.text, unwrapper)

        self.assertEqual(twice.text, once.text)
        self.assertFalse(twice.changed)
        self.assertEqual(json.loads(undone.text), original)
        self.assertEqual(undone.text, text)

    def test_untouched_file_keeps_its_exact_bytes(self):
        text = '{"mcpServers": {"r": {"url": "https://x"}}}'

        result = edit(self.config(), text, wrapper())

        self.assertEqual(result.text, text)
        self.assertFalse(result.changed)

    def test_claude_code_project_servers_are_wrapped_too(self):
        text = json.dumps(
            {
                "mcpServers": {"global": {"type": "stdio", "command": "g"}},
                "projects": {
                    "/repo": {"mcpServers": {"local": {"command": "l"}}},
                    "/other": {"allowedTools": []},
                },
            }
        )

        result = edit(self.config("claude-code"), text, wrapper())
        document = json.loads(result.text)

        self.assertEqual(document["mcpServers"]["global"]["command"], EXE)
        self.assertEqual(document["mcpServers"]["global"]["type"], "stdio")
        self.assertEqual(
            document["projects"]["/repo"]["mcpServers"]["local"]["command"], EXE
        )
        self.assertEqual(document["projects"]["/other"], {"allowedTools": []})

    def test_claude_code_http_type_is_remote(self):
        text = json.dumps({"mcpServers": {"h": {"type": "http", "command": "x"}}})

        result = edit(self.config("claude-code"), text, wrapper())

        self.assertEqual([r.status for r in result.results], ["remote"])

    def test_non_string_args_are_reported_not_rewritten(self):
        text = json.dumps({"mcpServers": {"odd": {"command": "x", "args": [1]}}})

        result = edit(self.config(), text, wrapper())

        self.assertFalse(result.changed)
        self.assertTrue(result.results[0].status.startswith("unsupported"))

    def test_invalid_json_is_an_edit_error(self):
        with self.assertRaises(EditError):
            edit(self.config(), "{ // comments are not JSON\n}", wrapper())

    def test_opencode_local_command_arrays_are_wrapped(self):
        text = json.dumps(
            {
                "mcp": {
                    "local": {
                        "type": "local",
                        "command": ["bunx", "server"],
                        "environment": {"KEY": "v"},
                    },
                    "remote": {"type": "remote", "url": "https://x"},
                }
            }
        )

        result = edit(self.config("opencode"), text, wrapper())
        local = json.loads(result.text)["mcp"]["local"]

        self.assertEqual(local["command"][:2], [EXE, "run"])
        self.assertIn("KEY", local["command"])
        self.assertEqual(local["command"][-3:], ["--", "bunx", "server"])
        self.assertEqual([r.status for r in result.results], ["changed", "remote"])


CODEX = """\
# Codex settings
model = "gpt-5"

[mcp_servers.github]
command = "npx" # launcher
args = ["-y", "@modelcontextprotocol/server-github"]
env_vars = ["GITHUB_TOKEN"]

[mcp_servers.github.env]
GITHUB_HOST = "github.com"

[mcp_servers."docs-server"]
command = 'docs-mcp'
cwd = "/srv/docs"

[mcp_servers.remote]
url = "https://example.com/mcp"

[profiles.fast]
model = "gpt-5-mini"
"""


class CodexEditTests(unittest.TestCase):
    config = ConfigFile("Codex", "default", Path("/x/config.toml"), "codex", None)

    def test_servers_are_wrapped_and_everything_else_is_kept(self):
        import tomllib

        result = edit(self.config, CODEX, wrapper())
        document = tomllib.loads(result.text)

        github = document["mcp_servers"]["github"]
        self.assertEqual(github["command"], EXE)
        self.assertEqual(
            github["args"][:8],
            [
                "run",
                "--domain",
                "default",
                "--server",
                "github",
                "--env-keys",
                "GITHUB_HOST,GITHUB_TOKEN",
                "--",
            ],
        )
        self.assertEqual(github["env"], {"GITHUB_HOST": "github.com"})
        docs = document["mcp_servers"]["docs-server"]
        self.assertIn("--workspace", docs["args"])
        self.assertEqual(docs["args"][-2:], ["--", "docs-mcp"])
        self.assertEqual(document["profiles"], {"fast": {"model": "gpt-5-mini"}})
        self.assertIn("# Codex settings", result.text)
        self.assertIn(f'command = "{EXE}" # launcher', result.text)
        self.assertEqual(
            [(r.name, r.status) for r in result.results],
            [("github", "changed"), ("docs-server", "changed"), ("remote", "remote")],
        )

    def test_wrap_is_idempotent_and_unwrap_is_exact_for_rewritten_lines(self):
        once = edit(self.config, CODEX, wrapper())
        twice = edit(self.config, once.text, wrapper())
        undone = edit(self.config, once.text, unwrapper)

        self.assertFalse(twice.changed)
        import tomllib

        self.assertEqual(tomllib.loads(undone.text), tomllib.loads(CODEX))

    def test_multi_line_args_are_reported_and_left_untouched(self):
        text = '[mcp_servers.multi]\ncommand = "m"\nargs = [\n  "--a",\n]\n'

        result = edit(self.config, text, wrapper())

        self.assertEqual(result.text, text)
        self.assertTrue(result.results[0].status.startswith("unsupported"))

    def test_inline_table_servers_are_reported_and_left_untouched(self):
        text = '[mcp_servers]\ninline = { command = "i" }\n'

        result = edit(self.config, text, wrapper())

        self.assertEqual(result.text, text)
        self.assertTrue(result.results[0].status.startswith("unsupported"))

    def test_invalid_toml_is_an_edit_error(self):
        with self.assertRaises(EditError):
            edit(self.config, "[mcp_servers.x\n", wrapper())


if __name__ == "__main__":
    unittest.main()
