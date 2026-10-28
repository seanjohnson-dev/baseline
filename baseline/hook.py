"""Claude Code hooks on Bash -> per-failing-test verdicts as additionalContext.

* PostToolUseFailure: the command failed; if it ran tests, attribute the failures.
* PostToolUse: the command "succeeded" but ran tests whose output shows failures — agents usually pipe
  (`pytest | tail -40`) or chain (`pytest; git status`), which hides the test command's exit code.

They fire on every Bash command, so the common path (not a test command) is cheap: stdlib only, no git,
no engine import. For a failing TEST command it runs the check in-process (no shell, no recursion) within
a time budget and prints ONE JSON object:

  {"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "..."}}

Never prints anything else to stdout, never exits non-zero, never modifies the user's tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import brand

EVENT = "PostToolUseFailure"
EVENT_OK = "PostToolUse"
EVENTS = {"post-tool-failure": EVENT, "post-tool-use": EVENT_OK}
MAX_CONTEXT = 3000          # hard cap (Claude Code truncates hook context at 10,000)
DEFAULT_BUDGET = 150.0      # seconds; hooks/hooks.json's timeout must stay above this
DEDUP_SECONDS = 600.0
DEFAULT_MAX_RUNS = 20
LINE_MAX = 230

SEPARATORS = {";", "&&", "||", "&", "\n", "(", ")", ";;", "|&"}
PIPE = "|"
REDIRECT = re.compile(r"^[0-9]*(?:[<>]+&?|&>>?|>\|)$")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PY = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|py|pypy3?)(?:\.exe)?$")
# wrappers that just run the rest of the command line
SIMPLE_WRAPPERS = {"time", "nice", "nohup", "command", "exec", "stdbuf", "ionice", "chronic", "caffeinate",
                   "xvfb-run", "dotenv", "with-contenv"}
RUN_WRAPPERS = {("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("pdm", "run"), ("rye", "run"),
                ("hatch", "run"), ("bundle", "exec"), ("pnpm", "exec"), ("pnpm", "dlx"), ("yarn", "dlx"),
                ("conda", "run"), ("mamba", "run"), ("pixi", "run")}
JS_RUNNERS = {"vitest", "jest", "mocha", "ava", "tap", "uvu", "jasmine", "karma", "playwright", "cypress"}
DIRECT_RUNNERS = {"pytest", "py.test", "rspec", "phpunit", "nosetests", "nose2", "ward", "tox", "nox", "ctest",
                  "pest", "behave"} | JS_RUNNERS
SUBCOMMAND_TESTS = {"go": {"test"}, "cargo": {"test", "nextest"}, "deno": {"test"}, "dotnet": {"test"},
                    "mvn": {"test", "verify"}, "mvnw": {"test", "verify"}, "gradle": {"test", "check"},
                    "gradlew": {"test", "check"}, "mix": {"test"}, "swift": {"test"}, "rake": {"test", "spec"},
                    "zig": {"build"}, "flutter": {"test"}, "dart": {"test"}, "bun": {"test"}, "sbt": {"test"},
                    "lein": {"test"}, "stack": {"test"}, "cabal": {"test"}, "elixir": set(), "make": set()}
MAKE_TARGETS = re.compile(r"^(?:test|tests|check|unit|unittest|spec|specs|test[-_:].+|.+[-_]test|.+[-_]tests)$")
NPM_LIKE = {"npm", "yarn", "pnpm", "bun"}
NPM_TEST_SCRIPT = re.compile(r"^(?:t|tst|test|tests|spec|specs|unit|test[-_:].+|.+[-_:]test|.+[-_:]tests)$")


# ------------------------------------------------------------------------------------- command parsing
@dataclass
class Segment:
    argv: List[str]
    cwd: str
    env: Dict[str, str] = field(default_factory=dict)
    kind: str = ""          # why it is a test command (runner family)

    def display(self) -> str:
        return " ".join(shlex.quote(a) for a in self.argv)


def tokenize(command: str) -> Optional[List[str]]:
    lx = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    lx.commenters = "#"
    try:
        return list(lx)
    except ValueError:  # unbalanced quotes etc.
        return None


def split_segments(tokens: List[str]) -> List[Tuple[List[str], bool]]:
    """-> [(argv, in_pipeline_not_last)] with redirections removed."""
    out: List[Tuple[List[str], bool]] = []
    cur: List[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS or t == PIPE:
            out.append((cur, t == PIPE))
            cur = []
            i += 1
            continue
        if REDIRECT.match(t) and any(c in t for c in "<>"):
            if cur and cur[-1].isdigit():
                cur.pop()  # `2>&1`: the fd number
            i += 2
            continue
        cur.append(t)
        i += 1
    out.append((cur, False))
    return [(a, p) for a, p in out if a]


def _unwrap(argv: List[str], env: Dict[str, str]) -> List[str]:
    """Strip env assignments and wrappers (env, timeout, time, uv run, npx, ...)."""
    a = list(argv)
    for _ in range(12):
        while a and ASSIGN.match(a[0]):
            k, v = a[0].split("=", 1)
            env[k] = v
            a = a[1:]
        if not a:
            return a
        head = os.path.basename(a[0])
        if head == "env":
            a = a[1:]
            while a and (a[0].startswith("-") or ASSIGN.match(a[0])):
                if ASSIGN.match(a[0]):
                    k, v = a[0].split("=", 1)
                    env[k] = v
                elif a[0] in ("-u", "--unset", "-C", "--chdir", "-S") and len(a) > 1:
                    a = a[1:]
                a = a[1:]
            continue
        if head in ("timeout", "gtimeout"):
            a = a[1:]
            while a and a[0].startswith("-"):
                if a[0] in ("-s", "--signal", "-k", "--kill-after") and len(a) > 1:
                    a = a[1:]
                a = a[1:]
            a = a[1:]  # the duration
            continue
        if head in SIMPLE_WRAPPERS:
            a = a[1:]
            while a and a[0].startswith("-"):
                if a[0] in ("-n", "-c", "-o", "-e", "-i") and len(a) > 1 and not a[1].startswith("-"):
                    a = a[1:]
                a = a[1:]
            continue
        if len(a) >= 2 and (head, a[1]) in RUN_WRAPPERS:
            a = a[2:]
            while a and a[0].startswith("-"):
                a = a[1:]
            continue
        if head in ("npx", "bunx", "pnpx"):
            a = a[1:]
            while a and a[0].startswith("-"):
                a = a[1:]
            continue
        break
    return a


def classify(argv: List[str], cwd: str) -> str:
    """Runner family if argv (already unwrapped) runs tests, else ''."""
    if not argv:
        return ""
    head = os.path.basename(argv[0])
    rest = argv[1:]
    if head == brand.CLI_NAME:
        return ""
    if head in DIRECT_RUNNERS or re.fullmatch(r"pytest-?\d+(\.\d+)*", head):
        if head in ("playwright", "cypress") and not (rest and rest[0] in ("test", "run")):
            return ""
        return head
    if PY.match(head):
        args = [x for x in rest if x not in ("-u", "-B", "-X", "-W") and not x.startswith(("-X", "-W"))]
        if len(args) >= 2 and args[0] == "-m":
            mod = args[1]
            if mod in ("pytest", "py.test", "unittest", "nose2", "tox", "nox", "ward"):
                return mod
            return ""
        if args and os.path.basename(args[0]) in ("manage.py", "runtests.py", "runtest.py") and \
                (os.path.basename(args[0]) != "manage.py" or args[1:2] == ["test"]):
            return "django" if args[0].endswith("manage.py") else "script"
        if args and re.fullmatch(r"(?:.*/)?(?:pytest|py\.test)", args[0]):
            return "pytest"
        return ""
    sub = SUBCOMMAND_TESTS.get(head.replace(".bat", "").replace("./", ""))
    if head in ("gradlew", "mvnw") or head.endswith(("/gradlew", "/mvnw")):
        sub = SUBCOMMAND_TESTS["gradlew"]
    if sub is not None:
        if head == "make":
            targets = [x for x in rest if not x.startswith("-") and "=" not in x]
            return "make" if targets and all(MAKE_TARGETS.match(t) for t in targets) else ""
        if head == "zig":
            return "zig" if rest[:2] == ["build", "test"] else ""
        return head if rest and rest[0] in sub else ""
    if head in NPM_LIKE:
        return _npm_kind(head, rest, cwd)
    return ""


def _npm_kind(head: str, rest: List[str], cwd: str) -> str:
    if not rest:
        return ""
    if head == "bun" and rest[0] == "test":
        return "bun"
    if rest[0] in ("t", "tst", "test"):
        return "npm-test"
    script = None
    if rest[0] in ("run", "run-script") and len(rest) > 1:
        script = rest[1]
    elif head in ("yarn", "pnpm", "bun") and not rest[0].startswith("-"):
        if rest[0] in JS_RUNNERS:
            return rest[0]
        script = rest[0]
    if script is None:
        return ""
    if NPM_TEST_SCRIPT.match(script):
        return "npm-test"
    try:
        scripts = json.loads((Path(cwd) / "package.json").read_text()).get("scripts") or {}
        body = scripts.get(script) or ""
    except (OSError, ValueError, AttributeError):
        return ""
    return "npm-test" if any(re.search(r"\b" + r + r"\b", body) for r in JS_RUNNERS | {"pytest"}) else ""


def is_own_cli(argv: List[str]) -> bool:
    if not argv:
        return False
    if os.path.basename(argv[0]) == brand.CLI_NAME:
        return True
    pkg = __name__.rsplit(".", 1)[0]
    if PY.match(os.path.basename(argv[0])) and len(argv) >= 2:
        a1 = argv[1]
        if (a1 == "-m" and len(argv) > 2 and argv[2] == pkg) or os.path.basename(a1) == brand.CLI_NAME:
            return True
    return False


def find_test_segment(command: str, cwd: str) -> Optional[Segment]:
    """The test command inside a shell command line, with its cwd and env prefix; None if there is none
    (or the line runs this tool itself, or the test segment can't be re-run faithfully)."""
    tokens = tokenize(command or "")
    if not tokens or any(t.startswith("<<") for t in tokens):
        return None  # a heredoc body would be read as commands
    cur = cwd
    exported: Dict[str, str] = {}
    found: Optional[Segment] = None
    for argv, _piped in split_segments(tokens):
        env: Dict[str, str] = {}
        a = _unwrap(argv, env)
        if is_own_cli(a) or is_own_cli(argv):
            return None
        if not a:
            exported.update(env)  # bare `FOO=1` assignment
            continue
        head = os.path.basename(a[0])
        if head in ("cd", "pushd") and found is None:
            target = a[1] if len(a) > 1 else "~"
            if target == "-":
                return None
            target = os.path.expanduser(os.path.expandvars(target))
            if "$" in target:
                return None
            cur = os.path.normpath(os.path.join(cur, target))
            continue
        if head == "export" and found is None:
            for x in a[1:]:
                if ASSIGN.match(x):
                    k, v = x.split("=", 1)
                    exported[k] = v
            continue
        if head in ("source", ".") and len(a) > 1 and found is None:
            act = os.path.normpath(os.path.join(cur, os.path.expanduser(a[1])))
            if os.path.basename(act) == "activate" and os.path.basename(os.path.dirname(act)) in ("bin", "Scripts"):
                venv = os.path.dirname(os.path.dirname(act))
                exported["VIRTUAL_ENV"] = venv
                exported["PATH"] = os.path.join(venv, os.path.basename(os.path.dirname(act))) + os.pathsep + \
                    exported.get("PATH", os.environ.get("PATH", ""))
            continue
        if found is not None:
            continue
        kind = classify(a, cur)
        if not kind:
            continue
        if any("$" in x or "`" in x for x in a):
            expanded = [os.path.expandvars(x) for x in a]
            if any("$" in x or "`" in x for x in expanded):
                return None
            a = expanded
        found = Segment(a, cur, {**exported, **env}, kind)
    return found


SUMMARY_PASS = [
    re.compile(r"^=+ (?:\d+ (?:passed|skipped|deselected|xfailed|xpassed|warnings?)(?:, )?)+ in [\d.]+s", re.M),
    re.compile(r"^\s*Tests\s+\d+ passed \(\d+\)", re.M),                     # vitest
    re.compile(r"^Tests:\s+(?:\d+ (?:passed|skipped|todo), )*\d+ total", re.M),  # jest (no 'failed')
]
FAIL_WORDS = re.compile(r"\b(?:failed|FAILED|FAIL|failures?|errors?|ERROR|panicked|Traceback)\b")
SUMMARY_FAIL = [
    re.compile(r"(?m)^(?:=+ )?(?:\d+ \w+, )*\d+ (?:failed|errors?)\b[^\n]* in [\d.]+m?s"),   # pytest
    re.compile(r"(?m)^(?:FAILED|ERROR) \S+\.py"),                                          # pytest -rA lines
    re.compile(r"Interrupted: \d+ errors? during collection"),
    re.compile(r"(?m)^\s*Test(?:s| Files)\s+\d+ failed"),                                  # vitest
    re.compile(r"(?m)^Tests:\s+\d+ failed"),                                                # jest
    re.compile(r"(?m)^--- FAIL: |^FAIL\s+\S+"),                                             # go
    re.compile(r"test result: FAILED"),                                                      # cargo
    re.compile(r"(?m)^\s*\d+ failing\b"),                                                    # mocha
    re.compile(r"\d+ examples?, [1-9]\d* failures?"),                                         # rspec
    re.compile(r"(?m)^FAILED \((?:failures|errors)=\d+"),                                     # unittest
]


def output_says_failed(text: str) -> bool:
    """A test runner summary in the output reports failures (used when the shell command itself succeeded)."""
    return bool(text) and any(rx.search(text) for rx in SUMMARY_FAIL)


def _tool_output(payload: Dict) -> str:
    resp = payload.get("tool_response")
    if isinstance(resp, dict):
        return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output"))
    return str(resp or "")


def output_says_passed(text: str) -> bool:
    """Conservative: True only when a runner summary says everything passed and nothing failed."""
    if not text:
        return False
    if any(rx.search(text) for rx in SUMMARY_PASS):
        return not FAIL_WORDS.search(text)
    return False


def in_git_repo(cwd: str) -> bool:
    p = Path(cwd)
    for d in [p] + list(p.parents):
        if (d / ".git").exists():
            return True
    return False


# ------------------------------------------------------------------------------------- session state
def _state_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / brand.CACHE_DIR_NAME / "hook"


class Session:
    """Per Claude Code session: verdict-run count and recent verdicts (dedup), in the user cache dir."""

    def __init__(self, session_id: str, directory: Optional[Path] = None):
        sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "no-session")[:80] or "no-session"
        self.dir = directory or _state_dir()
        self.path = self.dir / f"{sid}.json"
        self.data: Dict = {"runs": 0, "verdicts": {}}
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("verdicts"), dict):
                self.data = d
        except (OSError, ValueError):
            pass

    def recent(self, key: str, now: float) -> Optional[Dict]:
        v = self.data["verdicts"].get(key)
        if isinstance(v, dict) and now - float(v.get("t", 0)) <= DEDUP_SECONDS:
            return v
        return None

    def put(self, key: str, context: str, now: float) -> None:
        vs = self.data["verdicts"]
        vs[key] = {"t": now, "context": context}
        for k in [k for k, v in vs.items() if now - float(v.get("t", 0)) > DEDUP_SECONDS]:
            vs.pop(k, None)

    def save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
            if self.data.get("runs", 0) <= 1:  # now and then: forget sessions older than two days
                for old in self.dir.glob("*.json"):
                    if time.time() - old.stat().st_mtime > 2 * 86400:
                        old.unlink()
        except OSError:
            pass


