#!/usr/bin/env python3
"""Wall-clock benchmark of `check` on the research fixtures: median/max per ecosystem, mode and variant.

usage: bench_speed.py --fixtures DIR --truth DIR [--trials N] [--eco py,js,go] [--modes branch,dirty]
                      [--variants concurrent,serial,cached] [--launcher PATH] [--path-prepend DIRS]

DIR must be a COPY of the fixture repos (the tool writes .git/<name>/). Variants: `concurrent` (default
behaviour, --no-cache), `serial` (--serial --no-cache), `cached` (base cache on; one warm-up run first).
--launcher runs another checkout's bin/<name> (e.g. a worktree of an older commit) for before/after numbers;
variants that launcher doesn't know (serial) are skipped. Run it on a quiet machine; the load average is
printed with the results.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VARIANTS = {"concurrent": ["--no-cache"], "serial": ["--serial", "--no-cache"], "cached": []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--eco", default="py,js,go")
    ap.add_argument("--modes", default="branch,dirty")
    ap.add_argument("--variants", default="concurrent,serial,cached")
    ap.add_argument("--launcher", default=str(next(p for p in (ROOT / "bin").iterdir() if p.is_file())))
    ap.add_argument("--path-prepend", default="")
    ap.add_argument("--json", help="also write the raw timings here")
    a = ap.parse_args()
    cmds = json.loads((Path(a.truth) / "commands.json").read_text())
    env = dict(os.environ)
    if a.path_prepend:
        env["PATH"] = a.path_prepend + os.pathsep + env["PATH"]
    env.pop("DATABASE_URL", None)
    help_text = subprocess.run([sys.executable, a.launcher, "check", "--help"], capture_output=True, text=True,
                               env=env).stdout
    load0 = os.getloadavg() if hasattr(os, "getloadavg") else None
    rows, raw = [], {}
    for eco in a.eco.split(","):
        for mode in a.modes.split(","):
            repo = Path(a.fixtures) / (f"{eco}-shop" + ("-dirty" if mode == "dirty" else ""))
            for var in a.variants.split(","):
                flags = VARIANTS[var]
                if any(f not in help_text for f in flags if f.startswith("--") and f != "--no-cache"):
                    continue
                argv = [sys.executable, a.launcher, "check", "--json", *flags, "--", *cmds[eco].split()]
                if var == "cached":
                    subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=900)  # warm the cache
                times, runs = [], []
                for _ in range(a.trials):
                    t0 = time.time()
                    p = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=900)
                    times.append(time.time() - t0)
                    try:
                        runs.append(json.loads(p.stdout).get("test_runs", 0))
                    except ValueError:
                        runs.append(-1)
                times.sort()
                raw[f"{eco}/{mode}/{var}"] = times
                rows.append((eco, mode, var, times[len(times) // 2], times[-1], times[0],
                             sorted(runs)[len(runs) // 2]))
                print(f"  {eco:3} {mode:6} {var:10} median {rows[-1][3]:6.2f}s  max {rows[-1][4]:6.2f}s  "
                      f"min {rows[-1][5]:6.2f}s  test runs {rows[-1][6]}", flush=True)
    load1 = os.getloadavg() if hasattr(os, "getloadavg") else None
    print(f"\n{a.trials} trials each; launcher {a.launcher}; load average before {load0}, after {load1}")
    if a.json:
        Path(a.json).write_text(json.dumps({"trials": a.trials, "launcher": a.launcher, "load": [load0, load1],
                                            "times": raw}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
