"""Orchestration: head run, reruns, base worktree runs, cache, verdicts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, brand, closure, decide, envclass, gitutil, normalize, procs, pyenv
from .cache import BaseCache, env_fingerprint
from .model import (ABSENT, BLOCKED, CHECK, COMMAND, FAIL, FLAKY, LOAD_ERROR, NEW, PASS, PRE_CHANGED, SKIP, TEST,
                    UNKNOWN, Obs, RunResult, Unit)
from .runners import Detection, Side, auto_detect, from_command, make_runner, missing_npm_script
from .runners.base import Runner
from .runners.checks import WHOLE
from .worktree import BaseWorktree, cleanup_stale, make_tmp_dir, missing_ignored

FULL_BASE_MAX_SECONDS = 60.0
# the full base run starts alongside head's full run unless the last head run of this command took longer
SPECULATE_MAX_SECONDS = 30.0
# optional base confirmation runs may add this share of the elapsed time (but at least CONFIRM_MIN_SECONDS)
CONFIRM_SHARE = 0.25
CONFIRM_MIN_SECONDS = 1.0
SESSION_UNIT = "(test session)"
# git-ignored generated type files copied into the base worktree for type checks (see Engine._auto_copy)
AUTO_COPY = (".next/types", "next-env.d.ts")

# Signs that two runs at once got in each other's way (ports, sqlite/file locks, busy files). Seen in either
# side of a concurrent round, that round is discarded and redone serially. False alarms only cost time.
COLLISION = re.compile(
    r"EADDRINUSE|address already in use|address in use|\[Errno (?:98|48)\]|port \d+ is (?:already )?in use|"
    r"port is already allocated|already in use|database is locked|database table is locked|SQLITE_BUSY|"
    r"could not obtain lock|unable to (?:acquire|obtain|get) (?:a |the )?lock|lock (?:is )?(?:held|busy)|"
    r"\block(?:file)? already|resource temporarily unavailable|EAGAIN|EBUSY|resource busy|text file busy|ETXTBSY|"
    r"EEXIST|file exists|deadlock detected|too many clients", re.I)


class UsageError(Exception):
    """Exit code 2."""


class BaseError(Exception):
    """Exit code 3: the base result could not be established."""


class BudgetExceeded(Exception):
    """Options.deadline passed: no verdict may be claimed."""


class _Cancelled(Exception):
    """The lane this run belonged to was cancelled (e.g. a speculative base run that isn't needed)."""


class _Async:
    """fn() in a daemon thread; wait() returns its result or re-raises its exception."""

    def __init__(self, fn):
        self.result = None
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn) -> None:
        try:
            self.result = fn()
        except BaseException as e:  # handed to the waiting thread
            self.error = e

    def wait(self):
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.result


def collision_sign(r: Optional[RunResult]) -> str:
    """The first resource-collision sign in a run's output or failure texts ('' if none)."""
    if r is None:
        return ""
    parts = [r.output_tail, r.problem]
    for t in r.tests.values():
        if t.status == FAIL:
            parts += [t.message, t.raw]
    for le in r.load_errors.values():
        parts += [le.message, le.raw]
    m = COLLISION.search("\n".join(p for p in parts if p))
    return m.group(0) if m else ""


class TimingHint:
    """Duration of the last head full run per command (decides whether to speculate on base)."""

    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]

    def _load(self) -> Dict:
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self) -> Optional[float]:
        v = self._load().get(self.key)
        return float(v) if isinstance(v, (int, float)) else None

    def put(self, seconds: float) -> None:
        d = self._load()
        d[self.key] = round(seconds, 2)
        if len(d) > 50:
            d = dict(list(d.items())[-50:])
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)
        except OSError:
            pass


@dataclass
class Options:
    command: Optional[List[str]] = None
    base: Optional[str] = None
    runs: int = 5
    base_runs: Optional[int] = None
    max_runs: Optional[int] = None
    no_cache: bool = False
    keep_worktree: bool = False
    timeout: float = 900.0
    cwd: Optional[str] = None
    strict_changed: bool = False
    copy_ignored: List[str] = field(default_factory=list)
    full_base: str = "auto"       # auto | always | never
    verbose: bool = False
    serial: bool = False          # never run head and base at the same time
    flaky_hunt: int = 0           # extra full head runs to catch tests that flip
    deadline: Optional[float] = None  # absolute time.time(); past it -> BudgetExceeded (no verdict)


class Progress:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.t0 = time.time()

    def __call__(self, msg: str) -> None:
        if self.enabled:
            sys.stderr.write(f"[{brand.CLI_NAME} {time.time() - self.t0:5.1f}s] {msg}\n")
            sys.stderr.flush()


def detect(cwd: Path, command: Optional[List[str]]) -> Detection:
    if command:
        missing = missing_npm_script(command if len(command) > 1 else command[0].split(), cwd)
        if missing:
            have = sorted((_scripts(cwd) or {}).keys())
            raise UsageError(f"no such npm script: '{missing}' (package.json scripts: "
                             f"{', '.join(have) if have else 'none'}); nothing to check")
        return from_command(command, cwd)
    found = auto_detect(cwd)
    if not found:
        raise UsageError("could not detect a test runner here (looked for pytest, vitest, jest, node:test, go, cargo, "
                         "rspec); "
                         f"pass the command explicitly: {brand.CLI_NAME} check -- <test command>")
    return found[0]