def fingerprint(root: str) -> Optional[str]:
    """HEAD sha + hash of the working-tree state (tracked diff incl. content, untracked files' stats)."""
    from . import gitutil
    head = gitutil.try_git(["rev-parse", "HEAD"], root)
    if not head:
        return None
    h = hashlib.sha256(head.encode())
    try:
        h.update(gitutil.git(["diff", "--no-ext-diff", "--binary", "HEAD", "--"], root).encode("utf-8", "replace"))
        untracked = gitutil.git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    except gitutil.GitError:
        return None
    for f in sorted(x for x in untracked.split("\0") if x):
        try:
            st = os.stat(os.path.join(root, f))
            h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}\0".encode())
        except OSError:
            h.update(f"{f}:gone\0".encode())
    return head + ":" + h.hexdigest()[:20]


# ------------------------------------------------------------------------------------- formatting
LABELS = [
    ("new", "NEW — caused by the current change"),
    ("pre_existing_error_changed", "PRE-EXISTING, ERROR CHANGED — failed on base too, but with a different error "
                                   "now; check the change did not make it worse"),
    ("unknown", "UNKNOWN — no trustworthy base result; do not call these pre-existing"),
    ("pre_existing", "PRE-EXISTING — already failing on the base commit {base} with the same error"),
    ("environment", "ENVIRONMENT — fails on base and head for an environmental reason"),
    ("flaky", "FLAKY — passed and failed on the same code"),
]
PER_GROUP = {"new": 10, "pre_existing_error_changed": 5, "unknown": 5, "pre_existing": 6, "environment": 5,
             "flaky": 5}


