# mcp-stdio-mux

One shared process per MCP server across every AI agent session, app and
profile on your machine.

Open four Claude Code sessions and each one starts its own copy of every stdio
MCP server you configured. Add Codex, Cursor, or a second profile and the copies
multiply. `mcp-stdio-mux` puts one shared backend behind all of them; each
session reaches it through a 1.2 MB relay instead of a server of its own.

## Install

```bash
uvx mcp-stdio-mux setup
```

Setup finds Claude Code (every profile), Claude Desktop, Codex (every profile),
Gemini CLI, Cursor, Windsurf and OpenCode, shows what it will change, and asks
once. It installs itself as a `uv` tool so your configs point at a path that
stays. Restart your agent sessions afterwards.

Without `uv`: `pipx install mcp-stdio-mux && mcp-stdio-mux setup`.

Setup also gives Claude Code and Codex an agent skill, so you can ask an agent
to "share my MCP servers" or "share the server I just added".

## Measured

Four sessions of one server, every process the sessions caused (relays, daemon,
backend and its children), macOS 27.2 on Apple silicon with 16 GB, two runs on
10 November 2026. The unit is `phys_footprint`, the "Memory" column of Activity
Monitor.

| Server | Without a mux | With mcp-stdio-mux | Saved |
|---|---|---|---|
| serena (Python) | 679–710 MB | 202–203 MB | about 71% |
| tilth (Node) | 57 MB | 36 MB | about 37% |

Each session's own process drops from 12–94 MB to 1.2 MB. The saving grows with
the number of sessions and with how heavy the server is. Reproduce it with any
server:

```bash
python scripts/benchmark.py --sessions 4 -- tilth --mcp
python scripts/benchmark.py --sessions 4 \
  --prefix "mcp-stdio-mux run --domain bench --server tilth" -- tilth --mcp
```

## What setup changes, and what it never touches

- Each local (stdio) server's command in your user-level config files becomes
  `mcp-stdio-mux run --domain <profile> --server <name> -- <original command>`.
  The original command stays readable after `--`.
- Every file is backed up to `~/.local/state/mcp-stdio-mux/backups/` first,
  written atomically through symlinks with its permissions kept, then read back.
- Remote (HTTP or SSE) servers, project `.mcp.json` files, and servers you mark
  `native` are never touched. A file that nothing changes keeps its exact bytes.
- `mcp-stdio-mux undo` restores each file's exact bytes when the app has not
  written to it since setup, and removes only the wrapper when it has.
- Running setup a second time changes nothing that is already shared.

## Who shares with whom

Two sessions share one backend only when all of these match:

| Part | Where it comes from |
|---|---|
| Domain | The profile name: `~/.claude-work` and `~/.codex-work` are both `work`; default profiles are `default`. |
| Command | The exact backend command and arguments. |
| Credentials | A SHA-256 of the environment variables the config declares. Values are never logged. |
| Workspace | The git root, for servers bound to the directory they start in. |

Differently named profiles never share unless you map them to one domain.

Each backend also gets a scope:

| Scope | When | Backend |
|---|---|---|
| `global` | Default. | One per signature, started in your home directory. |
| `workspace` | An argument flag names `cwd`, `project`, `workspace` or `root`, an argument is `.`, or the config sets `cwd`. | One per signature and git root. |
| `session` | The first `tools/list` reply has a tool named like `browser`, `tab`, `page`, `navigate`, `click`, `screenshot`, `session`, `login` or `logout`. | One per session. |

Until a server is classified, a second session waits (up to 20 seconds), so no
session attaches to a stateful server before the mux knows it is one.

## Configuration

Optional. `~/.config/mcp-stdio-mux/config.toml`:

```toml
[profiles]
work = "default"          # let the work profile share with the default one

[servers]
my-browser = "session"    # by server name or executable name
heavy-indexer = "native"  # leave it alone; takes effect on the next setup
```

Scopes are `global`, `workspace`, `session` and `native`.

## Commands

| Command | What it does |
|---|---|
| `mcp-stdio-mux status` | Which servers each config shares, and every running backend with its sessions and memory. |
| `mcp-stdio-mux setup [--dry-run] [--yes]` | Share every stdio server in every detected config. |
| `mcp-stdio-mux undo [--yes]` | Put every config back and remove the skill. |
| `mcp-stdio-mux run ...` | What your apps call. You do not run this yourself. |

## How it works

1. The app starts `mcp-stdio-mux run`, a short-lived shim.
2. The shim finds or starts the daemon for its launch signature (one lock per
   signature), passes its stdin and stdout to the daemon over the Unix socket
   (`SCM_RIGHTS`), and replaces itself with `/bin/cat` named
   `mcp-stdio-mux: <server>`. That relay is the 1.2 MB the app keeps.
3. The daemon runs the backend in its own process group. It gives each session's
   JSON-RPC ids and progress tokens a private namespace, answers later
   `initialize` calls from the first result, and sends server requests (roots,
   sampling, elicitation) to a session that supports them.
4. A session ends when the app closes its stdin, kills the relay, or exits. When
   the last session is gone, the daemon waits five minutes, then stops the
   backend's whole process group, so `npx` children do not outlive it.

Sockets live in a directory only you can read (`$XDG_RUNTIME_DIR/mcp-stdio-mux`
or `/tmp/mcp-stdio-mux-<uid>`), and a Unix socket cannot be reached from another
machine.

## Limits

- macOS and Linux. Windows is not supported.
- A shared backend inherits environment variables the config does not declare
  from the session that started it. Declare anything that differs.
- A server that keeps per-client state but whose tool names do not show it must
  be marked `session`.
- When a backend crashes, its in-flight requests fail and are not replayed; the
  next session start launches a new backend.
- An app that dies without being reaped by its parent keeps its session until
  its relay is killed.

## Development

Runtime code is Python 3.11+ standard library only.

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
ruff format --check src tests scripts && ruff check src tests scripts
```

## License

MIT
