"""Data model shared by runners, the engine and the decision logic."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

# Observation statuses
PASS = "pass"
FAIL = "fail"
SKIP = "skip"
ABSENT = "absent"     # not in the report (not collected / does not exist on that side)
BLOCKED = "blocked"   # its file/package failed to load on that side

# Unit kinds
TEST = "test"
LOAD_ERROR = "load_error"
COMMAND = "command"   # unknown runner: the whole command is one unit
CHECK = "check_error" # one error reported by a check command (tsc, eslint, prettier, ...)

# Verdicts (stable machine strings)
NEW = "new"
PRE = "pre_existing"
PRE_CHANGED = "pre_existing_error_changed"
FLAKY = "flaky"
ENVIRONMENT = "environment"
UNKNOWN = "unknown"

VERDICT_LABELS = {
    NEW: "NEW",
    PRE: "PRE-EXISTING",
    PRE_CHANGED: "PRE-EXISTING (error changed)",
    FLAKY: "FLAKY",
    ENVIRONMENT: "ENVIRONMENT",
    UNKNOWN: "UNKNOWN",
}


@dataclass
class Obs:
    """One observation of one unit in one run."""
    status: str
    key: str = ""          # normalised comparison key (exception type + message)
    message: str = ""      # raw short message
    raw: str = ""          # raw excerpt (traceback/body), truncated
    env: Optional[Dict[str, str]] = None   # environment-class match {"class":..., "detail":...}
    cached: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("cached", None)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Obs":
        return cls(status=d.get("status", ABSENT), key=d.get("key", ""), message=d.get("message", ""),
                   raw=d.get("raw", ""), env=d.get("env"), cached=True)


@dataclass
class TestResult:
    """A test as parsed from a runner report."""
    id: str                 # display id (repo-relative), unique within a run
    group: str              # file (repo-relative) or go package: the unit that can fail to load
    status: str             # PASS/FAIL/SKIP
    name: str = ""          # bare name used to look the test up in base sources
    locator: Dict[str, str] = field(default_factory=dict)  # runner-specific rerun info
    key: str = ""
    message: str = ""
    raw: str = ""
    env: Optional[Dict[str, str]] = None

    def obs(self) -> Obs:
        return Obs(self.status, self.key, self.message, self.raw, self.env)


@dataclass
class LoadError:
    id: str                 # file (repo-relative) or package
    key: str = ""
    message: str = ""
    raw: str = ""
    env: Optional[Dict[str, str]] = None
    locator: Dict[str, str] = field(default_factory=dict)

    def obs(self) -> Obs:
        return Obs(FAIL, self.key, self.message, self.raw, self.env)


@dataclass
class RunResult:
    tests: Dict[str, TestResult] = field(default_factory=dict)
    load_errors: Dict[str, LoadError] = field(default_factory=dict)
    ok: bool = True              # a report was produced and parsed
    problem: str = ""            # why the run can't be trusted (crash, timeout, ...)
    exit_code: int = 0
    duration: float = 0.0
    output_tail: str = ""
    invocations: List[List[str]] = field(default_factory=list)

    @property
    def collected(self) -> int:
        return len(self.tests)

    def failing_tests(self) -> List[TestResult]:
        return [t for t in self.tests.values() if t.status == FAIL]

    def merge(self, other: "RunResult") -> None:
        for k, v in other.tests.items():
            prev = self.tests.get(k)
            if prev is None or (prev.status != FAIL and v.status == FAIL):
                self.tests[k] = v
        self.load_errors.update(other.load_errors)
        if not other.ok:
            self.ok = False
            self.problem = "; ".join(x for x in (self.problem, other.problem) if x)
        self.exit_code = self.exit_code or other.exit_code
        self.duration += other.duration
        self.output_tail = (self.output_tail + "\n" + other.output_tail).strip()[-4000:]
        self.invocations.extend(other.invocations)


@dataclass
class Unit:
    """A failing unit under investigation."""
    id: str
    kind: str
    group: str
    name: str = ""
    locator: Dict[str, str] = field(default_factory=dict)
