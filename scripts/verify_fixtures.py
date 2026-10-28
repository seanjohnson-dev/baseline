#!/usr/bin/env python3
"""Run the tool repeatedly against the research fixtures and score it against ground truth.

usage: verify_fixtures.py --fixtures DIR --truth DIR [--path-prepend DIRS] [--trials N] [--eco py,js,go]
                          [--modes branch,dirty] [--cache] [-- extra check args]

DIR must be a COPY of the fixture repos (the tool writes .git/<name>/). Every trial also asserts that
`git status` and `git worktree list` are unchanged afterwards.
FLAKY truth counts as correct when the test is reported FLAKY or not reported at all (it passed in the
first head run, so there was nothing to attribute); it must never be NEW.
"""
import argparse
import collections
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = next(p for p in (ROOT / "bin").iterdir() if p.is_file())

EXPECT = {"NEW": {"new"}, "NEW_COLLECT": {"new"}, "PRE": {"pre_existing"},
          "PRE_CHANGED": {"pre_existing_error_changed"}, "FLAKY": {"flaky", None}, "ENV": {"environment"},
          "PASS": {None}}


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, text=True).stdout


def labels(rep, truth):
    by_id = {v["id"]: v for v in rep.get("results", [])}
    out = {}
    for key in truth:
        v = by_id.get(key)
        if v is None and "::" in key:  # go: test inside a package that failed to build
            pkg = key.split("::")[0]
            pv = by_id.get(pkg)
            if pv is not None and key in pv.get("blocked_tests", []):
                v = pv
        lab = v["verdict"] if v else None
        if v is None and key in rep.get("fixed", []):
            lab = "fixed"
        out[key] = lab
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--eco", default="py,js,go")
    ap.add_argument("--modes", default="branch,dirty")
    ap.add_argument("--cache", action="store_true", help="allow the base cache (default: --no-cache)")
    ap.add_argument("--auto", action="store_true", help="let the tool auto-detect the test command")
    ap.add_argument("--path-prepend", default="", help="dirs (os.pathsep-separated) to put first on PATH")
    ap.add_argument("extra", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    extra = [x for x in a.extra if x != "--"]
    truth_dir = Path(a.truth)
    cmds = json.loads((truth_dir / "commands.json").read_text())
    env = dict(os.environ)
    if a.path_prepend:
        env["PATH"] = a.path_prepend + os.pathsep + env["PATH"]
    env.pop("DATABASE_URL", None)
    total_bad = 0
    false_new_flaky = 0
    for eco in a.eco.split(","):
        truth = json.loads((truth_dir / f"{eco}.json").read_text())
        for mode in a.modes.split(","):
            repo = Path(a.fixtures) / (f"{eco}-shop" + ("-dirty" if mode == "dirty" else ""))
            before = (git(repo, "status", "--porcelain", "--untracked-files=all"), git(repo, "worktree", "list"))
            tally = collections.defaultdict(collections.Counter)
            times, runs = [], []
            for t in range(a.trials):
                argv = [sys.executable, str(LAUNCHER), "check", "--json"] + ([] if a.cache else ["--no-cache"]) \
                    + extra + ([] if a.auto else ["--", *cmds[eco].split()])
                t0 = time.time()
                p = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=900)
                times.append(time.time() - t0)
                try:
                    rep = json.loads(p.stdout)
                except ValueError:
                    print(f"!! {eco} {mode} trial {t}: no JSON (exit {p.returncode})\n{p.stderr[-2000:]}")
                    total_bad += 1
                    continue
                runs.append(rep.get("test_runs", 0))
                labs = labels(rep, truth)
                for k, lab in labs.items():
                    tally[k][lab] += 1
                    if truth[k] == "FLAKY" and lab == "new":
                        false_new_flaky += 1
                after = (git(repo, "status", "--porcelain", "--untracked-files=all"), git(repo, "worktree", "list"))
                if after != before:
                    print(f"!! {eco} {mode} trial {t}: git state changed\n{before}\n{after}")
                    total_bad += 1
            times.sort()
            print(f"== {eco} {mode}: trials={a.trials} median {times[len(times) // 2]:.2f}s "
                  f"max {times[-1]:.2f}s, median test runs {sorted(runs)[len(runs) // 2] if runs else 0}")
            for k, case in truth.items():
                bad = sum(n for lab, n in tally[k].items() if lab not in EXPECT[case])
                total_bad += bad
                mark = "ok " if not bad else "BAD"
                print(f"  {mark} {case:12} {k:55} {dict(tally[k])}")
    print(f"\nwrong verdicts: {total_bad}; false NEW on flaky tests: {false_new_flaky}")
    return 1 if total_bad or false_new_flaky else 0


if __name__ == "__main__":
    sys.exit(main())
