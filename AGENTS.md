# Contributor Contract

Rules for people and coding agents changing this repository.

## Code

- Runtime code is Python 3.11+ standard library only; add no runtime dependency.
- Supported platforms are macOS and Linux (Unix sockets, `fcntl`, `/bin/cat`).
- Write the failing test before the change for any behaviour or bug fix, using
  `unittest`. Integration tests use temporary runtime and config directories;
  never point a test at a real user's config.
- Format and lint with Ruff: `ruff format src tests scripts` and
  `ruff check src tests scripts`.
- Keep the one-way imports: `cli` → `install` → `clients` → `config`, and
  `cli` → `daemon` → `runtime` → `config`; `router` imports nothing local.

## Safety

- `setup` may only change user-level config files of supported apps, and only
  the `command`/`args` of local servers. Anything else is a design change.
- Every write keeps a backup, is atomic, and must survive `undo`.
- Never log environment values, JSON-RPC payloads, or credentials.

## Git

- Conventional Commits in English.
- Branch from `sandbox`; open pull requests against `sandbox`. Only the owner
  merges to `main`, with a merge commit so `main` and `sandbox` keep one history.
- A merge to `main` is a release: `release.yml` publishes to PyPI when
  `__version__` is new, then tags it and opens a GitHub release from
  `CHANGELOG.md`. To release, bump `__version__` and add the changelog section
  in the pull request to `sandbox`; never push version tags by hand.