def _one(text: str, n: int = 160) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _counts(side: Dict, word: str) -> str:
    return f"{word} {side.get(word, 0)}/{side.get('runs', 0)}"


def _item(v: Dict, base_short: str) -> str:
    vid = v["id"] + (" [file failed to load]" if v.get("kind") == "load_error" else "")
    h, b = v.get("head") or {}, v.get("base") or {}
    herr = _one(h.get("message") or h.get("error") or "")
    berr = _one(b.get("message") or b.get("error") or "")
    verdict = v["verdict"]
    if verdict == "new":
        if b.get("passed"):
            ev = f"base {base_short}: {'loads' if v.get('kind') == 'load_error' else 'passed'} {b['passed']}/{b['runs']}; head: failed {h.get('failed', 0)}/{h.get('runs', 0)}"
        else:
            ev = f"not on base; head: failed {h.get('failed', 0)}/{h.get('runs', 0)}"
        line = f"- {vid}: {herr} ({ev})"
        if v.get("blocked_tests"):
            line += f"; {len(v['blocked_tests'])} test(s) in it can't run"
        if v.get("env_suspect"):
            line += " (error looks environmental: did the change add a dependency?)"
    elif verdict == "pre_existing":
        line = f"- {vid}: base {base_short} error: {berr} (failed base {b.get('failed', 0)}/{b.get('runs', 0)}, head {h.get('failed', 0)}/{h.get('runs', 0)})"
    elif verdict == "pre_existing_error_changed":
        line = f"- {vid}: base {base_short}: {_one(berr, 90)} | head now: {_one(herr, 90)}"
    elif verdict == "environment":
        detail = (v.get("env_class") or {}).get("detail") or herr
        line = f"- {vid}: {_one(detail, 120)} (fails on base {b.get('failed', 0)}/{b.get('runs', 0)} and head {h.get('failed', 0)}/{h.get('runs', 0)})"
    elif verdict == "flaky":
        if v.get("flaky_side") == "base":
            line = f"- {vid}: flaky on base (failed {b.get('failed', 0)}, passed {b.get('passed', 0)})"
        else:
            line = f"- {vid}: head failed {h.get('failed', 0)}, passed {h.get('passed', 0)} of {h.get('runs', 0)} runs"
        if v.get("surfaced"):
            line += " — passed in your run this time"
    else:
        line = f"- {vid}: {herr} — {_one(v.get('reason', ''), 120)}"
    return _one(line, LINE_MAX)


