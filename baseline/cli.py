"""Command line: `check`, `detect`, `version`.

Exit codes: 0 no NEW failures, 1 at least one NEW failure, 2 tool/usage error,
3 base could not be established (never to be read as "pre-existing").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, brand

EXIT_OK, EXIT_NEW, EXIT_USAGE, EXIT_BASE = 0, 1, 2, 3


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(brand.ENV_PREFIX + name, default))
    except ValueError:
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=brand.CLI_NAME, description="Per failing test: did this change break it, "
                                "or was it already broken (or flaky / environment)?")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="run the tests and attribute every failure",
                       usage=f"{brand.CLI_NAME} check [options] [-- <test command>]")
    c.add_argument("--base", help="commit/ref to compare against (default: merge-base of HEAD with the default "
                                  "branch; HEAD when on the default branch)")
    c.add_argument("--runs", type=int, default=_env_int("RUNS", 5),
                   help="max runs per failing test on head, the first full run counting (default 5)")
    c.add_argument("--base-runs", type=int, default=None,
                   help="runs on base behind a deterministic verdict (default 2x --runs)")
    c.add_argument("--max-runs", type=int, default=None,
                   help="per-side cap for suspicious verdicts (default 3x --runs)")
    c.add_argument("--json", action="store_true", help="print only JSON to stdout")
    c.add_argument("--format", choices=("text", "json", "both"), default=None)
    c.add_argument("--no-cache", action="store_true", help="don't read or write the base-result cache")
    c.add_argument("--keep-worktree", action="store_true", help="leave the base worktree on disk")
    c.add_argument("--timeout", type=float, default=float(_env_int("TIMEOUT", 900)),
                   help="seconds per test-command invocation (default 900)")
    c.add_argument("--cwd", help="run as if started in this directory")
    c.add_argument("--strict-changed", action="store_true",
                   help="exit 1 for PRE-EXISTING (error changed) too")
    c.add_argument("--copy-ignored", action="append", default=[], metavar="GLOB",
                   help="copy matching untracked/ignored files (e.g. '.env') into the base worktree; "
                        "repeatable (nothing is copied by default)")
    c.add_argument("--full-base", choices=("auto", "always", "never"), default="auto",
                   help="also run the full suite once on base for fixed/missing lists (auto: if the head "
                        "run took <= 60s)")
    c.add_argument("--serial", action="store_true", default=_env_flag("SERIAL"),
                   help=f"never run head and base at the same time (also {brand.ENV_PREFIX}SERIAL=1)")
    c.add_argument("--flaky-hunt", type=int, default=0, metavar="N",
                   help="N extra full runs on head; tests that flip between runs are reported FLAKY")
    c.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    c.add_argument("command", nargs=argparse.REMAINDER, help="test command after --")
    d = sub.add_parser("detect", help="print the detected test runner and command")
    d.add_argument("--cwd")
    d.add_argument("--json", action="store_true")
    d.add_argument("command", nargs=argparse.REMAINDER)
    k = sub.add_parser("clean", help="list and remove temp worktrees/dirs left behind by killed runs")
    k.add_argument("--dry-run", action="store_true", help="only list them")
    k.add_argument("--json", action="store_true")
    h = sub.add_parser("hook", help="Claude Code hook entry point (reads the hook JSON on stdin)")
    h.add_argument("event", choices=("post-tool-failure",))
    sub.add_parser("version", help="print the version")
    return p


def _env_flag(name: str) -> bool:
    return os.environ.get(brand.ENV_PREFIX + name, "") not in ("", "0", "false", "no", "off")


def cmd_clean(a) -> int:
    from . import worktree
    stale = worktree.find_stale()
    rows = []
    for s in stale:
        ok = None if a.dry_run else worktree.remove_stale(s)
        rows.append({"path": str(s.path), "reason": s.reason, "repo": s.repo or s.git_dir, "removed": ok})
    if a.json:
        print(json.dumps({"stale": rows, "dry_run": a.dry_run}))
        return EXIT_OK
    if not rows:
        print(f"{brand.CLI_NAME}: no stale temp worktrees/dirs")
    for r in rows:
        state = "would remove" if a.dry_run else ("removed" if r["removed"] else "could not remove")
        print(f"{state}: {r['path']}  ({r['reason']}{'; repo ' + r['repo'] if r['repo'] else ''})")
    return EXIT_OK


def _command(rest: List[str]) -> Optional[List[str]]:
    if rest and rest[0] == "--":
        rest = rest[1:]
    return rest or None


def cmd_detect(a) -> int:
    from .engine import UsageError, detect
    from .runners import auto_detect, make_runner
    cwd = Path(a.cwd or os.getcwd()).resolve()
    command = _command(a.command)
    try:
        det = detect(cwd, command)
    except UsageError as e:
        print(f"{brand.CLI_NAME}: {e}", file=sys.stderr)
        return EXIT_USAGE
    runner = make_runner(det, cwd)
    others = [x.runner for x in auto_detect(cwd)] if not command else []
    if a.json:
        print(json.dumps({"runner": det.runner, "command": det.argv, "source": det.source,
                          "per_test": runner.per_test, "candidates": others}))
    else:
        print(f"{det.runner}: {runner.display_command()}  ({det.source})")
        if len(others) > 1:
            print(f"also found: {', '.join(others[1:])} (pass a command after -- to choose)")
    return EXIT_OK


def cmd_check(a) -> int:
    from . import gitutil, report
    from .engine import BaseError, Engine, Options, UsageError
    fmt = "json" if a.json else (a.format or "text")
    opts = Options(command=_command(a.command), base=a.base, runs=a.runs, base_runs=a.base_runs,
                   max_runs=a.max_runs, no_cache=a.no_cache, keep_worktree=a.keep_worktree, timeout=a.timeout,
                   cwd=a.cwd, strict_changed=a.strict_changed, copy_ignored=a.copy_ignored,
                   full_base=a.full_base, verbose=a.verbose or (fmt == "text" and sys.stderr.isatty()),
                   serial=a.serial, flaky_hunt=a.flaky_hunt)
    if opts.runs < 1 or opts.flaky_hunt < 0:
        print(f"{brand.CLI_NAME}: --runs must be >= 1 and --flaky-hunt >= 0", file=sys.stderr)
        return EXIT_USAGE
    eng = Engine(opts)
    try:
        rep = eng.run()
    except UsageError as e:
        _error(fmt, str(e), EXIT_USAGE)
        return EXIT_USAGE
    except BaseError as e:
        _error(fmt, f"could not establish the base result: {e}", EXIT_BASE)
        return EXIT_BASE
    except gitutil.GitError as e:
        _error(fmt, str(e), EXIT_USAGE)
        return EXIT_USAGE
    json_path = ""
    try:
        state = gitutil.git_dir(eng.root) / brand.CACHE_DIR_NAME
        state.mkdir(parents=True, exist_ok=True)
        jp = state / "last.json"
        jp.write_text(json.dumps(rep, indent=1))
        json_path = os.path.relpath(jp, Path(opts.cwd or os.getcwd()).resolve())
    except OSError:
        pass
    if fmt in ("text", "both"):
        print(report.render(rep, json_path))
    if fmt in ("json", "both"):
        print(json.dumps(rep, indent=1))
    return rep["exit_code"]


def _error(fmt: str, msg: str, code: int) -> None:
    if fmt == "json":
        print(json.dumps({"tool": brand.NAME, "version": __version__, "exit_code": code, "error": msg}))
    print(f"{brand.CLI_NAME}: error: {msg}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hook"]:
        # fires on every failing Bash command: skip argparse and the engine imports unless needed
        from . import hook
        return hook.main(argv[1:])
    parser = build_parser()
    if not argv:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    # everything after a bare `--` is the test command, even if it looks like our options
    rest: List[str] = []
    if "--" in argv:
        k = argv.index("--")
        argv, rest = argv[:k], argv[k + 1:]
    try:
        a = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    if a.cmd is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if a.cmd == "version":
        print(f"{brand.NAME} {__version__}")
        return EXIT_OK
    if hasattr(a, "command"):
        a.command = (a.command or []) + rest
        if a.command and a.command[0] == "--":
            a.command = a.command[1:]
    from . import procs
    procs.install_signal_handlers()
    try:
        if a.cmd == "detect":
            return cmd_detect(a)
        if a.cmd == "clean":
            return cmd_clean(a)
        return cmd_check(a)
    except KeyboardInterrupt:
        procs.kill_all()
        print(f"{brand.CLI_NAME}: interrupted", file=sys.stderr)
        return 130
    except procs.Interrupted as e:
        procs.kill_all()
        print(f"{brand.CLI_NAME}: terminated by signal {e.signum}", file=sys.stderr)
        return 128 + e.signum
