# Changelog

## 0.1.2 (10 November 2026)

- `status` shows small memory totals with one decimal; two fresh relays read "0 MB" before.
- README: badges, the kinds of servers it handles, and a link to the release post.

## 0.1.1 (10 November 2026)

- `setup` never writes a uvx throwaway path into configs, wherever the uv cache lives. With `UV_CACHE_DIR` or `XDG_CACHE_HOME` outside a `.cache` directory, 0.1.0 could point every server at `<cache>/archive-v0/…`, which breaks once uv prunes its cache.
- The release job fails when the tag does not name the package version.

## 0.1.0 (10 November 2026)

First release: one shared process per stdio MCP server across sessions, apps and profiles; one-command `setup` with backups, `undo` and `status`; an agent skill for Claude Code and Codex.
