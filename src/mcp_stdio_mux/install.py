"""Setup and undo: rewrite every detected config safely, and put it all back.

Safety rules: re-read each file right before writing it, back up its bytes,
write atomically through symlinks with the same permissions, re-read to
confirm, and record what was written so `undo` can restore exact bytes.
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
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from mcp_stdio_mux import __version__
from mcp_stdio_mux.clients import (
    ConfigFile,
    Edit,
    EditError,
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
    edit: Edit | None = None
    error: str | None = None
    written: bool = False
    notes: list[str] = field(default_factory=list)

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
            item.edit = edit(
                found, found.path.read_text(), _wrapper(context, config, item)
            )
        except (OSError, UnicodeDecodeError, EditError) as error:
            item.error = str(error)
        planned.append(item)
    return planned


def apply(context: Context) -> list[Planned]:
    planned = plan(context)  # fresh read: an app may have written since the preview
    for item in planned:
        if item.edit and item.edit.changed:
            try:
                _write(context, item.config.path, item.edit.text)
                item.written = True
            except OSError as error:
                item.error = str(error)
    return planned


def undo(context: Context) -> list[tuple[ConfigFile, str]]:
    """Restore exact bytes where nothing touched the file since; unwrap otherwise."""
    latest = {Path(r["path"]): r for r in _history(context)}
    undone = []
    for found in detect(context.home, context.environ, context.platform):
        target = found.path.resolve()
        record = latest.get(target)
        current = target.read_text()
        how = None
        if record and _digest(current.encode()) == record["written"]:
            current = Path(record["backup"]).read_text()
            _write(context, found.path, current, record_history=False)
            how = "restored"
        try:
            unwrapped = edit(found, current, lambda s: unwrap_command(s.command))
        except EditError:
            unwrapped = None
        if unwrapped and unwrapped.changed:
            # Not recorded: a later undo must never "restore" wrapped bytes.
            _write(context, found.path, unwrapped.text, record_history=False)
            how = "unwrapped"
        if how:
            undone.append((found, how))
    remove_skills(context)
    return undone


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
    removed = []
    for found in detect(context.home, context.environ, context.platform):
        folder = found.skills.resolve() / SKILL_NAME if found.skills else None
        skill = folder / "SKILL.md" if folder else None
        if skill and skill.is_file() and f"name: {SKILL_NAME}" in skill.read_text():
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
        candidate = Path(entry) / "mcp-stdio-mux"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        real = candidate.resolve().as_posix()
        if any(marker in real for marker in EPHEMERAL_MARKERS):
            continue
        return str(candidate.absolute())
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
    context: Context, path: Path, text: str, record_history: bool = True
) -> None:
    target = path.resolve()  # write through a symlink, never replace it
    before = target.read_bytes()
    mode = stat.S_IMODE(target.stat().st_mode)
    backup = _backup(context, target, before, mode)
    temporary = target.with_name(f".{target.name}.mcp-stdio-mux-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    written = target.read_bytes()
    if written != text.encode():
        raise OSError(f"{target} changed while it was being written; run setup again")
    if record_history:
        records = _history(context)
        records.append(
            {
                "path": str(target),
                "backup": str(backup),
                "written": _digest(written),
                "time": time.time(),
            }
        )
        _state_file(context, "history.json").write_text(json.dumps(records, indent=2))


def _backup(context: Context, target: Path, content: bytes, mode: int) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    folder = context.state_dir / "backups" / stamp
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = _digest(str(target).encode())[:8] + "-" + target.name.lstrip(".")
    backup = folder / name
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode & 0o600)
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
