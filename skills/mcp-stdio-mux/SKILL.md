---
name: mcp-stdio-mux
description: Use when the user wants MCP servers to stop piling up across agent sessions, asks to share or dedupe MCP server processes to save memory, added a new stdio MCP server after mcp-stdio-mux setup, sees an MCP server fail after setup, or wants to undo mcp-stdio-mux.
---

# mcp-stdio-mux

`mcp-stdio-mux` makes every session of every agent app (Claude Code, Claude
Desktop, Codex, Gemini CLI, Cursor, Windsurf, OpenCode) share one process per
stdio MCP server instead of starting its own copy. It rewrites each server's
command in the user's own config files, with backups and a full undo.

## Share the user's servers

1. Look before changing anything:

   ```bash
   mcp-stdio-mux status          # or: uvx mcp-stdio-mux status
   mcp-stdio-mux setup --dry-run
   ```

2. Show the user the plan: which apps, profiles, and how many servers. Say that
   profiles with different names never share (unless mapped in the config), and
   that remote (HTTP) servers and project `.mcp.json` files are not touched.
3. Apply only after the user agrees:

   ```bash
   mcp-stdio-mux setup --yes
   ```

   If `mcp-stdio-mux` is not on PATH, `uvx mcp-stdio-mux setup --yes` installs it
   as a tool first, so the configs point at a path that stays.
4. Tell the user to restart agent sessions; open sessions keep their old servers.
5. After the restart, `mcp-stdio-mux status` lists each running backend and how
   many sessions it serves.

Never wrap or unwrap a server by editing a config file by hand; re-run `setup`.

## When the user adds a server later

Run `mcp-stdio-mux setup --dry-run`, show the one new line, then `setup --yes`.
A second `setup` changes nothing that is already shared.

## When a server misbehaves

- Find its daemon in `mcp-stdio-mux status`; its log is
  `<runtime dir>/<key>.log` (runtime dir: `$MCP_STDIO_MUX_RUNTIME_DIR`, else
  `$XDG_RUNTIME_DIR/mcp-stdio-mux`, else `/tmp/mcp-stdio-mux-<uid>`).
- A server that keeps per-client state (a browser, a login) must not be shared.
  The mux detects most of them from their tool names. For one it misses, add to
  `~/.config/mcp-stdio-mux/config.toml`:

  ```toml
  [servers]
  my-browser = "session"    # one backend per session
  # other scopes: "global", "workspace" (one per git root), "native" (leave alone)
  ```

  Keys are the server name or the executable name. `native` takes effect after
  `setup` runs again; the other scopes apply at the next session start.
- To let two differently named profiles share (for example `work` and `default`):

  ```toml
  [profiles]
  work = "default"
  ```

  then run `setup` again.

## Undo

```bash
mcp-stdio-mux undo --yes
```

It restores each file's exact bytes when the app has not written to it since
setup, and removes only the mux wrapper when it has. It also removes this skill.
