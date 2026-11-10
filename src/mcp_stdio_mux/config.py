"""User configuration: which profiles share, and per-server scope overrides."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SCOPES = ("native", "global", "workspace", "session")

# Servers the automatic detection gets wrong. A user entry with the same key wins.
BUILTIN_SCOPES: Mapping[str, str] = {
    # Ships its own daemon; each session's process is already a thin client.
    "codebase-memory-mcp": "native",
    # Indexes the directory it starts in, but takes no flag that says so.
    "tilth": "workspace",
}


@dataclass(frozen=True)
class Config:
    path: Path
    profiles: Mapping[str, str] = field(default_factory=dict)
    servers: Mapping[str, str] = field(default_factory=dict)

    def domain(self, profile: str) -> str:
        """Profiles share backends only inside one domain; default: their own."""
        return self.profiles.get(profile, profile)

    def scope(self, command: Sequence[str], server: str) -> str | None:
        """Override for a backend, keyed by executable name, else server name."""
        executable = Path(command[0]).name if command else ""
        for table in (self.servers, BUILTIN_SCOPES):
            for key in (executable, server):
                if key in table:
                    return table[key]
        return None


def config_path(environ: Mapping[str, str] = os.environ) -> Path:
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-stdio-mux" / "config.toml"


def load_config(environ: Mapping[str, str] = os.environ) -> Config:
    path = config_path(environ)
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return Config(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path}: {error}") from error
    profiles = _string_table(data, "profiles", path)
    servers = _string_table(data, "servers", path)
    for name, scope in servers.items():
        if scope not in SCOPES:
            raise ValueError(
                "{}: server {} has scope {!r}; use one of {}".format(
                    path, name, scope, ", ".join(SCOPES)
                )
            )
    return Config(path, profiles, servers)


def _string_table(data: Mapping[str, object], name: str, path: Path) -> dict[str, str]:
    table = data.get(name, {})
    if not isinstance(table, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value
        for key, value in table.items()
    ):
        raise ValueError(f"{path}: [{name}] must map names to non-empty strings")
    return dict(table)
