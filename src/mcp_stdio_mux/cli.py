"""Command line: apps call `run` (and it calls `daemon`); people call the rest."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from mcp_stdio_mux import __version__


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else ""
    try:
        if command == "run":
            from mcp_stdio_mux.runtime import run

            return run(arguments[1:])
        if command == "daemon":
            from mcp_stdio_mux.daemon import run_daemon

            return run_daemon(arguments[1:])
        options = _parser().parse_args(arguments)
        return options.handler(options)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"mcp-stdio-mux: {error}", file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-stdio-mux",
        description="One shared process per MCP server across every agent "
        "session, app and profile on this machine.",
        epilog="Apps call `mcp-stdio-mux run`; setup writes those commands.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="share every stdio MCP server")
    setup.add_argument("-y", "--yes", action="store_true", help="do not ask")
    setup.add_argument("--dry-run", action="store_true", help="only show the plan")
    setup.add_argument("--no-skill", action="store_true", help="skip SKILL.md")
    setup.add_argument("--exe", help="mux path to write (default: installed copy)")
    setup.set_defaults(handler=_setup)
    undo = commands.add_parser("undo", help="put every config back")
    undo.add_argument("-y", "--yes", action="store_true", help="do not ask")
    undo.set_defaults(handler=_undo)
    status = commands.add_parser("status", help="show configs and running backends")
    status.set_defaults(handler=_status)
    return parser


def _setup(options: argparse.Namespace) -> int:
    from mcp_stdio_mux import install

    exe = options.exe or install.find_executable(os.environ)
    context = _context(exe or "mcp-stdio-mux")
    planned = install.plan(context)
    if not planned:
        print("No supported agent app config was found. Nothing to do.")
        return 0
    apps = len({item.config.app for item in planned})
    print(f"mcp-stdio-mux {__version__}: {apps} apps, {len(planned)} config files\n")
    for item in planned:
        print(_row(item.config, item.error or _summary(item.edit, planning=True)))
    to_share = sum(item.changes for item in planned)
    domains = sorted({item.domain for item in planned})
    print(f"\n{to_share} servers will run through {exe or 'a newly installed mux'}.")
    print(f"Backends are shared only inside a domain: {', '.join(domains)}.")
    if exe is None:
        print("First, install this version as a tool (uv or pipx) so the path stays.")
    running = install.running_agents()
    if running:
        print(
            f"Running now: {', '.join(running)}. Open sessions keep their current "
            "servers until restarted."
        )
    if options.dry_run or not _confirm(options.yes, "Apply?"):
        return 0
    if exe is None:
        context = _context(install.install_persistently())
    applied = install.apply(context)
    written = [item for item in applied if item.written]
    skills = (
        []
        if options.no_skill
        else install.install_skills(applied, install.skill_text())
    )
    failed = [item for item in applied if item.error]
    print(
        f"\nUpdated {len(written)} files. Backups: {_tilde(context.state_dir)}/backups/"
    )
    if skills:
        print(f"Installed the agent skill in {len(skills)} skill folders.")
    for item in failed:
        print(_row(item.config, f"not changed: {item.error}"))
    print("Restart agent sessions to use the shared servers.")
    print("Check: mcp-stdio-mux status    Undo: mcp-stdio-mux undo")
    return 1 if failed else 0


def _undo(options: argparse.Namespace) -> int:
    from mcp_stdio_mux import install

    if not _confirm(options.yes, "Put every config back the way it was?"):
        return 0
    undone = install.undo(_context("mcp-stdio-mux"))
    for config, how in undone:
        print(_row(config, how))
    print(f"Done: {len(undone)} files. Running backends exit once their sessions end.")
    return 0


def _status(options: argparse.Namespace) -> int:
    from mcp_stdio_mux.clients import EditError, detect, edit
    from mcp_stdio_mux.config import load_config

    context = _context("mcp-stdio-mux")
    config = load_config(os.environ)
    unshared = 0
    print("Configs")
    for found in detect(context.home, os.environ, context.platform):
        try:
            result = edit(found, found.path.read_text(), lambda server: server.command)
        except (OSError, UnicodeDecodeError, EditError) as error:
            print(_row(found, f"cannot read: {error}"))
            continue
        counts = _counts(result, config)
        unshared += counts["not shared"]
        print(_row(found, _format(counts)))
    if unshared:
        print(f"{unshared} servers are not shared yet: run `mcp-stdio-mux setup`.")
    print()
    _print_backends()
    return 0


def _print_backends() -> None:
    from mcp_stdio_mux.runtime import RELAY_PREFIX, runtime_root

    backends = []
    for meta in sorted(runtime_root().glob("*.json")):
        try:
            data = json.loads(meta.read_text())
            os.kill(int(data["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # a daemon that died without cleaning up
        backends.append(data)
    if not backends:
        print("No shared backends are running.")
        return
    table = _process_table()
    group_rss = Counter()
    for _pid, group, rss, _args in table:
        group_rss[group] += rss
    print(f"{'SERVER':<24}{'DOMAIN':<12}{'SCOPE':<11}{'SESSIONS':>9}{'MEMORY':>10}")
    for data in sorted(backends, key=lambda item: item["server"]):
        memory = megabytes(group_rss.get(data.get("backend_pid"), 0))
        print(
            f"{data['server'][:23]:<24}{data['domain'][:11]:<12}{data['scope']:<11}"
            f"{data['clients']:>9}{memory:>10}"
        )
    relays = [rss for _, _, rss, args in table if args.startswith(RELAY_PREFIX)]
    print(
        f"{len(relays)} session relays use {megabytes(sum(relays))} in total "
        "(resident memory, as reported by ps)."
    )


def megabytes(kilobytes: int) -> str:
    """Whole megabytes, with one decimal below 10 MB so small totals are not 0."""
    if kilobytes == 0 or kilobytes >= 10 * 1024:
        return f"{kilobytes // 1024} MB"
    return f"{kilobytes / 1024:.1f} MB"


def _process_table() -> list[tuple[int, int, int, str]]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,rss=,args="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    rows = []
    for line in output.splitlines():
        fields = line.split(None, 3)
        if len(fields) == 4 and all(field.isdigit() for field in fields[:3]):
            rows.append((int(fields[0]), int(fields[1]), int(fields[2]), fields[3]))
    return rows


def _counts(result, config) -> Counter:
    from mcp_stdio_mux.clients import is_wrapped

    counts: Counter = Counter({"not shared": 0})
    for item in result.results:
        if item.status == "remote":
            counts["remote"] += 1
        elif item.status.startswith("unsupported"):
            counts["unsupported"] += 1
        elif is_wrapped(item.command):
            counts["shared"] += 1
        elif config.scope(item.command, item.name) == "native":
            counts["native"] += 1
        else:
            counts["not shared"] += 1
    return counts


def _summary(result, planning: bool) -> str:
    from mcp_stdio_mux.clients import is_wrapped

    counts: Counter = Counter()
    for item in result.results:
        if item.status == "changed":
            counts["to share" if planning else "shared"] += 1
        elif item.status == "unchanged":
            counts["shared" if is_wrapped(item.command) else "native"] += 1
        else:
            counts[item.status.split(":")[0]] += 1
    return _format(counts) or "no MCP servers"


def _format(counts: Counter) -> str:
    order = ("to share", "shared", "not shared", "native", "remote", "unsupported")
    return " · ".join(f"{counts[key]} {key}" for key in order if counts[key])


def _row(config, detail: str) -> str:
    return f"  {config.app:<15}{config.profile:<12}{_tilde(config.path):<46} {detail}"


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


def _context(exe: str):
    from mcp_stdio_mux.install import Context

    return Context(Path.home(), dict(os.environ), exe)


def _confirm(yes: bool, question: str) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise RuntimeError("no terminal to confirm on; re-run with --yes")
    return input(f"\n{question} [y/N] ").strip().lower() in ("y", "yes")