def format_context(rep: Dict, command: str, launcher: str, limit: int = MAX_CONTEXT) -> str:
    s = rep.get("summary", {})
    base, head = rep.get("base", {}), rep.get("head", {})
    bshort = base.get("short", "?")
    results = rep.get("results", [])
    counts = ", ".join(f"{s.get(k, 0)} {w}" for k, w in (("new", "NEW"), ("pre_existing", "pre-existing"),
                                                        ("pre_existing_error_changed", "pre-existing (error changed)"),
                                                        ("flaky", "flaky"), ("environment", "environment"),
                                                        ("unknown", "unknown")) if s.get(k))
    head_s = head.get("short", "?") + (" + uncommitted changes" if head.get("dirty") else "")
    how = base.get("how", "")
    hdr = (f"[{brand.NAME}] Per-test verdicts for the failing command `{_one(command, 120)}`: {counts or 'no failures'}. "
           f"Base {bshort} ({_one(how, 70)}) vs head {head_s}; {s.get('tests_total', 0)} tests, "
           f"{rep.get('test_runs', 0)} test runs, {rep.get('duration_s', 0):.0f}s.")
    guide = [
        "How to report these failures:",
        "- Fix the NEW failures: they pass on the base commit and fail with the current changes.",
        f"- Call a failure pre-existing only if it is listed under PRE-EXISTING above, and cite base {bshort} and its error.",
        "- FLAKY and ENVIRONMENT failures are not caused by this change and are not yours to fix: leave them and "
        "mention them to the user in one line.",
        "- UNKNOWN means there is no trustworthy base result: say so instead of guessing.",
        f"Details: .git/{brand.CACHE_DIR_NAME}/last.json, or run `{launcher} check -- {_one(command, 80)}`.",
    ]
    present = {v["verdict"] for v in results}
    if "unknown" not in present:
        guide = [g for g in guide if not g.startswith("- UNKNOWN")]
    if not present & {"flaky", "environment"}:
        guide = [g for g in guide if not g.startswith("- FLAKY")]
    if "new" not in present:
        guide[1:2] = ["- None of these failures was caused by the current change (no NEW)."]
    for per in (1.0, 0.6, 0.3, 0.0):
        lines = [hdr, ""]
        for verdict, title in LABELS:
            items = [v for v in results if v["verdict"] == verdict]
            if not items:
                continue
            cap = max(1, int(PER_GROUP[verdict] * per)) if per else 1
            lines.append(f"{title.format(base=bshort)} ({len(items)}):")
            lines += [_item(v, bshort) for v in items[:cap]]
            if len(items) > cap:
                lines.append(f"- … {len(items) - cap} more (see the details)")
        lines += [""] + guide
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + "…"


