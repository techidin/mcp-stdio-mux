"""Find the MCP configs of every agent app and profile, and rewrite their commands.

Each format is edited as narrowly as possible: JSON documents keep every other
key, TOML keeps every other line (comments included), and a file that nothing
changes keeps its exact bytes.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mcp_stdio_mux.config import Config

EXECUTABLE_NAME = "mcp-stdio-mux"
REMOTE_KEYS = ("url", "httpUrl", "serverUrl")
BACKUP_WORDS = ("bak", "backup")


@dataclass(frozen=True)
class ConfigFile:
    app: str
    profile: str
    path: Path
    kind: str  # "json" | "claude-code" | "opencode" | "codex"
    skills: Path | None  # where this profile's agent reads skills, if it does


class Server(NamedTuple):
    location: str
    name: str
    command: list[str]
    env_keys: tuple[str, ...]
    cwd: str | None


class Result(NamedTuple):
    location: str
    name: str
    status: str  # "changed" | "unchanged" | "remote" | "unsupported: <why>"
    command: list[str]


@dataclass(frozen=True)
class Edit:
    text: str
    results: list[Result]

    @property
    def changed(self) -> bool:
        return any(result.status == "changed" for result in self.results)


class EditError(ValueError):
    pass


Transform = Callable[[Server], list[str]]


def detect(
    home: Path, environ: Mapping[str, str], platform: str = sys.platform
) -> list[ConfigFile]:
    """Every supported config file that exists, one entry per real file."""
    candidates = [
        ConfigFile(
            "Claude Code",
            "default",
            home / ".claude.json",
            "claude-code",
            home / ".claude" / "skills",
        ),
        *(
            ConfigFile(
                "Claude Code",
                profile,
                folder / ".claude.json",
                "claude-code",
                folder / "skills",
            )
            for folder, profile in _profile_dirs(
                home, ".claude", environ.get("CLAUDE_CONFIG_DIR")
            )
        ),
        ConfigFile(
            "Claude Desktop",
            "default",
            _desktop_dir(home, environ, platform) / "claude_desktop_config.json",
            "json",
            None,
        ),
        *(
            ConfigFile(
                "Codex", profile, folder / "config.toml", "codex", folder / "skills"
            )
            for folder, profile in _profile_dirs(
                home, ".codex", environ.get("CODEX_HOME")
            )
        ),
        ConfigFile(
            "Gemini CLI", "default", home / ".gemini/settings.json", "json", None
        ),
        ConfigFile("Cursor", "default", home / ".cursor/mcp.json", "json", None),
        ConfigFile(
            "Windsurf",
            "default",
            home / ".codeium/windsurf/mcp_config.json",
            "json",
            None,
        ),
        ConfigFile(
            "OpenCode",
            "default",
            _config_home(home, environ) / "opencode/opencode.json",
            "opencode",
            None,
        ),
    ]
    found: list[ConfigFile] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate.path.is_file() and candidate.path.resolve() not in seen:
            seen.add(candidate.path.resolve())
            found.append(candidate)
    return found


def wrap_command(exe: str, domain: str, server: Server, config: Config) -> list[str]:
    """The command that routes this server through the mux (or none, if native)."""
    original = unwrap_command(server.command)
    if config.scope(original, server.name) == "native":
        return original
    # --option=value, so a name that starts with "-" is not read as a flag.
    wrapped = [exe, "run", f"--domain={domain}", f"--server={server.name}"]
    if server.cwd:
        wrapped.append(f"--workspace={server.cwd}")
    if server.env_keys:
        wrapped.append("--env-keys=" + ",".join(sorted(server.env_keys)))
    return [*wrapped, "--", *original]


def unwrap_command(command: list[str]) -> list[str]:
    if is_wrapped(command):
        return list(command[command.index("--") + 1 :])
    return list(command)


def is_wrapped(command: list[str]) -> bool:
    return (
        len(command) > 2
        and Path(command[0]).name == EXECUTABLE_NAME
        and command[1] == "run"
        and "--" in command
    )


def edit(config: ConfigFile, text: str, transform: Transform) -> Edit:
    """Apply `transform` to every local server; untouched files keep their bytes."""
    if config.kind == "codex":
        return _edit_codex(text, transform)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise EditError(f"{config.path} is not plain JSON: {error}") from error
    if not isinstance(document, dict):
        raise EditError(f"{config.path} does not hold a JSON object")
    results = [
        _edit_json_server(config.kind, location, name, server, transform, home)
        for location, servers, home in _json_server_tables(config.kind, document)
        for name, server in servers.items()
    ]
    if not any(result.status == "changed" for result in results):
        return Edit(text, results)
    rendered = json.dumps(document, indent=2, ensure_ascii=False)
    try:
        rendered.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate can only be kept escaped
        rendered = json.dumps(document, indent=2)
    rendered += "\n" if text.endswith("\n") else ""
    if "\r\n" in text:
        rendered = rendered.replace("\n", "\r\n")  # JSON strings hold no raw \n
    return Edit(rendered, results)


def _json_server_tables(
    kind: str, document: MutableMapping[str, object]
) -> Iterator[tuple[str, MutableMapping[str, object], str | None]]:
    """Each server table with the directory its servers start in, if fixed."""
    key = "mcp" if kind == "opencode" else "mcpServers"
    if isinstance(document.get(key), dict):
        yield key, document[key], None
    projects = document.get("projects") if kind == "claude-code" else None
    for project, settings in (projects or {}).items():
        if isinstance(settings, dict) and isinstance(settings.get("mcpServers"), dict):
            # Claude Code starts a project's own servers in that project.
            yield f"projects[{project}]", settings["mcpServers"], project


def _edit_json_server(
    kind: str,
    location: str,
    name: str,
    server: object,
    transform: Transform,
    home: str | None,
) -> Result:
    def result(status: str, command: list[str] | None = None) -> Result:
        return Result(location, name, status, command or [])

    if not isinstance(server, dict):
        return result("unsupported: not an object")
    if any(key in server for key in REMOTE_KEYS) or server.get("type") in (
        "http",
        "sse",
        "remote",
        "streamable-http",
    ):
        return result("remote")
    if kind == "opencode":
        command = server.get("command")
        env = server.get("environment", {})
    else:
        executable, args = server.get("command"), server.get("args", [])
        command = [executable, *args] if isinstance(args, list) else None
        env = server.get("env", {})
    if not _is_command(command):
        return result("unsupported: command is not a list of strings")
    cwd = server.get("cwd") if isinstance(server.get("cwd"), str) else home
    current = list(command)
    new = transform(Server(location, name, current, _keys(env), cwd))
    if new == current:
        return result("unchanged", current)
    if kind == "opencode":
        server["command"] = new
    else:
        server["command"] = new[0]
        server["args"] = new[1:]
        if not new[1:]:
            del server["args"]  # unwrapping a server that never had args
    return result("changed", new)


_SERVER_HEADER = re.compile(
    r'^\[mcp_servers\.(?:"([^"\\]+)"|([A-Za-z0-9_-]+))\][ \t]*(?:#[^\r\n]*)?\r?$',
    re.M,
)
_ANY_HEADER = re.compile(r"^[ \t]*\[", re.M)


def _edit_codex(text: str, transform: Transform) -> Edit:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise EditError(f"config.toml is not valid TOML: {error}") from error
    servers = document.get("mcp_servers", {})
    headers = {
        (match.group(1) or match.group(2)): match
        for match in _SERVER_HEADER.finditer(text)
    }
    expected = copy.deepcopy(document)
    results: list[Result] = []
    replacements: list[tuple[int, int, str]] = []
    for name, server in servers.items() if isinstance(servers, dict) else []:
        outcome = _plan_codex_server(text, name, server, headers.get(name), transform)
        results.append(outcome[0])
        if outcome[1] is not None:
            replacements.append(outcome[1])
            new = outcome[0].command
            expected["mcp_servers"][name]["command"] = new[0]
            expected["mcp_servers"][name]["args"] = new[1:]
            if not new[1:]:
                del expected["mcp_servers"][name]["args"]
    if not replacements:
        return Edit(text, results)
    new_text = text
    for start, end, section in sorted(replacements, reverse=True):
        new_text = new_text[:start] + section + new_text[end:]
    try:
        round_trip = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        round_trip = None
    if round_trip != expected:
        raise EditError("config.toml rewrite did not round-trip; nothing written")
    return Edit(new_text, results)


def _plan_codex_server(
    text: str,
    name: str,
    server: object,
    header: re.Match | None,
    transform: Transform,
) -> tuple[Result, tuple[int, int, str] | None]:
    def result(status: str, command: list[str] | None = None) -> Result:
        return Result("mcp_servers", name, status, command or [])

    if not isinstance(server, dict):
        return result("unsupported: not a table"), None
    if "url" in server or "command" not in server:
        return result("remote"), None
    command = [server["command"], *server.get("args", [])]
    if header is None:
        return result("unsupported: not a [mcp_servers.<name>] table"), None
    if not _is_command(command):
        return result("unsupported: command is not a list of strings"), None
    following = _ANY_HEADER.search(text, header.end())
    start, end = header.end(), following.start() if following else len(text)
    section = text[start:end]
    command_line = _value_line(section, "command", server["command"])
    args_line = _value_line(section, "args", server.get("args"))
    if command_line is None or ("args" in server and args_line is None):
        return result("unsupported: command or args span several lines"), None
    env_keys = set(server.get("env", {})) | {
        key for key in server.get("env_vars", []) if isinstance(key, str)
    }
    cwd = server.get("cwd") if isinstance(server.get("cwd"), str) else None
    new = transform(Server("mcp_servers", name, command, _keys(env_keys), cwd))
    if new == command:
        return result("unchanged", command), None
    # ensure_ascii=False: TOML has no surrogate-pair escapes for emoji.
    section = _replace_line(
        section, command_line, "command", json.dumps(new[0], ensure_ascii=False)
    )
    args = json.dumps(new[1:], ensure_ascii=False)
    newline = "\r\n" if "\r\n" in text else "\n"
    if args_line is not None:
        args_line = _value_line(section, "args", server.get("args"))
        assert args_line is not None
        if new[1:]:
            section = _replace_line(section, args_line, "args", args)
        else:  # unwrapping a server that never had args: drop the line
            after = args_line.end() + len(newline)
            section = section[: args_line.start()] + section[after:]
    elif new[1:]:
        line = _value_line(section, "command", new[0])
        assert line is not None
        insert = f"{newline}args = {args}"
        section = section[: line.end()] + insert + section[line.end() :]
    return result("changed", new), (start, end, section)


def _value_line(section: str, key: str, expected: object) -> re.Match | None:
    """The single line assigning `key`, if it alone yields the parsed value."""
    for match in re.finditer(rf"^{key}[ \t]*=[^\r\n]*", section, re.M):
        try:
            if tomllib.loads(match.group(0)).get(key) == expected:
                return match
        except tomllib.TOMLDecodeError:
            continue
    return None


def _replace_line(section: str, line: re.Match, key: str, value: str) -> str:
    """Swap the value, keeping a trailing comment if the line had one."""
    text = line.group(0)
    comment = ""
    for position in (i for i, char in enumerate(text) if char == "#"):
        try:
            tomllib.loads(text[:position])
        except tomllib.TOMLDecodeError:
            continue
        comment = " " + text[position:]
        break
    return section[: line.start()] + f"{key} = {value}{comment}" + section[line.end() :]


def _profile_dirs(
    home: Path, stem: str, from_env: str | None
) -> list[tuple[Path, str]]:
    folders = [
        folder
        for folder in sorted(home.glob(stem + "*"))
        if folder.is_dir()
        and (folder.name == stem or folder.name[len(stem)] in "-_")
        and not any(word in folder.name.lower() for word in BACKUP_WORDS)
    ]
    if from_env:
        folders.append(Path(from_env).expanduser())
    bare = stem.lstrip(".")
    profiles = []
    for folder in folders:
        name = folder.name.lstrip(".")
        name = name[len(bare) :].lstrip("-_") if name.startswith(bare) else name
        profiles.append((folder, name or "default"))
    return profiles


def _desktop_dir(home: Path, environ: Mapping[str, str], platform: str) -> Path:
    if platform == "darwin":
        return home / "Library/Application Support/Claude"
    return _config_home(home, environ) / "Claude"


def _config_home(home: Path, environ: Mapping[str, str]) -> Path:
    return Path(environ.get("XDG_CONFIG_HOME") or home / ".config")


def _is_command(command: object) -> bool:
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(part, str) for part in command)
    )


def _keys(env: object) -> tuple[str, ...]:
    if isinstance(env, dict | set | list | tuple):
        return tuple(sorted(key for key in env if isinstance(key, str)))
    return ()
