"""Error-text normalisation for comparing a failure on base with one on head.

The base run happens in a temp worktree, so absolute paths differ; timings,
addresses, uuids, temp names, ports and pids differ run to run. Assertion
values are *kept*: ``assert 1.57 == 1.58`` vs ``assert 1.56 == 1.58`` is an
"error changed".
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Iterable, List, Optional

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
ISO_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
CLOCK_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")
DURATION_RE = re.compile(
    r"(?<![\w.])\d+(?:\.\d+)?\s?(?:ns|µs|us|ms|s|sec|secs|seconds?|milliseconds?|m(?:in)?)(?![\w])")
PAREN_DURATION_RE = re.compile(r"\(\d+(?:\.\d+)?s\)")
GO_ROUTINE_RE = re.compile(r"\bgoroutine \d+\b")
PID_RE = re.compile(r"\b(pid|PID)([ =:#]+)\d+\b")
# ephemeral ports (32768+) after a host
PORT_RE = re.compile(r"((?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|::1)[:.])(3[2-9]\d{3}|[4-5]\d{4}|6[0-5]\d{3})\b")
TMPNAME_RE = re.compile(r"\btmp(?=[a-z_]*\d)[a-z0-9_]{6,12}\b")
PYTEST_TMP_RE = re.compile(r"pytest-of-[^/\s]+/pytest-\d+/[^/\s]+?\d+")
LINE_IN_FRAME_RE = re.compile(r"((?:[\w./\\-]+)\.(?:py|js|mjs|cjs|ts|tsx|jsx|mts|cts|go|rs|rb)):(\d+)(?::(\d+))?")
PY_LINE_RE = re.compile(r'(File "[^"]+", line )\d+')
WS_RE = re.compile(r"[ \t]+")
NUM_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _root_variants(roots: Iterable[str]) -> List[str]:
    out = set()
    for r in roots:
        if not r:
            continue
        r = str(r).rstrip("/\\")
        if len(r) < 2:
            continue
        out.add(r)
        try:
            out.add(os.path.realpath(r))
        except OSError:
            pass
        out.add(r.replace("\\", "/"))
    # longest first so nested roots win
    return sorted(out, key=len, reverse=True)


def _temp_dirs() -> List[str]:
    dirs = {tempfile.gettempdir(), "/tmp", "/var/tmp", "/private/tmp", "/private/var/folders"}
    t = os.environ.get("TMPDIR")
    if t:
        dirs.add(t)
    return sorted((d.rstrip("/") for d in dirs if d), key=len, reverse=True)


def normalize(text: str, roots: Iterable[str] = (), line_numbers: bool = True) -> str:
    """Normalise an error message for exact comparison.

    roots: absolute directories to strip (repo root, base worktree root ...);
    paths below them become repo-relative.
    """
    if not text:
        return ""
    s = strip_ansi(text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("file://", "")
    for r in _root_variants(roots):
        s = s.replace(r + "/", "").replace(r + "\\", "").replace(r, "<ROOT>")
    s = PYTEST_TMP_RE.sub("<TMP>", s)
    home = os.path.expanduser("~")
    for t in _temp_dirs():
        s = re.sub(re.escape(t) + r"/[^\s'\"():,]*", "<TMP>", s)
    if home and len(home) > 1:
        s = s.replace(home + "/", "~/")
    s = TMPNAME_RE.sub("<TMP>", s)
    s = UUID_RE.sub("<UUID>", s)
    s = ADDR_RE.sub("0x<ADDR>", s)
    s = ISO_TS_RE.sub("<TS>", s)
    s = CLOCK_RE.sub("<TIME>", s)
    s = PAREN_DURATION_RE.sub("(<DUR>)", s)
    s = DURATION_RE.sub("<DUR>", s)
    s = GO_ROUTINE_RE.sub("goroutine <N>", s)
    s = PID_RE.sub(lambda m: m.group(1) + m.group(2) + "<N>", s)
    s = PORT_RE.sub(lambda m: m.group(1) + "<PORT>", s)
    if line_numbers:
        s = PY_LINE_RE.sub(r"\1<L>", s)
        s = LINE_IN_FRAME_RE.sub(lambda m: m.group(1) + ":<L>" + (":<C>" if m.group(3) else ""), s)
    lines = [WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def loose(text: str) -> str:
    """Numbers masked. Only used when a side's own message varies run to run."""
    return NUM_RE.sub("<N>", text)


def first_lines(text: str, n: int = 12, width: int = 2000) -> str:
    s = strip_ansi(text or "").strip("\n")
    lines = s.splitlines()
    out = "\n".join(lines[:n])
    if len(lines) > n:
        out += "\n…"
    return out[:width]


def one_line(text: str, width: int = 200) -> str:
    s = " ".join(strip_ansi(text or "").split())
    return s if len(s) <= width else s[: width - 1] + "…"


def similarity(a: Optional[str], b: Optional[str]) -> float:
    import difflib
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)
