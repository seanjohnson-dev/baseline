#!/usr/bin/env python3
"""Run one tool against one fixture repo N times and record what it said about every test.

usage: harness.py <tool> <eco> <mode> [--trials N]
  tool: this tool's name (see brand.py) | wasitme | fof (flake-or-fault) | pp (preexisting-proof)
  eco:  py | js | go        mode: branch (change committed, base main) | dirty (change uncommitted, base HEAD)

Writes $BENCH_RESULTS/<tool>-<eco>-<mode>.json (default bench/results/): per-trial labels per ground-truth
key, seconds per trial, the tool's version/commit, `uptime` load averages before/after, and the raw output
of trial 1. Paths: fixtures from $BENCH_WORK (default bench/_work, made by fixtures/make_fixtures.py),
rival clones from $RIVALS_DIR (default bench/rivals/<name>), this tool from bin/<name> (override $BENCH_TOOL_BIN).
"""
import argparse, datetime, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ROOT = BENCH.parent


def _brand_name():
    for p in sorted(ROOT.iterdir()):
        if (p / "brand.py").is_file() and (p / "__init__.py").is_file():
            m = re.search(r'^NAME\s*=\s*"([^"]+)"', (p / "brand.py").read_text(), re.M)
            if m:
                return m.group(1)
    raise SystemExit("harness: brand.py not found")


SELF = _brand_name()
WORK = Path(os.environ.get("BENCH_WORK", BENCH / "_work")).resolve()
RES = Path(os.environ.get("BENCH_RESULTS", BENCH / "results")).resolve()
RIV = Path(os.environ.get("RIVALS_DIR", BENCH / "rivals")).resolve()
SELF_BIN = os.environ.get("BENCH_TOOL_BIN", str(ROOT / "bin" / SELF))
WASITME = RIV / "wasitme/bin/wasitme"
FLAKERUN = RIV / "flake-or-fault/skills/flake-or-fault/scripts/flakerun.py"
PREEX = RIV / "preexisting-proof/scripts/preexisting.py"
RIVAL_DIRS = {"wasitme": RIV / "wasitme", "fof": RIV / "flake-or-fault", "pp": RIV / "preexisting-proof"}
# gotestsum (preexisting-proof on Go) is usually installed to ~/go/bin
ENV = dict(os.environ, PATH=os.path.expanduser("~/go/bin") + os.pathsep + os.environ["PATH"])
ENV.pop("DATABASE_URL", None)  # the ENV cases need it unset

TRUTH = BENCH / "truth"
CMD = json.loads((TRUTH / "commands.json").read_text())


def norm(s): return re.sub(r"[^a-z0-9]", "", s.lower())


# token per truth key, used to match the ids a tool reports
TOK = {
    "py": lambda k: norm("test_report") if k == "tests/test_report.py" else norm(k.split("::")[1]),
    "js": lambda k: norm("report.test") if k == "test/report.test.js" else norm(k.split(" > ")[1]),
    "go": lambda k: norm("shop/report") if k.endswith("TestNewCollectSummary") else norm(k.split("::")[1]),
}


def sh(cmd, cwd, shell=False, timeout=900):
    t = time.time()
    r = subprocess.run(cmd, cwd=cwd, env=ENV, shell=shell, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr, time.time() - t


def match(truth, ids_by_label, eco):
    """ids_by_label: {label: [ids]} -> {truthkey: sorted labels or None}. Go's compile-error case matches on the package path."""
    out = {}
    for k in truth:
        tok = TOK[eco](k); found = []
        for label, ids in ids_by_label.items():
            for i in ids:
                n = norm(i)
                if eco == "go" and k.endswith("TestNewCollectSummary"):
                    ok = n.endswith("shopreport") or "shopreportbuildfailed" in n or n.startswith("examplecomshopreport")
                    if "shopreport" in n and "newcollect" not in n: ok = True
                else:
                    ok = tok in n
                if ok: found.append(label)
        out[k] = sorted(set(found)) or None
    return out


# ---------------- base worktree helpers (the rivals that need a base checkout get one made here)
def base_ref(mode): return "main" if mode == "branch" else "HEAD"


def mk_base(repo, mode, eco, copy_deps=False):
    wt = Path(tempfile.mkdtemp(prefix="bench-base-")) / "wt"
    subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "--detach", str(wt), base_ref(mode)], check=True, env=ENV)
    if (Path(repo) / "node_modules").exists():
        if copy_deps: shutil.copytree(Path(repo) / "node_modules", wt / "node_modules", symlinks=True)
        else: os.symlink(Path(repo) / "node_modules", wt / "node_modules")
    return wt


