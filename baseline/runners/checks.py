"""Check commands (type check, lint, format check, build): one unit per error.

A check prints its errors; we parse them into units whose id is stable across
sides (file + code/rule + normalised message, no line numbers), so base and head
can be compared as error SETS rather than by exit code.

Parsers: tsc (both output forms), eslint (stylish and JSON), prettier --check,
and `next build` type errors. An exit code != 0 with nothing parsed becomes one
whole-command unit (WHOLE) that can't be compared per error.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import normalize
from ..model import CHECK, COMMAND, FAIL, PASS, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result

WHOLE = "(command)"

# file(12,5): error TS2322: msg      |   file:12:5 - error TS2322: msg
TSC_PAREN = re.compile(r"^(?P<file>[^\s(][^()]*?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): (?P<msg>.*)$")
TSC_PRETTY = re.compile(r"^(?P<file>\S[^:]*?):(?P<line>\d+):(?P<col>\d+) - error (?P<code>TS\d+): (?P<msg>.*)$")
TSC_GLOBAL = re.compile(r"^error (?P<code>TS\d+): (?P<msg>.*)$")
# eslint stylish: a file path line, then "  12:5  error  msg  rule-id"
ESLINT_FILE = re.compile(r"^(/\S.*|[A-Za-z]:\\.*|\S+\.\w+)$")
ESLINT_ROW = re.compile(r"^\s+(\d+):(\d+)\s+(error|warning)\s+(.*?)(?:\s{2,}(\S+))?\s*$")
PRETTIER_WARN = re.compile(r"^\[warn\] (?!Code style issues)(.+?)\s*$")
PRETTIER_ERR = re.compile(r"^\[error\] (.+?): (.+)$")
NEXT_LOC = re.compile(r"^(?:\./)?(?P<file>\S+\.\w+):(?P<line>\d+):(?P<col>\d+)\s*$")
NEXT_TYPE = re.compile(r"^Type error: (?P<msg>.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")

TOOLS = ("tsc", "eslint", "prettier", "next-build")
NOUN = {"tsc": "type error", "eslint": "lint error", "prettier": "unformatted file", "next-build": "build error"}


def _clean(text: str) -> List[str]:
    return [ANSI.sub("", ln).rstrip() for ln in text.splitlines()]


def _norm_msg(msg: str, side: Optional[Side]) -> str:
    msg = " ".join(msg.split())
    if side is not None:
        msg = normalize.normalize(msg, side.roots())
    return msg


def parse_tsc(text: str, side: Optional[Side] = None) -> List[Tuple[str, str, str]]:
    """-> [(file, code, message)]; continuation lines of a message are appended."""
    out: List[Tuple[str, str, str]] = []
    lines = _clean(text)
    i = 0
    while i < len(lines):
        ln = lines[i]
        m = TSC_PAREN.match(ln) or TSC_PRETTY.match(ln)
        f = m.group("file") if m else ""
        if not m:
            m = TSC_GLOBAL.match(ln.strip())
        if m:
            msg = m.group("msg")
            j = i + 1
            # paren form: indented continuation lines belong to the message
            while j < len(lines) and lines[j].startswith("  ") and not re.match(r"^\s*\d+\s", lines[j]) \
                    and not lines[j].strip().startswith("~") and TSC_PRETTY.match(lines[j].strip()) is None:
                msg += " " + lines[j].strip()
                j += 1
            rel = side.rel(f) if (side is not None and f) else f
            out.append((rel, m.group("code"), _norm_msg(msg, side)))
            i = j
            continue
        i += 1
    return out


def parse_eslint(text: str, side: Optional[Side] = None) -> List[Tuple[str, str, str]]:
    """JSON (array of {filePath, messages}) or stylish text. Errors only (warnings don't fail)."""
    s = text.strip()
    out: List[Tuple[str, str, str]] = []
    if s.startswith("["):
        end = s.rfind("]")
        try:
            data = json.loads(s[: end + 1])
        except ValueError:
            data = None
        if isinstance(data, list):
            for f in data:
                rel = side.rel(f.get("filePath", "")) if side is not None else f.get("filePath", "")
                for msg in f.get("messages") or []:
                    if msg.get("severity", 2) >= 2:
                        out.append((rel, msg.get("ruleId") or "(parse)", _norm_msg(msg.get("message", ""), side)))
            return out
    cur = ""
    for ln in _clean(text):
        m = ESLINT_ROW.match(ln)
        if m and cur:
            if m.group(3) == "error":
                out.append((cur, m.group(5) or "(parse)", _norm_msg(m.group(4), side)))
            continue
        if ln and not ln[0].isspace() and ESLINT_FILE.match(ln) and not ln.startswith(("✖", "Oops")):
            cur = side.rel(ln) if side is not None else ln
    return out


def parse_prettier(text: str, side: Optional[Side] = None) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for ln in _clean(text):
        m = PRETTIER_ERR.match(ln)
        if m:
            f = side.rel(m.group(1)) if side is not None else m.group(1)
            out.append((f, "syntax", _norm_msg(m.group(2), side)))
            continue
        m = PRETTIER_WARN.match(ln)
        if m and not m.group(1).startswith(("Code style issues", "Run Prettier")):
            f = side.rel(m.group(1)) if side is not None else m.group(1)
            out.append((f, "format", "not formatted"))
    return out


def parse_next_build(text: str, side: Optional[Side] = None) -> List[Tuple[str, str, str]]:
    """`next build`: './src/x.tsx:12:3' followed by 'Type error: ...'; tsc lines too if present."""
    out = parse_tsc(text, side)
    lines = _clean(text)
    for i, ln in enumerate(lines):
        m = NEXT_TYPE.match(ln.strip())
        if not m:
            continue
        f = ""
        for k in range(i - 1, max(-1, i - 4), -1):
            lm = NEXT_LOC.match(lines[k].strip())
            if lm:
                f = side.rel(lm.group("file")) if side is not None else lm.group("file")
                break
        out.append((f, "type", _norm_msg(m.group("msg"), side)))
    return out


PARSERS = {"tsc": parse_tsc, "eslint": parse_eslint, "prettier": parse_prettier, "next-build": parse_next_build}


def check_tool(argv: List[str]) -> Optional[str]:
    """Which check a command is (after npm-script resolution), or None."""
    names = [os.path.basename(a) for a in argv[:4]]
    if not names:
        return None
    first = 1 if names[0] in ("npx", "pnpx", "bunx") or argv[:2] in (["pnpm", "exec"], ["yarn", "exec"]) else 0
    if argv[:2] in (["pnpm", "exec"], ["yarn", "exec"]):
        first = 2
    rest = names[first:]
    if not rest:
        return None
    tool = rest[0]
    if tool in ("tsc", "vue-tsc"):
        return None if any(a in ("-w", "--watch", "--init", "-v", "--version") for a in argv) else "tsc"
    if tool == "eslint":
        return None if any(a in ("--fix", "--init") for a in argv) else "eslint"
    if tool == "prettier":
        return "prettier" if any(a in ("--check", "-c", "--list-different", "-l") for a in argv) else None
    if tool == "next" and len(rest) > 1 and rest[1] == "build":
        return "next-build"
    return None


def error_lines(text: str, n: int = 6) -> Dict[str, List[str]]:
    """First and last lines that look like errors (for proof on whole-command runs)."""
    rx = re.compile(r"\b(?:error|Error|ERR!?|fail(?:ed|ure)?|FAIL|panic|Traceback|not found|"
                    r"Cannot|cannot|warn(?:ing)?)\b")
    lines = [ln.strip() for ln in _clean(text) if ln.strip()]
    hits = [ln for ln in lines if rx.search(ln)]
    if not hits:
        hits = lines[-n:]
    first = hits[:n]
    last = [ln for ln in hits[-n:] if ln not in first]
    return {"first": [ln[:300] for ln in first], "last": [ln[:300] for ln in last]}


class CheckRunner(Runner):
    """A check command (tsc/eslint/prettier/next build): rerun = the whole command; units = errors."""
    per_test = True
    deterministic = True

    def __init__(self, argv: List[str], project_dir: Path, tool: str = ""):
        super().__init__(argv, project_dir)
        self.tool = tool or check_tool(argv) or "tsc"
        self.name = self.tool
        self.noun = NOUN.get(self.tool, "error")
        self.json_report = False
        if self.tool == "eslint" and not any(a in ("-f", "--format") or a.startswith("--format=") or
                                             a.startswith("-o") or a.startswith("--output-file")
                                             for a in argv):
            self.json_report = True  # stylish is the default; JSON is unambiguous and safe to ask for

    def _argv(self) -> List[str]:
        if self.json_report:
            return list(self.argv) + ["--format", "json"]
        return list(self.argv)

    def full(self, side: Side, tag: str) -> List[Invocation]:
        return [Invocation(self._argv(), None, "full", {"merge_stderr": True})]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        return self.full(side, tag)

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"{self.tool} timed out after {proc.duration:.0f}s", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        errors = PARSERS[self.tool](proc.output, side) if proc.returncode != 0 else []
        if proc.returncode == 0 or errors:
            res.tests[WHOLE] = TestResult(WHOLE, WHOLE, PASS, WHOLE, {"kind": COMMAND})
        else:
            res.tests[WHOLE] = whole_failure(proc, side, self.classify)
        seen: Dict[str, int] = {}
        for f, code, msg in errors:
            base_id = f"{f or '(global)'}: {code} {msg}" if self.tool != "prettier" or code != "format" \
                else f"{f}: not formatted"
            seen[base_id] = seen.get(base_id, 0) + 1
            uid = base_id if seen[base_id] == 1 else f"{base_id} (#{seen[base_id]})"
            key = f"{code}: {msg}"
            res.tests[uid] = TestResult(uid, f or "(global)", FAIL, uid, {"kind": CHECK, "file": f}, key,
                                        msg, f"{f}: {code} {msg}", None)
        return res


def whole_failure(proc, side: Side, classify) -> TestResult:
    """The whole command failed and its output can't be read per error."""
    text = normalize.strip_ansi(proc.output)
    el = error_lines(text)
    shown = el["first"] + el["last"]
    msg = shown[0] if shown else f"exit code {proc.returncode}"
    key = normalize.normalize("\n".join(shown[-5:]) or f"exit code {proc.returncode}", side.roots())
    raw = "\n".join(shown) or normalize.first_lines("\n".join(text.strip().splitlines()[-15:]), 15)
    env = classify(normalize.normalize("\n".join(shown), side.roots()), text[-4000:], side)
    return TestResult(WHOLE, WHOLE, FAIL, WHOLE, {"kind": COMMAND, "exit_code": str(proc.returncode)}, key,
                      normalize.one_line(msg)[:300], raw, env)