# ------------------------------------------------------------------------------------- entry point
def _emit(context: str, event: str = EVENT) -> None:
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
    sys.__stdout__.write(json.dumps(out) + "\n")
    sys.__stdout__.flush()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(brand.ENV_PREFIX + name, default))
    except ValueError:
        return default


def launcher_path() -> str:
    p = Path(__file__).resolve().parents[1] / "bin" / brand.CLI_NAME
    return str(p) if p.exists() else brand.CLI_NAME


def handle(payload: Dict, now: Optional[float] = None, session_dir: Optional[Path] = None) -> Optional[str]:
    """The additionalContext to return for this hook input, or None for 'say nothing'."""
    now = now or time.time()
    event = payload.get("hook_event_name") or EVENT
    if event not in (EVENT, EVENT_OK) or payload.get("tool_name") not in (None, "Bash"):
        return None
    if payload.get("is_interrupt"):
        return None
    tin = payload.get("tool_input") or {}
    command = tin.get("command") if isinstance(tin, dict) else None
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(command, str) or not command.strip() or not os.path.isdir(cwd):
        return None
    seg = find_test_segment(command, cwd)
    if seg is None or not os.path.isdir(seg.cwd) or not in_git_repo(seg.cwd):
        return None
    if event == EVENT and output_says_passed(str(payload.get("error") or "")):
        return None
    if event == EVENT_OK and not output_says_failed(_tool_output(payload)):
        return None  # the command succeeded and no test failure is visible: nothing to attribute
    name = brand.NAME
    budget = _env_float("HOOK_TIMEOUT", DEFAULT_BUDGET)
    dur = payload.get("duration_ms")
    if isinstance(dur, (int, float)) and dur / 1000.0 > budget / 2:
        return (f"[{name}] No verdict: `{_one(seg.display(), 100)}` took {dur / 1000:.0f}s, too long to re-run on "
                f"head and base within the hook's {budget:.0f}s budget. To attribute the failures run "
                f"`{launcher_path()} check -- {_one(seg.display(), 100)}`.")
    from .runners import from_command
    det = from_command(seg.argv, Path(seg.cwd))
    if det.runner == "generic":
        return (f"[{name}] `{_one(seg.display(), 100)}` is not a test runner it can attribute per test "
                f"(pytest, vitest, jest, go test, cargo test, rspec); no verdict. Running the underlying runner "
                f"through `{launcher_path()} check -- <runner command>` gives one.")
    from . import gitutil
    try:
        root = str(gitutil.repo_root(seg.cwd))
    except gitutil.GitError:
        return None
    fp = fingerprint(root)
    if fp is None:
        return None
    sess = Session(str(payload.get("session_id") or ""), session_dir)
    key = hashlib.sha256("\0".join([seg.display(), os.path.realpath(seg.cwd), json.dumps(seg.env, sort_keys=True),
                                    fp]).encode()).hexdigest()[:24]
    hit = sess.recent(key, now)
    if hit and hit.get("context"):
        return (f"[{name}] Same command, HEAD and working tree as a check {now - float(hit['t']):.0f}s ago, so "
                f"that check's result still holds:\n" + hit["context"])
    max_runs = int(_env_float("HOOK_MAX_RUNS", DEFAULT_MAX_RUNS))
    if sess.data.get("runs", 0) >= max_runs:
        return (f"[{name}] No verdict: this session already ran {max_runs} checks (the per-session limit, "
                f"{brand.ENV_PREFIX}HOOK_MAX_RUNS). Run `{launcher_path()} check -- {_one(seg.display(), 100)}` "
                f"to attribute these failures.")
    sess.data["runs"] = sess.data.get("runs", 0) + 1
    sess.save()
    ctx = run_check(seg, budget)
    if ctx is not None and not ctx.startswith(f"[{name}] No verdict"):
        sess.put(key, ctx, now)
        sess.save()
    return ctx