def rm_base(repo, wt):
    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(wt)], env=ENV, capture_output=True)
    shutil.rmtree(wt.parent, ignore_errors=True)
    subprocess.run(["git", "-C", repo, "worktree", "prune"], env=ENV, capture_output=True)


# ---------------- tools: each returns ({label: [ids]}, raw text, seconds, rc)
def run_wasitme(repo, eco, mode):
    args = [sys.executable, str(WASITME), "--fresh"] + (["--base", "main"] if mode == "branch" else []) + ["--"] + [CMD[eco]]
    rc, out, dt = sh(args, repo)
    lab = {}
    for line in out.splitlines():
        m = re.match(r"- (INTRODUCED|PRE-EXISTING|FIXED)[^:]*: (.*)$", line)
        if m:
            lab[{"INTRODUCED": "NEW", "PRE-EXISTING": "PRE", "FIXED": "FIXED"}[m.group(1)]] = [x.strip() for x in m.group(2).split(", ")]
    return lab, out, dt, rc


def run_fof(repo, eco, mode):
    t0 = time.time(); log = []
    wt = mk_base(repo, mode, eco)
    try:
        if eco == "py":
            cmd = CMD["py"] + " --junitxml=.flake/junit.xml"; extra = ["--results", ".flake/junit.xml"]
        elif eco == "js":
            cmd = "npx vitest run --reporter=default --reporter=junit --outputFile.junit=.flake/junit.xml"; extra = ["--results", ".flake/junit.xml"]
        else:
            cmd = "go test -json -count=1 ./..."; extra = ["--format", "go-json"]
        d = Path(tempfile.mkdtemp(prefix="bench-fof-"))
        for name, cwd, n in (("head", repo, 10), ("base", str(wt), 5)):
            rc, out, _ = sh([sys.executable, str(FLAKERUN), "run", "--cmd", cmd, "--cwd", cwd, "--runs", str(n), "--out", str(d / f"{name}.json")] + extra, repo)
            log.append(out)
        rc, out, _ = sh([sys.executable, str(FLAKERUN), "classify", "--head", str(d / "head.json"), "--base", str(d / "base.json"), "--json"], repo)
        data = json.loads(out)
        lab = {}
        m = {"broken-by-change": "NEW", "pre-existing": "PRE", "flaky": "FLAKY", "order-dependent": "ORDER", "consistent-failure": "CONSISTENT", "inconclusive": "INCONCLUSIVE"}
        for t in data["tests"]:
            if t["verdict"] in m: lab.setdefault(m[t["verdict"]], []).append(t["test"])
        text = "\n".join(log) + "\n" + subprocess.run([sys.executable, str(FLAKERUN), "classify", "--head", str(d / "head.json"), "--base", str(d / "base.json")],
                                                      capture_output=True, text=True).stdout
        shutil.rmtree(d, ignore_errors=True)
    finally:
        rm_base(repo, wt)
        shutil.rmtree(Path(repo) / ".flake", ignore_errors=True)
    return lab, text, time.time() - t0, 0


def run_pp(repo, eco, mode):
    t0 = time.time()
    if eco == "py":
        tcmd = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--continue-on-collection-errors", "--junitxml={junit}"]
    elif eco == "js":
        tcmd = ["npx", "vitest", "run", "--reporter=junit", "--outputFile={junit}"]
    else:
        tcmd = ["gotestsum", "--junitfile", "{junit}", "--", "-count=1", "./..."]
    wt = mk_base(repo, mode, eco, copy_deps=True); d = Path(tempfile.mkdtemp(prefix="bench-pp-")); log = []
    try:
        for name, cwd in (("before", str(wt)), ("after", repo)):
            rc, out, _ = sh([sys.executable, str(PREEX), "capture", "--out", str(d / name), "--cwd", cwd, "--project", "shop", "--scope", "all",
                             "--environment", "local", "--"] + tcmd, repo)
            log.append(f"capture {name}: rc={rc} {out.strip()[:300]}")
        rc, out, _ = sh([sys.executable, str(PREEX), "compare", "--json", str(d / "before"), str(d / "after")], repo)
        try: data = json.loads(out)
        except Exception: return {}, "\n".join(log) + "\n" + out, time.time() - t0, rc
        m = {"new_failure": "NEW", "changed_failure": "CHANGED", "same_failure_observed_before": "PRE", "now_passing": "FIXED",
             "other_missing": "MISSING", "skipped_not_verified": "SKIPPED", "missing_not_verified": "MISSING"}
        lab = {}
        for cat, items in data["categories"].items():
            for it in items: lab.setdefault(m.get(cat, cat), []).append(it["test"] + " " + it.get("message", ""))
        text = "\n".join(log) + "\n" + subprocess.run([sys.executable, str(PREEX), "compare", str(d / "before"), str(d / "after")], capture_output=True, text=True).stdout
        if not data.get("comparable"): text += "\nNOT COMPARABLE: " + json.dumps(data.get("blockers"))
    finally:
        rm_base(repo, wt); shutil.rmtree(d, ignore_errors=True)
    return lab, text, time.time() - t0, 0


