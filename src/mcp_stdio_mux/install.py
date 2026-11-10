"""Setup and undo: rewrite every detected config safely, and put it all back.

Safety rules: work on bytes (line endings survive), refuse to write a file that
changed since it was read, back up its bytes, write atomically through symlinks
with the exact permissions, read it back, and record what was written so `undo`
can restore exact bytes, even for a profile it can no longer detect.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from mcp_stdio_mux import __version__
from mcp_stdio_mux.clients import (
    ConfigFile,
    Edit,
    Server,
    detect,
    edit,
    unwrap_command,
    wrap_command,
)
from mcp_stdio_mux.config import Config, load_config

SKILL_NAME = "mcp-stdio-mux"
EPHEMERAL_MARKERS = ("/.cache/", "/Caches/", "/pipx/.cache/")


@dataclass(frozen=True)
class Context:
    home: Path
    environ: Mapping[str, str]
    exe: str
    platform: str = sys.platform

    @property
    def state_dir(self) -> Path:
        base = self.environ.get("XDG_STATE_HOME") or self.home / ".local/state"
        return Path(base) / "mcp-stdio-mux"


@dataclass
class Planned:
    config: ConfigFile
    domain: str
    original: bytes = b""
    edit: Edit | None = None
    error: str | None = None
    written: bool = False

    @property
    def changes(self) -> int:
        return sum(r.status == "changed" for r in self.edit.results) if self.edit else 0


def plan(context: Context) -> list[Planned]:
    """What `apply` would do, computed from the files as they are now."""
    config = load_config(context.environ)
    planned = []
    for found in detect(context.home, context.environ, context.platform):
        item = Planned(found, config.domain(found.profile))
        try:
            item.original = found.path.read_bytes()
            text = item.original.decode("utf-8")
            item.edit = edit(found, text, _wrapper(context, config, item))
        except (OSError, ValueError) as error:  # ValueError: decode, JSON, TOML
            item.error = str(error)
        planned.append(item)
    return planned


def apply(context: Context) -> list[Planned]:
    planned = plan(context)  # fresh read: an app may have written since the preview
    for item in planned:
        if item.edit and item.edit.changed:
            try:
                data = item.edit.text.encode("utf-8")
                _write(context, item.config, data, expected=item.original)
                item.written = True
            except (OSError, ValueError) as error:
                item.error = str(error)
    return planned


def undo(context: Context) -> list[tuple[ConfigFile, str]]:
    """Restore exact bytes where nothing touched the file since; unwrap otherwise.

    Walks every file setup ever wrote as well as every file detected now, so a
    profile set up from a shell with CLAUDE_CONFIG_DIR is found from any shell.
    """
    records = _history(context)
    latest = {record["path"]: record for record in records}
    undone = []
    for found in _undo_candidates(context, records):
        try:
            how = _undo_file(context, found, latest.get(str(found.path.resolve())))
        except (OSError, ValueError) as error:
            how = f"failed: {error}"
        if how:
            undone.append((found, how))
    remove_skills(context)
    return undone


def _undo_file(context: Context, found: ConfigFile, record: dict | None) -> str:
    current = found.path.read_bytes()
    how = ""
    if record and _digest(current) == record["written"]:
        backup = Path(record["backup"])
        if backup.is_file():
            original = backup.read_bytes()
            _write(context, found, original, expected=current, record=False)
            current, how = original, "restored"
    unwrapped = edit(
        found, current.decode("utf-8"), lambda s: unwrap_command(s.command)
    )
    if unwrapped.changed:
        # Not recorded: a later undo must never "restore" wrapped bytes.
        data = unwrapped.text.encode("utf-8")
        _write(context, found, data, expected=current, record=False)
        how = "unwrapped"
    return how


def _undo_candidates(context: Context, records: list[dict]) -> list[ConfigFile]:
    found = detect(context.home, context.environ, context.platform)
    seen = {config.path.resolve() for config in found}
    for record in records:
        path = Path(record["path"])
        if path not in seen and path.is_file() and record.get("kind"):
            seen.add(path)
            skills = Path(record["skills"]) if record.get("skills") else None
            found.append(
                ConfigFile(
                    record.get("app", "?"),
                    record.get("profile", "?"),
                    path,
                    record["kind"],
                    skills,
                )
            )
    return found


def install_skills(planned: list[Planned], text: str) -> list[Path]:
    """Put SKILL.md in every profile whose agent reads skills; once per real dir."""
    written, seen = [], set()
    for item in planned:
        skills = item.config.skills
        if skills is None or not skills.parent.is_dir():
            continue
        target = skills.resolve() / SKILL_NAME / "SKILL.md"
        if target in seen:
            continue
        seen.add(target)
        if not target.exists() or target.read_text() != text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        written.append(target)
    return written


def remove_skills(context: Context) -> list[Path]:
    folders = {
        config.skills.resolve() / SKILL_NAME
        for config in _undo_candidates(context, _history(context))
        if config.skills
    }
    removed = []
    for folder in sorted(folders):
        skill = folder / "SKILL.md"
        if skill.is_file() and f"name: {SKILL_NAME}" in skill.read_text():
            skill.unlink()
            if not any(folder.iterdir()):
                folder.rmdir()
            removed.append(skill)
    return removed


def skill_text() -> str:
    packaged = resources.files("mcp_stdio_mux") / "SKILL.md"
    if packaged.is_file():
        return packaged.read_text()
    # A source checkout keeps the one copy in skills/; the wheel bundles it.
    return (Path(__file__).parents[2] / "skills" / SKILL_NAME / "SKILL.md").read_text()


def find_executable(environ: Mapping[str, str]) -> str | None:
    """A stable `mcp-stdio-mux` on PATH; uvx and pipx-run copies are temporary."""
    for entry in environ.get("PATH", "").split(os.pathsep):
        if not os.path.isabs(entry):
            continue  # "" and "." mean the current directory: never trust it
        candidate = Path(entry) / "mcp-stdio-mux"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        real = candidate.resolve().as_posix()
        if any(marker in real for marker in EPHEMERAL_MARKERS):
            continue
        return str(candidate)
    return None


def install_persistently() -> str:
    """Install this version as a tool so configs point at a path that stays."""
    requirement = f"mcp-stdio-mux=={__version__}"
    if shutil.which("uv"):
        subprocess.run(["uv", "tool", "install", requirement], check=True)
        folder = subprocess.run(
            ["uv", "tool", "dir", "--bin"], check=True, capture_output=True, text=True
        ).stdout.strip()
    elif shutil.which("pipx"):
        subprocess.run(["pipx", "install", requirement], check=True)
        folder = subprocess.run(
            ["pipx", "environment", "--value", "PIPX_BIN_DIR"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    else:
        raise RuntimeError("install uv or pipx first, then run setup again")
    exe = Path(folder) / "mcp-stdio-mux"
    if not exe.is_file():
        raise RuntimeError(f"installed, but {exe} is missing")
    return str(exe)


def running_agents() -> list[str]:
    """Agent apps with live sessions; their open sessions keep the old servers."""
    output = subprocess.run(
        ["ps", "-axo", "comm="], capture_output=True, text=True, check=False
    ).stdout
    names = {Path(line.strip()).name.lower() for line in output.splitlines()}
    apps = {
        "claude": "Claude Code",
        "codex": "Codex",
        "cursor": "Cursor",
        "opencode": "OpenCode",
        "windsurf": "Windsurf",
    }
    return sorted({label for name, label in apps.items() if name in names})


def _wrapper(context: Context, config: Config, item: Planned):
    def transform(server: Server) -> list[str]:
        return wrap_command(context.exe, item.domain, server, config)

    return transform


def _write(
    context: Context,
    config: ConfigFile,
    data: bytes,
    expected: bytes,
    record: bool = True,
) -> None:
    target = config.path.resolve()  # write through a symlink, never replace it
    before = target.read_bytes()
    if before != expected:
        raise OSError(f"{target} changed since it was read; run the command again")
    mode = stat.S_IMODE(target.stat().st_mode)
    backup = _backup(context, target, before)
    temporary = target.with_name(f".{target.name}.mcp-stdio-mux-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, mode)  # exact mode: os.open's would pass the umask
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    if target.read_bytes() != data:
        raise OSError(f"{target} changed while it was being written; run it again")
    if record:
        records = _history(context)
        records.append(
            {
                "path": str(target),
                "backup": str(backup),
                "written": _digest(data),
                "time": time.time(),
                "app": config.app,
                "profile": config.profile,
                "kind": config.kind,
                "skills": str(config.skills) if config.skills else None,
            }
        )
        _state_file(context, "history.json").write_text(json.dumps(records, indent=2))


def _backup(context: Context, target: Path, content: bytes) -> Path:
    folder = context.state_dir / "backups" / time.strftime("%Y%m%dT%H%M%S")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unique per write: setup and undo within one second must not collide.
    name = f"{_digest(str(target).encode())[:8]}-{time.time_ns()}-{target.name}"
    backup = folder / name.replace("-.", "-")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
    return backup


def _history(context: Context) -> list[dict]:
    try:
        return json.loads(_state_file(context, "history.json").read_text())
    except (FileNotFoundError, ValueError):
        return []


def _state_file(context: Context, name: str) -> Path:
    context.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return context.state_dir / name


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