def _scripts(cwd: Path) -> Optional[Dict]:
    try:
        return json.loads((cwd / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return None


class Engine:
    def __init__(self, opts: Options):
        self.opts = opts
        self.policy = decide.Policy.from_runs(opts.runs, opts.base_runs, opts.max_runs)
        self.progress = Progress(opts.verbose)
        self.warnings: List[str] = []
        self.notes: List[str] = []
        self.test_runs = 0
        self.wt: Optional[BaseWorktree] = None
        self.base_side: Optional[Side] = None
        self.base_seen: Dict[str, Dict[str, str]] = {}   # group -> {test id: status} seen on base
        self.base_problem_global = ""
        self._full_fresh = False
        self.base_round_times: List[float] = []
        self.confirm_limited = False
        self.t_start = time.time()
        self.serial = opts.serial or os.environ.get(brand.ENV_PREFIX + "SERIAL", "") not in ("", "0")
        self.collision = ""
        self.overlap_saved = 0.0   # seconds that running head and base concurrently saved
        self._last_overlap = 0.0
        self.base_lane = procs.Lane("base")
        self._tasks: List[_Async] = []
        self._lock = threading.Lock()
        # every observation per side and test id, from all runs made anyway (to surface flaky tests)
        self.seen: Dict[str, Dict[str, list]] = {"head": {}, "base": {}}
        self.base_full: Optional[Dict] = None
        self.fixed_states: List[decide.UnitState] = []
        self.full_wanted = False
        self._full_used = False

    # ------------------------------------------------------------------ helpers
    def _timeout(self) -> float:
        t = self.opts.timeout
        if self.opts.deadline is not None:
            left = self.opts.deadline - time.time()
            if left <= 0:
                raise BudgetExceeded("the time budget ran out")
            t = min(t, left)
        return t

    def _execute(self, invs, side: Side, lane: Optional[procs.Lane] = None, progress: str = "") -> RunResult:
        total = RunResult()
        first = True
        if progress:
            self.progress(progress)
        for inv in invs:
            env = dict(side.env)
            env.update(inv.env)
            side.outdir.mkdir(parents=True, exist_ok=True)
            if inv.report is not None and inv.report.exists():
                inv.report.unlink()
            proc = procs.run(inv.argv, str(side.cwd), env=env, timeout=self._timeout(),
                             merge_stderr=bool(inv.meta.get("merge_stderr")), lane=lane)
            if proc.cancelled:
                raise _Cancelled()
            if proc.timed_out and self.opts.deadline is not None and time.time() >= self.opts.deadline - 1:
                raise BudgetExceeded("the time budget ran out")
            if proc.not_found:
                r = RunResult(ok=False, problem=f"command not found: {inv.argv[0]}", exit_code=127,
                              duration=proc.duration, output_tail=proc.stderr)
                r.invocations.append(inv.argv)
                r.not_found = True  # type: ignore[attr-defined]
            else:
                r = self.runner.parse(inv, proc, side)
            with self._lock:
                if getattr(self.runner, "deterministic", False) or not self.runner.per_test:
                    self.test_runs += 1  # whole-command runs: count runs, not units
                else:
                    self.test_runs += sum(1 for t in r.tests.values() if t.status in (PASS, FAIL)) + \
                        len(r.load_errors)
            if first:
                total = r
                first = False
            else:
                total.merge(r)
            if getattr(r, "not_found", False):
                total.not_found = True  # type: ignore[attr-defined]
        return total

    def _obs_for(self, u: Unit, r: RunResult) -> Obs:
        if u.kind == CHECK:
            t = r.tests.get(u.id)
            if t is not None:
                return t.obs()
            whole = r.tests.get(WHOLE)
            # the run's errors were read and this one is not among them: on this side it 'passes'
            return Obs(PASS) if r.ok and whole is not None and whole.status == PASS else Obs(ABSENT)
        if u.kind == COMMAND or u.id == SESSION_UNIT:
            if u.id == SESSION_UNIT:
                if not r.ok:
                    return _crash_obs(r, self._side_roots(), self.head.env_ctx())
                return Obs(PASS)
            t = r.tests.get(u.id)
            return t.obs() if t else Obs(ABSENT)
        if u.kind == LOAD_ERROR:
            le = r.load_errors.get(u.id)
            if le is not None:
                return le.obs()
            whole = u.group.startswith("(")  # whole-build failure (e.g. a crate that doesn't compile)
            if any(whole or t.group == u.group for t in r.tests.values()):
                return Obs(PASS)
            return Obs(ABSENT)
        t = r.tests.get(u.id)
        if t is not None:
            return t.obs()
        le = r.load_errors.get(u.group)
        if le is not None:
            o = le.obs()
            o.status = BLOCKED
            o.key = "[file failed to load] " + o.key
            return o
        return Obs(ABSENT)

    def _side_roots(self) -> List[str]:
        roots = [str(self.root)]
        if self.wt is not None and self.wt.path is not None:
            roots.append(str(self.wt.path))
        return roots

    # ------------------------------------------------------------------ main
    def run(self) -> Dict:
        t0 = time.time()
        o = self.opts
        cwd = Path(o.cwd or os.getcwd()).resolve()
        if not cwd.is_dir():
            raise UsageError(f"--cwd {cwd} is not a directory")
        try:
            self.root = gitutil.repo_root(cwd)
        except gitutil.GitError as e:
            raise UsageError(str(e))
        self.project_rel = os.path.relpath(cwd, self.root)
        self.cwd = cwd
        det = detect(cwd, o.command)
        if det.note:
            self.notes.append(det.note)
        self.runner: Runner = make_runner(det, cwd)
        try:
            self.base = gitutil.resolve_base(self.root, o.base)
        except gitutil.GitError as e:
            if o.base:
                raise UsageError(str(e))
            raise BaseError(str(e))
        if self.base.warning:
            self.warnings.append(self.base.warning)
        self.head_sha = gitutil.head_sha(self.root) or ""
        self.dirty = gitutil.is_dirty(self.root)
        self.changed = set(gitutil.changed_files(self.root, self.base.sha))
        gdir = gitutil.git_dir(self.root)
        self.state_dir = gdir / brand.CACHE_DIR_NAME
        common = gitutil.common_git_dir(self.root)
        cache_dir = common / brand.CACHE_DIR_NAME / "cache"
        removed = cleanup_stale(repo=self.root)
        if removed:
            self.notes.append(f"removed {len(removed)} stale temp dir(s)/worktree(s) left by earlier killed runs")
        outdir = make_tmp_dir("run-", common)
        self.outdir = outdir
        report: Dict = {}
        try:
            self._setup_env()
            key = self._cache_key()
            self.cache = BaseCache(cache_dir, self.base.sha, key, enabled=not o.no_cache)
            self.timing = TimingHint(common / brand.CACHE_DIR_NAME / "timing.json",
                                     self.runner.cache_key() + "\0" + self.project_rel)
            report = self._check()
        finally:
            self._cancel_base()
            if self.wt is not None:
                self.wt.remove()
                self.notes.extend(self.wt.notes)
            shutil.rmtree(outdir, ignore_errors=True)
        report["duration_s"] = round(time.time() - t0, 2)
        return report

    def _setup_env(self) -> None:
        env = dict(os.environ)
        self.head_env = dict(env)
        self.base_env = dict(env)
        self.editable: List[str] = []
        if self.runner.name == "pytest":
            self.editable = pyenv.editable_dirs(self.root, env)
            if self.editable:
                self.notes.append("editable install of this repo detected (" + ", ".join(self.editable) +
                                  "); each side imports its own code via PYTHONPATH")
                self.head_env["PYTHONPATH"] = pyenv.pythonpath_for(self.root, self.root, self.editable,
                                                                   env.get("PYTHONPATH"))
            if not env.get("PYTHONPYCACHEPREFIX") and not env.get("PYTHONDONTWRITEBYTECODE"):
                # keep __pycache__ out of the working tree (the base worktree is temporary: no need)
                self.head_env["PYTHONPYCACHEPREFIX"] = str(self.state_dir / "pycache")
        self.head = Side("head", self.root, self.cwd, self.outdir / "head", self.head_env)

    def _cache_key(self) -> str:
        extra = []
        for d in {self.root, self.cwd}:
            for f in ("node_modules/.package-lock.json", "node_modules/.modules.yaml", "node_modules/.yarn-state.yml"):
                extra.append(d / f)
        if self.runner.name == "pytest":
            extra += pyenv.site_dirs(self.base_env)
        fp = env_fingerprint(os.environ, extra, ignore_prefix=brand.ENV_PREFIX)
        return "\0".join([__version__, self.runner.cache_key(), self.project_rel, fp, ",".join(self.editable),
                          ",".join(self.opts.copy_ignored)])

    def _ensure_base(self) -> Side:
        if self.base_side is not None:
            return self.base_side
        self.progress(f"creating base worktree at {gitutil.short(self.base.sha)}")
        self.wt = BaseWorktree(self.root, self.base.sha, keep=self.opts.keep_worktree,
                               copy_ignored=self.opts.copy_ignored + self._auto_copy(), project_rel=self.project_rel)
        try:
            wt_path = self.wt.create()
        except gitutil.GitError as e:
            self.wt = None
            raise BaseError(f"could not create a worktree for base {gitutil.short(self.base.sha)}: {e}")
        if self.wt.copied:
            self.notes.append("copied into base worktree: " + ", ".join(self.wt.copied))
        env = dict(self.base_env)
        if self.runner.name == "pytest":
            pp = pyenv.pythonpath_for(wt_path, self.root, self.editable, env.get("PYTHONPATH"))
            if pp:
                env["PYTHONPATH"] = pp
        bcwd = (wt_path / self.project_rel).resolve() if self.project_rel != "." else wt_path
        if not bcwd.is_dir():
            raise BaseError(f"directory {self.project_rel} does not exist at base {gitutil.short(self.base.sha)}")
        self.base_side = Side("base", wt_path, bcwd, self.outdir / "base", env, [str(self.root)])
        self.head.other_roots = [str(wt_path)]
        return self.base_side

    def _auto_copy(self) -> List[str]:
        """Generated type files a type check needs but a fresh checkout lacks (e.g. `.next/types`, made by
        `next build`). Only for check commands, only when git-ignored. Build output (dist/, build/) is never
        copied: on base it would be head's code and could hide what the change broke."""
        if not getattr(self.runner, "deterministic", False) or self.runner.name not in ("tsc", "next-build"):
            return []
        out = []
        for name in AUTO_COPY:
            rel = name if self.project_rel == "." else os.path.join(self.project_rel, name)
            if (self.root / rel).exists() and gitutil.try_git(["check-ignore", "-q", rel], self.root) is not None:
                out.append(rel)
        return out

    # ------------------------------------------------------------------ the algorithm
    def _check(self) -> Dict:
        o = self.opts
        p = self.progress
        spec = self._start_speculation()
        try:
            r1 = self._head_full()
            units = self._failing_units(r1)
            r1, units, spec_out = self._settle_speculation(spec, r1, units)
            hunts = self._flaky_hunt(r1)
        except BaseException:
            self._cancel_base()
            raise
        self.head_full = r1
        self.timing.put(r1.duration)
        p(f"head: {r1.collected} tests, {len(units)} failing unit(s)")
        if not units:
            if r1.exit_code not in (0, 5) and self.runner.per_test:
                self.warnings.append(f"no failing tests in the report, but the command exited with "
                                     f"{r1.exit_code} (coverage threshold, warnings-as-errors, ...?)")
            return self._report([], r1, None)
        states = {u.id: decide.UnitState(u) for u in units}
        for st in states.values():
            st.head.append(self._obs_for(st.unit, r1))
            st.head_attempts = 1
            for rh in hunts:
                st.head.append(self._obs_for(st.unit, rh))
                st.head_attempts += 1
            st.closure_changed = self._closure(st.unit)

        self.full_wanted = self._full_wanted(r1)
        full_pending = False
        try:
            if spec_out is not None:
                kind, val = spec_out
                if kind == "error":
                    raise val
                self._apply_base_full(states, val)
            elif self.full_wanted and self.cache.full is not None:
                self.progress("base full run: from cache")
                self._use_full(states, self.cache.full, fresh=False)
            elif self.full_wanted:
                full_pending = True
            else:
                self._use_full(states, None, fresh=False)
        except BaseError as e:
            self._fail_base(states, str(e))
            self._use_full(states, None, fresh=False)

        # Rounds. Each iteration has at most one head round and one base task (the full base run or a
        # targeted round); concurrently unless serial, else base first, then head (stage-1 order).
        self.rnd = self.brnd = 0
        confirm_spent = 0.0
        confirm_budget: Optional[float] = None
        base_dead = bool(self.base_problem_global)
        while True:
            try:
                pol = self.policy
                hneed = [s for s in states.values() if decide.needs_head(s, pol) > 0]
                hneed += [s for s in self.fixed_states if decide.needs_head_fixed(s, pol) > 0]
                btask = None
                if base_dead:
                    pass  # no base any more; head reruns still finish (flaky-on-head beats everything)
                elif full_pending:
                    if self.serial and hneed:
                        pass  # serial: head reruns first, so an all-flaky head can skip the base run
                    elif self.serial and all(_head_flaky(s) for s in states.values()):
                        full_pending = False
                        self._use_full(states, None, fresh=False)
                        continue
                    else:
                        btask = ("full", [])
                else:
                    bneed = [s for s in states.values() if decide.needs_base(s, pol) > 0]
                    bneed += [s for s in self.fixed_states if decide.needs_base_fixed(s, pol) > 0]
                    if bneed:
                        # optional confirmation runs ride along for free in a round that happens anyway
                        ride = [s for s in states.values() if s not in bneed and decide.wants_base(s, pol) > 0]
                        btask = ("round", bneed + ride)
                if hneed or btask:
                    if btask and btask[0] == "full":
                        full_pending = False
                    self._iteration(states, hneed, btask)
                    continue
                bwant = [s for s in states.values() if decide.wants_base(s, pol) > 0] if not base_dead else []
                if not bwant:
                    break
                if confirm_budget is None:
                    # measured against the time a serial check would have taken by now, so running head and
                    # base concurrently never buys fewer confirmation runs than stage 1's serial order did
                    serial_elapsed = time.time() - self.t_start + self.overlap_saved
                    confirm_budget = max(CONFIRM_MIN_SECONDS, CONFIRM_SHARE * serial_elapsed)
                avg = sum(self.base_round_times) / len(self.base_round_times) if self.base_round_times else 0.0
                if confirm_spent + avg > confirm_budget:
                    self.confirm_limited = True
                    break
                t = time.time()
                self._iteration(states, [], ("round", bwant))
                confirm_spent += time.time() - t
            except BaseError as e:
                self._fail_base(states, str(e))
                full_pending = False
                if not self._full_used:
                    self._use_full(states, None, fresh=False)
                base_dead = True
        self._fill_blocked(states)
        self.cache.save()
        return self._report(list(states.values()), r1, self.base_full)

    def _head_full(self, tag: str = "head-full") -> RunResult:
        self.progress(f"running {self.runner.name} on head (working tree)")
        r1 = self._execute(self.runner.full(self.head, tag), self.head)
        if getattr(r1, "not_found", False):
            raise UsageError(f"test command not found: {self.runner.argv[0]}")
        if not r1.ok and "timed out" in r1.problem:
            raise UsageError(f"the test command timed out on head ({r1.problem}); raise --timeout")
        return r1

    def _flaky_hunt(self, r1: RunResult) -> List[RunResult]:
        """--flaky-hunt N: N extra full head runs; tests that flip between them are reported FLAKY."""
        self._record("head", r1)
        out = []
        for i in range(max(0, self.opts.flaky_hunt)):
            rh = self._head_full(f"head-hunt{i + 1}")
            if not rh.ok:
                break
            self._record("head", rh)
            out.append(rh)
        return out

    # -- concurrency ------------------------------------------------------------------
    def _full_wanted(self, r1: RunResult) -> bool:
        mode = self.opts.full_base
        if mode == "never" or not self.runner.per_test:
            return False
        if mode == "auto" and r1.duration > FULL_BASE_MAX_SECONDS:
            self.notes.append(f"head run took {r1.duration:.0f}s, so only the failing tests were run on base "
                              f"(no fixed/missing lists; use --full-base always)")
            return False
        return True

    def _start_speculation(self) -> Optional["_Async"]:
        """Start the full base run while head's full run is still going (it is needed in nearly every
        check with a failure). Not when serial, when the base result is cached, or when the last head
        run of this command was slow (two full suites at once would mostly fight for the CPU)."""
        o = self.opts
        if self.serial or o.full_base == "never" or not self.runner.per_test or self.cache.full is not None:
            return None
        hint = self.timing.get()
        if hint is not None and (hint > SPECULATE_MAX_SECONDS or (o.full_base == "auto" and
                                                                    hint > FULL_BASE_MAX_SECONDS)):
            return None
        self.progress("starting the full base run alongside head")
        task = _Async(self._exec_base_full)
        self._tasks.append(task)
        return task

    def _settle_speculation(self, spec: Optional["_Async"], r1: RunResult, units: List[Unit]):
        """-> (r1, units, None | ("ok", base RunResult) | ("error", BaseError))."""
        if spec is None:
            return r1, units, None
        too_slow = self.opts.full_base == "auto" and r1.duration > FULL_BASE_MAX_SECONDS
        if not units or too_slow:
            self._cancel_base()
            return r1, units, None
        try:
            rb = spec.wait()
            out = ("ok", rb)
        except BaseError as e:
            out = ("error", e)
        except _Cancelled:
            out = None
        self._tasks.remove(spec)
        sign = collision_sign(r1) or (collision_sign(out[1]) if out and out[0] == "ok" else "")
        if sign:
            self._go_serial(sign)
            r1 = self._head_full()
            return r1, self._failing_units(r1), None
        if out and out[0] == "ok":
            self.overlap_saved += min(r1.duration, out[1].duration)
        return r1, units, out

    def _go_serial(self, sign: str) -> None:
        self.serial = True
        self.collision = sign
        self.notes.append(f"head and base ran concurrently and one of them showed a resource-collision sign "
                          f"({sign!r}); that round was discarded and redone serially, and the rest of this "
                          f"check ran serially (--serial forces this)")
        self.progress(f"collision sign {sign!r}: going serial")

    def _cancel_base(self) -> None:
        """Kill whatever runs on the base lane and wait for its threads (before the worktree goes)."""
        self.base_lane.cancel()
        for t in self._tasks:
            t.thread.join(timeout=30)
        self._tasks = []
        self.base_lane = procs.Lane("base")

    def _iteration(self, states, hneed: List[decide.UnitState], btask) -> None:
        head_fn = base_fn = None
        if hneed:
            self.rnd += 1
            rnd = self.rnd
            head_fn = lambda: self._execute(self.runner.subset([s.unit for s in hneed], self.head, f"head-r{rnd}"),
                                            self.head, progress=f"head rerun {rnd}: {len(hneed)} unit(s)")
        plan = None
        if btask:
            if btask[0] == "full":
                base_fn = self._exec_base_full
            else:
                self.brnd += 1
                plan = self._plan_base(btask[1])
                brnd = self.brnd
                base_fn = lambda: self._exec_base_round(plan[0], brnd)
        if head_fn and base_fn and not self.serial:
            h, b, berr = self._pair(head_fn, base_fn)
            sign = collision_sign(h) or (collision_sign(b) if berr is None else "")
            if not sign:
                self.overlap_saved += self._last_overlap
                self._apply_head_round(hneed, h)
                if berr is not None:
                    raise berr
                self._apply_base(states, btask, plan, b)
                return
            self._go_serial(sign)
        # serial: base first, then head (the stage-1 order)
        if base_fn:
            self._apply_base(states, btask, plan, base_fn())
        if head_fn:
            self._apply_head_round(hneed, head_fn())

    def _pair(self, head_fn, base_fn):
        """Run head_fn here and base_fn in a worker thread. -> (head result, base result, base BaseError)."""
        task = _Async(base_fn)
        self._tasks.append(task)
        t0 = time.time()
        try:
            h = head_fn()
        except BaseException:
            self._cancel_base()
            raise
        th = time.time() - t0
        try:
            b = task.wait()
        except BaseError as e:
            self._tasks.remove(task)
            self._last_overlap = 0.0
            return h, None, e
        self._tasks.remove(task)  # (on any other exception it stays listed, so cleanup joins it)
        self._last_overlap = min(th, time.time() - t0)
        return h, b, None

    def _apply_base(self, states, btask, plan, result) -> None:
        if btask[0] == "full":
            self._apply_base_full(states, result)
        else:
            self._apply_base_round(plan[0], plan[1], result)

    def _fixed_candidates(self, r1: RunResult, full: Optional[Dict]) -> List[decide.UnitState]:
        if full is None:
            return []
        out = []
        for tid, st in full["tests"].items():
            ht = r1.tests.get(tid)
            if st == FAIL and ht is not None and ht.status == PASS:
                u = Unit(tid, TEST, ht.group, ht.name, dict(ht.locator))
                s = decide.UnitState(u, head=[Obs(PASS)], base=[Obs.from_dict(full["obs"][tid])],
                                     exists_on_base=True, mentioned_on_base=True)
                s.head_attempts = s.base_attempts = 1
                s.closure_changed = self._closure(u)
                out.append(s)
        return out

    def _apply_head_round(self, need: List[decide.UnitState], r: RunResult) -> None:
        self._record("head", r)
        for s in need:
            s.head_attempts += 1
            if not r.ok and s.unit.id != SESSION_UNIT:
                continue  # crashed rerun: no evidence either way
            s.head.append(self._obs_for(s.unit, r))

    def _fail_base(self, states, problem: str) -> None:
        self.base_problem_global = problem
        for s in states.values():
            if not s.base:
                s.base_problem = problem

    def _exec_base_full(self) -> RunResult:
        side = self._ensure_base()
        return self._execute(self.runner.full(side, "base-full"), side, lane=self.base_lane,
                             progress="running full suite on base")

    def _apply_base_full(self, states, rb: RunResult) -> None:
        """One full base run (cached per base sha) gives fixed/missing/blocked lists."""
        r1 = self.head_full
        problem = ""
        if getattr(rb, "not_found", False):
            problem = rb.problem
        elif not rb.ok:
            if SESSION_UNIT in states:
                problem = ""
            else:
                problem = f"base run failed: {rb.problem}"
        elif rb.collected == 0 and not rb.load_errors and r1.collected > 0:
            problem = "the base run collected no tests although head collected some"
        if problem:
            self._fail_base(states, problem)
            self._use_full(states, None, fresh=False)
            return
        full = {"tests": {tid: t.status for tid, t in rb.tests.items()},
                "groups": {tid: t.group for tid, t in rb.tests.items()},
                "obs": {}, "load_errors": {k: v.obs().to_dict() for k, v in rb.load_errors.items()},
                "ok": rb.ok, "problem": rb.problem, "output_tail": rb.output_tail[-2000:]}
        for tid, t in rb.tests.items():
            if t.status == FAIL:
                full["obs"][tid] = t.obs().to_dict()
        self.cache.set_full(full)
        self._use_full(states, full, fresh=True)

    def _use_full(self, states, full: Optional[Dict], fresh: bool) -> None:
        """Adopt the full base result (fresh, cached or none): first base sample, fixed candidates. Once."""
        if self._full_used:
            return
        self._full_used = True
        self._full_fresh = fresh
        self.base_full = full
        if full is not None:
            for tid, st in full.get("tests", {}).items():
                ob = Obs.from_dict(full["obs"][tid]) if st == FAIL and tid in full.get("obs", {}) else Obs(st)
                self._seen_add("base", tid, ob, full.get("groups", {}).get(tid, ""))
        self._prefill(states, full)
        self.fixed_states = self._fixed_candidates(self.head_full, full)

    def _obs_from_full(self, u: Unit, full: Dict) -> Obs:
        if u.id == SESSION_UNIT:
            if not full.get("ok", True):
                return Obs(FAIL, normalize.normalize("\n".join(full.get("output_tail", "").splitlines()[-5:]),
                                                     self._side_roots()), full.get("problem", ""),
                           full.get("output_tail", ""))
            return Obs(PASS)
        les = full.get("load_errors", {})
        if u.kind == CHECK:
            st = full.get("tests", {}).get(u.id)
            if st == FAIL:
                return Obs.from_dict(full["obs"][u.id])
            readable = full.get("ok", True) and full.get("tests", {}).get(WHOLE) == PASS
            return Obs(PASS) if readable else Obs(ABSENT)
        if u.kind == LOAD_ERROR:
            if u.id in les:
                return Obs.from_dict(les[u.id])
            if any(u.group.startswith("(") or g == u.group for g in full.get("groups", {}).values()):
                return Obs(PASS)
            return Obs(ABSENT)
        st = full.get("tests", {}).get(u.id)
        if st == FAIL:
            return Obs.from_dict(full["obs"][u.id])
        if st in (PASS, SKIP):
            return Obs(st)
        if u.group in les:
            ob = Obs.from_dict(les[u.group])
            ob.status = BLOCKED
            ob.key = "[file failed to load] " + ob.key
            return ob
        return Obs(ABSENT)

    def _seen_add(self, side: str, tid: str, ob: Obs, group: str) -> None:
        rec = self.seen[side].get(tid)
        if rec is None:
            rec = self.seen[side][tid] = {"pass": 0, "fail": 0, "fail_obs": None, "group": group}
        if ob.status == PASS:
            rec["pass"] += 1
        elif ob.status == FAIL:
            rec["fail"] += 1
            if rec["fail_obs"] is None:
                rec["fail_obs"] = ob

    def _record(self, side: str, r: RunResult) -> None:
        """Remember every test outcome of a run (also tests we didn't ask for) to surface flips."""
        if not r.ok or not self.runner.per_test:
            return
        for tid, t in r.tests.items():
            self._seen_add(side, tid, t.obs(), t.group)

    def _flips(self, states: List[decide.UnitState]) -> List[Dict]:
        """FLAKY results for tests that are not head failures but flipped in the runs made anyway:
        'fixed' candidates that turned out flaky, and any test that both passed and failed on one side."""
        out = []
        done = {s.unit.id for s in states}
        for fs in self.fixed_states:
            done.add(fs.unit.id)
            if decide.fixed_verdict(fs) != "fixed":
                out.append(decide.flip_result(fs))
        for tid in sorted(set(self.seen["head"]) | set(self.seen["base"])):
            if tid in done:
                continue
            h = self.seen["head"].get(tid)
            b = self.seen["base"].get(tid)
            if h is None or not any(x and x["pass"] and x["fail"] for x in (h, b)):
                continue
            rec = h or b
            st = decide.UnitState(Unit(tid, TEST, rec["group"], ""))
            for side, x in (("head", h), ("base", b)):
                if x:
                    obs = [Obs(PASS)] * x["pass"] + [x["fail_obs"]] * x["fail"]
                    setattr(st, side, obs)
            out.append(decide.flip_result(st))
        return out

    def _prefill(self, states, full: Optional[Dict]) -> None:
        for s in states.values():
            if s.base_problem:
                continue
            if full is not None:
                ob = self._obs_from_full(s.unit, full)
                ob.cached = not getattr(self, "_full_fresh", False)
                s.base.append(ob)
                s.base_attempts += 1
                for tid, g in full.get("groups", {}).items():
                    self.base_seen.setdefault(g, {})[tid] = full["tests"][tid]
            s.base.extend(self.cache.obs(s.unit.id))
            s.base_attempts += len(self.cache.obs(s.unit.id))
            self._presence(s)

    def _presence(self, s: decide.UnitState) -> None:
        """Does the unit's file / test exist at the base commit? (git only, no worktree)."""
        if s.exists_on_base is not None:
            return
        u = s.unit
        if u.id == SESSION_UNIT or u.kind in (COMMAND, CHECK):
            s.exists_on_base = True
            return
        if self.runner.name == "go":
            d = self.runner.pkg_dir(u.group)  # type: ignore[attr-defined]
            if d is None:
                return
            rel = d if self.project_rel == "." else os.path.normpath(os.path.join(self.project_rel, d))
            rel = rel.replace(os.sep, "/")
            files = [f for f in gitutil.ls_tree(self.root, self.base.sha, rel) if f.endswith(".go")]
            s.exists_on_base = bool(files)
            if u.kind == TEST and files:
                s.mentioned_on_base = any(self.runner.source_contains(gitutil.show(self.root, self.base.sha, f) or "",
                                                                      u) for f in files if f.endswith("_test.go"))
            return
        f = u.locator.get("file", u.group)
        if not f or f.startswith("("):
            return
        s.exists_on_base = gitutil.exists_at(self.root, self.base.sha, f)
        if u.kind == TEST and s.exists_on_base:
            text = gitutil.show(self.root, self.base.sha, f) or ""
            s.mentioned_on_base = self.runner.source_contains(text, u)

    def _plan_base(self, need: List[decide.UnitState]):
        """-> (units to run on base, units known to be absent there). Git lookups only; no mutation of
        the evidence."""
        run_units, absent_units = [], []
        for s in need:
            self._presence(s)
            absent = (s.unit.kind == LOAD_ERROR and s.exists_on_base is False) or \
                (s.unit.kind == TEST and (s.exists_on_base is False or s.mentioned_on_base is False)
                 and self.runner.name in ("pytest", "go", "rspec"))
            # don't ask the runner for a test the base doesn't have (pytest errors out on unknown ids)
            (absent_units if absent else run_units).append(s)
        return run_units, absent_units

    def _exec_base_round(self, run_units: List[decide.UnitState], rnd: int) -> Optional[RunResult]:
        if not run_units:
            return None
        side = self._ensure_base()
        t = time.time()
        r = self._execute(self.runner.subset([s.unit for s in run_units], side, f"base-r{rnd}"), side,
                          lane=self.base_lane, progress=f"base run {rnd}: {len(run_units)} unit(s)")
        self.base_round_times.append(time.time() - t)
        return r

    def _apply_base_round(self, run_units: List[decide.UnitState], absent_units: List[decide.UnitState],
                          r: Optional[RunResult]) -> None:
        for s in absent_units:
            s.base.append(Obs(ABSENT))
            s.base_attempts += 1
        if r is None:
            return
        problem = ""
        if getattr(r, "not_found", False) or (not r.ok and SESSION_UNIT not in {s.unit.id for s in run_units}):
            problem = f"base run failed: {r.problem}"
        elif r.ok and not r.tests and not r.load_errors and any(s.exists_on_base for s in run_units) \
                and self.runner.per_test:
            problem = "the base rerun produced no results"
        for tid, t in r.tests.items():
            self.base_seen.setdefault(t.group, {})[tid] = t.status
        if not problem:
            self._record("base", r)
        for s in run_units:
            s.base_attempts += 1
            if problem:
                s.base_problem = problem
                continue
            ob = self._obs_for(s.unit, r)
            s.base.append(ob)
            self.cache.add(s.unit.id, ob)

    def _fill_blocked(self, states) -> None:
        for s in states.values():
            if s.unit.kind == LOAD_ERROR:
                if s.unit.group.startswith("("):
                    s.blocked_tests = sorted({t for seen in self.base_seen.values() for t in seen})
                else:
                    s.blocked_tests = sorted(self.base_seen.get(s.unit.group, {}))

    def _closure(self, u: Unit) -> Optional[bool]:
        try:
            if u.kind != TEST:
                return None
            if self.runner.name == "pytest":
                return closure.python_changed(self.root, u.group, self.changed)
            if self.runner.name in ("vitest", "jest", "node-test"):
                return closure.js_changed(self.root, self.cwd, u.group, self.changed)
            if self.runner.name == "go":
                mod = getattr(self.runner, "module", "")
                return closure.go_changed(self.root, mod, self._go_module_dir(), u.group, self.changed)
        except Exception:  # analysis is best-effort; never let it break a check
            return None
        return None

    def _go_module_dir(self) -> Path:
        cur = self.cwd
        while not (cur / "go.mod").exists() and cur != self.root and cur.parent != cur:
            cur = cur.parent
        return cur

    def _failing_units(self, r1: RunResult) -> List[Unit]:
        units: List[Unit] = []
        if not r1.ok:
            units.append(Unit(SESSION_UNIT, LOAD_ERROR, SESSION_UNIT, SESSION_UNIT))
            return units
        for le in r1.load_errors.values():
            units.append(Unit(le.id, LOAD_ERROR, le.locator.get("file") or le.locator.get("pkg") or le.id,
                              le.id, dict(le.locator)))
        for t in r1.failing_tests():
            kind = t.locator.get("kind") or (COMMAND if not self.runner.per_test else TEST)
            units.append(Unit(t.id, kind, t.group, t.name, dict(t.locator)))
        return units

    # ------------------------------------------------------------------ output model
    def _report(self, states: List[decide.UnitState], r1: RunResult, base_full: Optional[Dict]) -> Dict:
        results = []
        for s in states:
            v = decide.decide(s)
            v["suspicious_escalated"] = decide.suspicious(s)
            results.append(v)
        flips = self._flips(states)
        results += flips
        order = {NEW: 0, PRE_CHANGED: 1, UNKNOWN: 2, "pre_existing": 3, "environment": 4, "flaky": 5}
        results.sort(key=lambda v: (order.get(v["verdict"], 9), bool(v.get("surfaced")), v["id"]))
        head_ids = set(r1.tests)
        head_load = set(r1.load_errors)
        fixed: List[str] = []
        missing: List[str] = []
        flaky_passing: List[str] = []
        for fs in getattr(self, "fixed_states", []):
            (fixed if decide.fixed_verdict(fs) == "fixed" else flaky_passing).append(fs.unit.id)
        whole_build_failed = any(k.startswith("(") for k in head_load) or not r1.ok
        if base_full is not None and not whole_build_failed:
            for tid, st in base_full["tests"].items():
                g = base_full["groups"].get(tid, "")
                if tid not in head_ids and g not in head_load:
                    missing.append(tid)
        is_check = getattr(self.runner, "deterministic", False)
        if is_check and base_full is not None:
            extra = sorted(tid for tid, st in base_full["tests"].items() if st == FAIL and tid not in head_ids)
            if extra:
                self.notes.append(f"base also failed with {len(extra)} error(s) that head does not have (base "
                                  f"failed for a different reason, e.g. generated or ignored files the base "
                                  f"worktree lacks); head's own errors were still compared one by one: "
                                  + "; ".join(normalize.one_line(x)[:160] for x in extra[:3])
                                  + (f" (+{len(extra) - 3} more)" if len(extra) > 3 else ""))
        if self.base_side is not None and (any(v["verdict"] == UNKNOWN for v in results) or
                                           (is_check and base_full is not None and
                                            any(st == FAIL and tid not in head_ids
                                                for tid, st in base_full["tests"].items()))):
            miss = missing_ignored(self.root, self.base_side.root)
            if miss:
                self.notes.append("ignored files in your tree that the base worktree lacks: " + ", ".join(miss) +
                                  f" (use --copy-ignored GLOB if tests need them)")
        counts = {k: 0 for k in (NEW, "pre_existing", PRE_CHANGED, "flaky", "environment", UNKNOWN)}
        for v in results:
            counts[v["verdict"]] += 1
        tests = list(r1.tests.values())
        if is_check:
            tests = []  # the units are errors, not tests
        exit_code = 0
        if counts[NEW] or (self.opts.strict_changed and counts[PRE_CHANGED]):
            exit_code = 1
        elif counts[UNKNOWN]:
            exit_code = 3
        for v in results:
            v["base_sha"] = self.base.sha
            v["head_sha"] = self.head_sha
            v["dirty"] = self.dirty
        return {
            "tool": brand.NAME, "version": __version__, "schema": 1,
            "exit_code": exit_code,
            "summary": {**counts,
                        "tests_total": len(tests),
                        "tests_passed": sum(1 for t in tests if t.status == PASS),
                        "tests_failed": sum(1 for t in tests if t.status == FAIL),
                        "tests_skipped": sum(1 for t in tests if t.status == SKIP),
                        "load_errors": len(r1.load_errors),
                        "check_errors": sum(1 for t in r1.tests.values()
                                            if t.status == FAIL and t.locator.get("kind") == CHECK),
                        "fixed": len(fixed), "missing": len(missing),
                        "flaky_surfaced": len(flips)},
            "base": {"sha": self.base.sha, "short": gitutil.short(self.base.sha), "ref": self.base.ref,
                     "how": self.base.how},
            "head": {"sha": self.head_sha, "short": gitutil.short(self.head_sha), "dirty": self.dirty},
            "runner": {"name": self.runner.name, "command": self.runner.argv, "per_test": self.runner.per_test,
                       "check": is_check, "noun": getattr(self.runner, "noun", ""), "cwd": self.project_rel},
            "policy": {"head_runs": self.policy.runs, "base_required": self.policy.base_required,
                       "base_confirm": self.policy.base_runs, "max_runs": self.policy.max_runs,
                       "confirm_budget_hit": self.confirm_limited, "flaky_hunt": self.opts.flaky_hunt},
            "concurrency": {"serial": self.serial, "forced": bool(self.opts.serial) or self.serial and
                            not self.collision, "collision": self.collision},
            "test_runs": self.test_runs,
            "cache": {"enabled": not self.opts.no_cache, "hit": self.cache.hit},
            "results": results,
            "fixed": sorted(fixed),
            "flaky_passing": sorted(flaky_passing),
            "missing": sorted(missing),
            "warnings": self.warnings + ([f"base: {self.base_problem_global}"] if self.base_problem_global else []),
            "notes": self.notes,
        }


def _head_flaky(st: decide.UnitState) -> bool:
    return any(o.status == PASS for o in st.head)


def _crash_obs(r: RunResult, roots: List[str], ctx) -> Obs:
    tail = [ln for ln in normalize.strip_ansi(r.output_tail).splitlines() if ln.strip()]
    msg = "\n".join(tail[-5:]) or r.problem
    key = normalize.normalize(msg, roots)
    raw = normalize.first_lines("\n".join(tail[-30:]), 30)
    return Obs(FAIL, key, normalize.first_lines(msg, 5), raw, envclass.classify(key, raw, ctx))