def run_check(seg: Segment, budget: float) -> Optional[str]:
    """Run the engine in-process for one test command; -> context text (or a one-line note)."""
    import contextlib
    import signal

    from . import procs
    from .engine import BaseError, BudgetExceeded, Engine, Options, UsageError
    name = brand.NAME
    deadline = time.time() + budget
    os.environ[brand.ENV_PREFIX + "IN_HOOK"] = "1"   # children (and anything they start) see it
    os.environ.update(seg.env)
    procs.install_signal_handlers()

    def on_alarm(signum, frame):  # noqa: ARG001 - backstop if a subprocess ignores its own timeout
        raise BudgetExceeded("hook budget")

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, on_alarm)
        signal.alarm(int(budget) + 5)
    opts = Options(command=list(seg.argv), cwd=seg.cwd, timeout=budget, deadline=deadline)
    eng = Engine(opts)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rep = eng.run()
    except BudgetExceeded:
        procs.kill_all()
        return (f"[{name}] No verdict (timed out after {budget:.0f}s): nothing is known about which of these "
                f"failures are new or pre-existing, so don't describe any of them as pre-existing.")
    except UsageError as e:
        if "not found" in str(e):
            return None
        return f"[{name}] No verdict: {_one(str(e), 200)}"
    except BaseError as e:
        return (f"[{name}] No verdict: could not establish the base result ({_one(str(e), 160)}). Without it, "
                f"don't describe any failure as pre-existing.")
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    try:
        st = eng.state_dir
        st.mkdir(parents=True, exist_ok=True)
        (st / "last.json").write_text(json.dumps(rep, indent=1))
    except OSError:
        pass
    if not rep.get("results"):
        warn = "; ".join(rep.get("warnings", []))
        return (f"[{name}] Re-ran `{_one(seg.display(), 100)}`: all {rep['summary'].get('tests_total', 0)} tests "
                f"passed this time, so the failure did not reproduce (a flaky test, or not a test failure"
                f"{': ' + _one(warn, 150) if warn else ''}).")
    return format_context(rep, seg.display(), launcher_path())