def run_self(repo, eco, mode):
    """This tool, explicit command, no cache. Also checks it left the repo exactly as it found it."""
    probe = "git status --porcelain; git worktree list; git branch -a; git stash list"
    before = subprocess.run(probe, shell=True, cwd=repo, capture_output=True, text=True).stdout
    args = [sys.executable, SELF_BIN, "check", "--json", "--no-cache", "--", CMD[eco]]
    rc, out, dt = sh(args, repo)
    after = subprocess.run(probe, shell=True, cwd=repo, capture_output=True, text=True).stdout
    try: data = json.loads(out[out.index("{"):])
    except Exception: return {}, out, dt, rc
    m = {"new": "NEW", "pre_existing": "PRE", "pre_existing_error_changed": "CHANGED", "flaky": "FLAKY", "environment": "ENV", "unknown": "UNKNOWN"}
    lab = {}
    for r in data["results"]:
        lab.setdefault(m.get(r["verdict"], r["verdict"]), []).append(r["id"])
    for r in data.get("fixed", []) or []: lab.setdefault("FIXED", []).append(r["id"] if isinstance(r, dict) else str(r))
    text = json.dumps(data, indent=1)
    if before != after:
        lab["MUTATED"] = ["repo"]; text += "\nREPO MUTATED:\n" + before + "\n---\n" + after
    return lab, text, dt, rc


TOOLS = {SELF: run_self, "wasitme": run_wasitme, "fof": run_fof, "pp": run_pp}


def version(tool):
    if tool == SELF:
        r = subprocess.run([sys.executable, SELF_BIN, "version"], capture_output=True, text=True)
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        return {"version": r.stdout.strip(), "commit": sha}
    d = RIVAL_DIRS[tool]
    sha = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    date = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cs"], capture_output=True, text=True).stdout.strip()
    return {"commit": sha, "commit_date": date}


def load():
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip().split("load average:")[-1].strip()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("tool", choices=sorted(TOOLS)); ap.add_argument("eco", choices=["py", "js", "go"])
    ap.add_argument("mode", choices=["branch", "dirty"]); ap.add_argument("--trials", type=int, default=1)
    a = ap.parse_args()
    truth = json.loads((TRUTH / f"{a.eco}.json").read_text())
    repo = str(WORK / f"{a.eco}-shop{'-dirty' if a.mode == 'dirty' else ''}")
    if not Path(repo).is_dir():
        raise SystemExit(f"harness: {repo} missing; run bench/fixtures/make_fixtures.py first")
    RES.mkdir(parents=True, exist_ok=True)
    started = datetime.datetime.now().astimezone().isoformat(timespec="seconds"); load0 = load()
    trials = []; raw1 = ""; times = []
    for i in range(a.trials):
        lab, raw, dt, rc = TOOLS[a.tool](repo, a.eco, a.mode)
        if i == 0: raw1 = raw
        times.append(round(dt, 2))
        trials.append(match(truth, lab, a.eco))
    out = {"tool": a.tool, "eco": a.eco, "mode": a.mode, "trials": a.trials, "started": started, "load_before": load0, "load_after": load(),
           "tool_version": version(a.tool), "seconds": times, "truth": truth, "labels": trials, "raw_trial1": raw1}
    text = json.dumps(out, indent=1)
    # keep machine-specific paths out of the published files
    for path, alias in ((str(WORK), "<work>"), (str(ROOT), "<repo>"), (str(RIV), "<rivals>"), (os.path.expanduser("~"), "~")):
        text = text.replace(path, alias)
    (RES / f"{a.tool}-{a.eco}-{a.mode}.json").write_text(text)
    print(f"== {a.tool} {a.eco} {a.mode}  trials={a.trials}  median {sorted(times)[len(times) // 2]}s")
    for k, t in truth.items():
        c = Counter(",".join(tr[k]) if tr[k] else "-" for tr in trials)
        print(f"  {t:12} {k:55} {dict(c)}")


if __name__ == "__main__":
    main()
