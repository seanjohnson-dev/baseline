"""Orchestration: head run, reruns, base worktree runs, cache, verdicts."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, brand, closure, decide, envclass, gitutil, normalize, procs, pyenv
from .cache import BaseCache, env_fingerprint
from .model import (ABSENT, BLOCKED, COMMAND, FAIL, LOAD_ERROR, NEW, PASS, PRE_CHANGED, SKIP, TEST, UNKNOWN, Obs,
                    RunResult, Unit)
from .runners import Detection, Side, auto_detect, from_command, make_runner
from .runners.base import Runner
from .worktree import BaseWorktree, missing_ignored, prune_stale

FULL_BASE_MAX_SECONDS = 60.0
# optional base confirmation runs may add this share of the elapsed time (but at least CONFIRM_MIN_SECONDS)
CONFIRM_SHARE = 0.25
CONFIRM_MIN_SECONDS = 1.0
SESSION_UNIT = "(test session)"


class UsageError(Exception):
    """Exit code 2."""


class BaseError(Exception):
    """Exit code 3: the base result could not be established."""


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
        return from_command(command, cwd)
    found = auto_detect(cwd)
    if not found:
        raise UsageError("could not detect a test runner here (looked for pytest, vitest, jest, go, cargo, rspec); "
                         f"pass the command explicitly: {brand.CLI_NAME} check -- <test command>")
    return found[0]


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

    # ------------------------------------------------------------------ helpers
    def _execute(self, invs, side: Side) -> RunResult:
        total = RunResult()
        first = True
        for inv in invs:
            env = dict(side.env)
            env.update(inv.env)
            side.outdir.mkdir(parents=True, exist_ok=True)
            if inv.report is not None and inv.report.exists():
                inv.report.unlink()
            proc = procs.run(inv.argv, str(side.cwd), env=env, timeout=self.opts.timeout,
                             merge_stderr=bool(inv.meta.get("merge_stderr")))
            if proc.not_found:
                r = RunResult(ok=False, problem=f"command not found: {inv.argv[0]}", exit_code=127,
                              duration=proc.duration, output_tail=proc.stderr)
                r.invocations.append(inv.argv)
                r.not_found = True  # type: ignore[attr-defined]
            else:
                r = self.runner.parse(inv, proc, side)
            self.test_runs += sum(1 for t in r.tests.values() if t.status in (PASS, FAIL)) + len(r.load_errors)
            if first:
                total = r
                first = False
            else:
                total.merge(r)
            if getattr(r, "not_found", False):
                total.not_found = True  # type: ignore[attr-defined]
        return total

    def _obs_for(self, u: Unit, r: RunResult) -> Obs:
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
        cache_dir = gitutil.common_git_dir(self.root) / brand.CACHE_DIR_NAME / "cache"
        removed = prune_stale(self.root)
        if removed:
            self.notes.append(f"removed {len(removed)} stale base worktree(s) from an earlier interrupted run")
        outdir = Path(tempfile.mkdtemp(prefix=brand.TMP_PREFIX + "run-"))
        self.outdir = outdir
        report: Dict = {}
        try:
            self._setup_env()
            self.cache = BaseCache(cache_dir, self.base.sha, self._cache_key(), enabled=not o.no_cache)
            report = self._check()
        finally:
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
                               copy_ignored=self.opts.copy_ignored, project_rel=self.project_rel)
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

    # ------------------------------------------------------------------ the algorithm
    def _check(self) -> Dict:
        o = self.opts
        p = self.progress
        p(f"running {self.runner.name} on head (working tree)")
        r1 = self._execute(self.runner.full(self.head, "head-full"), self.head)
        if getattr(r1, "not_found", False):
            raise UsageError(f"test command not found: {self.runner.argv[0]}")
        if not r1.ok and "timed out" in r1.problem:
            raise UsageError(f"the test command timed out on head ({r1.problem}); raise --timeout")
        self.head_full = r1
        units = self._failing_units(r1)
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
            st.closure_changed = self._closure(st.unit)

        # head reruns (early stop on first pass)
        rnd = 0
        while True:
            need = [s for s in states.values() if decide.needs_head(s, self.policy) > 0]
            if not need:
                break
            rnd += 1
            self._head_round(need, rnd)

        # base: cache first, then (optionally) one full run, then targeted rounds
        base_full = self._base_full(states, r1)
        self._prefill(states, base_full)
        self.fixed_states = self._fixed_candidates(r1, base_full)
        brnd = 0
        confirm_spent = 0.0
        confirm_budget: Optional[float] = None
        try:
            while True:
                pol = self.policy
                bneed = [s for s in states.values() if decide.needs_base(s, pol) > 0]
                hneed = [s for s in states.values() if decide.needs_head(s, pol) > 0]
                bneed += [s for s in self.fixed_states if decide.needs_base_fixed(s, pol) > 0]
                hneed += [s for s in self.fixed_states if decide.needs_head_fixed(s, pol) > 0]
                if bneed or hneed:
                    if bneed:
                        # optional confirmation runs ride along for free in a round that happens anyway
                        ride = [s for s in states.values() if s not in bneed and decide.wants_base(s, pol) > 0]
                        brnd += 1
                        self._base_round(bneed + ride, brnd)
                    if hneed:
                        rnd += 1
                        self._head_round(hneed, rnd)
                    continue
                bwant = [s for s in states.values() if decide.wants_base(s, pol) > 0]
                if not bwant:
                    break
                if confirm_budget is None:
                    confirm_budget = max(CONFIRM_MIN_SECONDS, CONFIRM_SHARE * (time.time() - self.t_start))
                avg = sum(self.base_round_times) / len(self.base_round_times) if self.base_round_times else 0.0
                if confirm_spent + avg > confirm_budget:
                    self.confirm_limited = True
                    break
                t = time.time()
                brnd += 1
                self._base_round(bwant, brnd)
                confirm_spent += time.time() - t
        except BaseError as e:
            self._fail_base(states, str(e))
        self._fill_blocked(states)
        self.cache.save()
        return self._report(list(states.values()), r1, base_full)

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
                out.append(s)
        return out

    def _head_round(self, need: List[decide.UnitState], rnd: int) -> None:
        self.progress(f"head rerun {rnd}: {len(need)} unit(s)")
        r = self._execute(self.runner.subset([s.unit for s in need], self.head, f"head-r{rnd}"), self.head)
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

    def _base_full(self, states, r1: RunResult) -> Optional[Dict]:
        """One full base run (cached per base sha) gives fixed/missing/blocked lists."""
        mode = self.opts.full_base
        if mode == "never" or not self.runner.per_test:
            return None
        if mode == "auto" and r1.duration > FULL_BASE_MAX_SECONDS:
            self.notes.append(f"head run took {r1.duration:.0f}s, so only the failing tests were run on base "
                              f"(no fixed/missing lists; use --full-base always)")
            return None
        cached = self.cache.full
        if cached is not None:
            self.progress("base full run: from cache")
            return cached
        # skip the full run if every unit is flaky on head already (nothing to attribute)
        if all(_head_flaky(s) for s in states.values()):
            return None
        try:
            side = self._ensure_base()
        except BaseError as e:
            self._fail_base(states, str(e))
            return None
        self.progress("running full suite on base")
        rb = self._execute(self.runner.full(side, "base-full"), side)
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
            return None
        full = {"tests": {tid: t.status for tid, t in rb.tests.items()},
                "groups": {tid: t.group for tid, t in rb.tests.items()},
                "obs": {}, "load_errors": {k: v.obs().to_dict() for k, v in rb.load_errors.items()},
                "ok": rb.ok, "problem": rb.problem, "output_tail": rb.output_tail[-2000:]}
        for tid, t in rb.tests.items():
            if t.status == FAIL:
                full["obs"][tid] = t.obs().to_dict()
        self.cache.set_full(full)
        self._full_fresh = True
        return full

    def _obs_from_full(self, u: Unit, full: Dict) -> Obs:
        if u.id == SESSION_UNIT:
            if not full.get("ok", True):
                return Obs(FAIL, normalize.normalize("\n".join(full.get("output_tail", "").splitlines()[-5:]),
                                                     self._side_roots()), full.get("problem", ""),
                           full.get("output_tail", ""))
            return Obs(PASS)
        les = full.get("load_errors", {})
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
        if u.id == SESSION_UNIT or u.kind == COMMAND:
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

    def _base_round(self, need: List[decide.UnitState], rnd: int) -> None:
        side = self._ensure_base()
        run_units = []
        for s in need:
            self._presence(s)
            absent = (s.unit.kind == LOAD_ERROR and s.exists_on_base is False) or \
                (s.unit.kind == TEST and (s.exists_on_base is False or s.mentioned_on_base is False)
                 and self.runner.name in ("pytest", "go", "rspec"))
            if absent:
                # don't ask the runner for a test the base doesn't have (pytest errors out on unknown ids)
                s.base.append(Obs(ABSENT))
                s.base_attempts += 1
                continue
            run_units.append(s)
        if not run_units:
            return
        self.progress(f"base run {rnd}: {len(run_units)} unit(s)")
        t = time.time()
        r = self._execute(self.runner.subset([s.unit for s in run_units], side, f"base-r{rnd}"), side)
        self.base_round_times.append(time.time() - t)
        problem = ""
        if getattr(r, "not_found", False) or (not r.ok and SESSION_UNIT not in {s.unit.id for s in run_units}):
            problem = f"base run failed: {r.problem}"
        elif r.ok and not r.tests and not r.load_errors and any(s.exists_on_base for s in run_units) \
                and self.runner.per_test:
            problem = "the base rerun produced no results"
        for tid, t in r.tests.items():
            self.base_seen.setdefault(t.group, {})[tid] = t.status
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
            if self.runner.name in ("vitest", "jest"):
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
            kind = COMMAND if not self.runner.per_test else TEST
            units.append(Unit(t.id, kind, t.group, t.name, dict(t.locator)))
        return units

    # ------------------------------------------------------------------ output model
    def _report(self, states: List[decide.UnitState], r1: RunResult, base_full: Optional[Dict]) -> Dict:
        results = []
        for s in states:
            v = decide.decide(s)
            v["suspicious_escalated"] = decide.suspicious(s)
            results.append(v)
        order = {NEW: 0, PRE_CHANGED: 1, UNKNOWN: 2, "pre_existing": 3, "environment": 4, "flaky": 5}
        results.sort(key=lambda v: (order.get(v["verdict"], 9), v["id"]))
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
        if self.base_side is not None and any(v["verdict"] == UNKNOWN for v in results):
            miss = missing_ignored(self.root, self.base_side.root)
            if miss:
                self.notes.append("ignored files in your tree that the base worktree lacks: " + ", ".join(miss) +
                                  f" (use --copy-ignored GLOB if tests need them)")
        counts = {k: 0 for k in (NEW, "pre_existing", PRE_CHANGED, "flaky", "environment", UNKNOWN)}
        for v in results:
            counts[v["verdict"]] += 1
        tests = list(r1.tests.values())
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
                        "fixed": len(fixed), "missing": len(missing)},
            "base": {"sha": self.base.sha, "short": gitutil.short(self.base.sha), "ref": self.base.ref,
                     "how": self.base.how},
            "head": {"sha": self.head_sha, "short": gitutil.short(self.head_sha), "dirty": self.dirty},
            "runner": {"name": self.runner.name, "command": self.runner.argv, "per_test": self.runner.per_test,
                       "cwd": self.project_rel},
            "policy": {"head_runs": self.policy.runs, "base_required": self.policy.base_required,
                       "base_confirm": self.policy.base_runs, "max_runs": self.policy.max_runs,
                       "confirm_budget_hit": self.confirm_limited},
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