def _trace(payload: Dict, event: str, ctx: Optional[str], seconds: float) -> None:
    """<PREFIX>HOOK_LOG=<file>: append one JSON line per invocation (what fired, what it returned)."""
    path = os.environ.get(brand.ENV_PREFIX + "HOOK_LOG")
    if not path:
        return
    tin = payload.get("tool_input") or {}
    rec = {"t": round(time.time(), 3), "event": event, "session": payload.get("session_id"),
           "command": str(tin.get("command") if isinstance(tin, dict) else "")[:300], "cwd": payload.get("cwd"),
           "seconds": round(seconds, 3), "context_chars": len(ctx or ""), "context": (ctx or "")[:MAX_CONTEXT]}
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def main(argv: List[str]) -> int:
    """`<cli> hook post-tool-failure|post-tool-use`: hook JSON on stdin -> at most one JSON object on stdout;
    always exit 0."""
    try:
        if os.environ.get(brand.ENV_PREFIX + "IN_HOOK"):
            return 0  # never recurse (a test suite that runs an agent, ...)
        if os.environ.get(brand.ENV_PREFIX + "HOOK", "").lower() in ("off", "0", "false", "no"):
            return 0
        event = EVENTS.get(argv[0] if argv else "")
        if event is None:
            return 0
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        payload.setdefault("hook_event_name", event)
        if payload["hook_event_name"] != event:
            return 0
        t0 = time.time()
        ctx = handle(payload)
        if ctx:
            _emit(ctx[:MAX_CONTEXT], event)
        _trace(payload, event, ctx, time.time() - t0)
    except BaseException:  # never crash, never block the agent
        try:
            if os.environ.get(brand.ENV_PREFIX + "HOOK_DEBUG"):
                import traceback
                traceback.print_exc()
        except BaseException:
            pass
    return 0
