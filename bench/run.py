#!/usr/bin/env python3
"""Run the benchmark: make the fixtures if needed, run the harness per tool/ecosystem/mode, write the scorecard.

usage: python3 bench/run.py [--tools T1,T2] [--ecosystems py,js,go] [--modes branch,dirty] [--trials N] [--quick]
  --tools       default: this tool only. Rivals: wasitme, fof (flake-or-fault), pp (preexisting-proof);
                they must be cloned under $RIVALS_DIR (default bench/rivals/), see bench/README.md
  --quick       this tool, Python fixtures only, 3 trials (needs only python3, git, pytest); writes to
                bench/_work/quick-results/ unless $BENCH_RESULTS is set, so the published results stay as they are
  --rival-trials N   trials for the rivals (default: --trials)
Results go to $BENCH_RESULTS (default bench/results/); the scorecard is regenerated from every result file there.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
import harness  # noqa: E402  (paths and the tool name come from there)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tools", default=harness.SELF)
    ap.add_argument("--ecosystems", default="py,js,go")
    ap.add_argument("--modes", default="branch,dirty")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--rival-trials", type=int)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-score", action="store_true")
    a = ap.parse_args()
    if a.quick:
        a.tools, a.ecosystems, a.trials = harness.SELF, "py", 3
        os.environ.setdefault("BENCH_RESULTS", str(harness.WORK / "quick-results"))
    tools = [t for t in a.tools.split(",") if t]
    ecos = [e for e in a.ecosystems.split(",") if e]
    modes = [m for m in a.modes.split(",") if m]
    for t in tools:
        if t not in harness.TOOLS:
            raise SystemExit(f"run: unknown tool {t!r} (known: {', '.join(sorted(harness.TOOLS))})")
        if t != harness.SELF and not harness.RIVAL_DIRS[t].is_dir():
            raise SystemExit(f"run: {harness.RIVAL_DIRS[t]} missing; clone it (bench/README.md) or set RIVALS_DIR")
    missing = [e for e in ecos if not (harness.WORK / f"{e}-shop").is_dir() or not (harness.WORK / f"{e}-shop-dirty").is_dir()]
    if missing:
        subprocess.run([sys.executable, str(BENCH / "fixtures" / "make_fixtures.py"), str(harness.WORK), "--ecosystems", ",".join(missing)], check=True)
    for t in tools:
        n = a.trials if t == harness.SELF or a.rival_trials is None else a.rival_trials
        for e in ecos:
            for m in modes:
                subprocess.run([sys.executable, str(BENCH / "harness.py"), t, e, m, "--trials", str(n)], check=True)
    if not a.no_score:
        subprocess.run([sys.executable, str(BENCH / "score.py")], check=True)


if __name__ == "__main__":
    main()
