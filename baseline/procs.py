"""Subprocess execution with timeouts and process-group kill.

Every child runs in its own session/process group so that a timeout or Ctrl-C
kills the whole tree (test runners love to spawn workers).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

_ACTIVE: "set[subprocess.Popen]" = set()
_LOCK = threading.Lock()
MAX_CAPTURE = 4 * 1024 * 1024  # keep at most the last 4 MiB of each stream


@dataclass
class ProcResult:
    argv: List[str]
    cwd: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    not_found: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def output(self) -> str:
        return self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr


def _read_tail(f) -> str:
    f.flush()
    size = f.seek(0, os.SEEK_END)
    f.seek(max(0, size - MAX_CAPTURE))
    return f.read().decode("utf-8", "replace")


def kill_group(p: subprocess.Popen, grace: float = 2.0) -> None:
    """Terminate the child's whole process group (TERM, then KILL)."""
    if p.poll() is not None:
        # leader gone; workers may survive in the group
        _signal_group(p, getattr(signal, "SIGKILL", signal.SIGTERM))
        return
    _signal_group(p, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if p.poll() is not None:
            break
        time.sleep(0.02)
    _signal_group(p, getattr(signal, "SIGKILL", signal.SIGTERM))
    try:
        p.wait(timeout=5)
    except Exception:
        pass


def _signal_group(p: subprocess.Popen, sig: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(p.pid, sig)
        else:  # pragma: no cover - windows best effort
            p.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_all() -> None:
    with _LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        kill_group(p, grace=0.5)


def run(argv: List[str], cwd: str, env: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None, merge_stderr: bool = False) -> ProcResult:
    """Run argv (no shell). Never raises for a failing/missing command.

    merge_stderr: interleave stderr into stdout (for runners that split one report over both)."""
    t0 = time.time()
    out = tempfile.TemporaryFile()
    err = tempfile.TemporaryFile()
    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT if merge_stderr else err, **kwargs)
        except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
            return ProcResult(argv, cwd, 127, "", f"{argv[0]}: {e.strerror or e}",
                              time.time() - t0, not_found=True)
        with _LOCK:
            _ACTIVE.add(p)
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_group(p)
        except BaseException:
            kill_group(p, grace=0.5)
            raise
        finally:
            with _LOCK:
                _ACTIVE.discard(p)
        if not timed_out:
            # a runner may leave orphaned workers in its group; reap them
            _signal_group(p, getattr(signal, "SIGKILL", signal.SIGTERM))
        return ProcResult(argv, cwd, p.returncode if p.returncode is not None else -9,
                          _read_tail(out), _read_tail(err), time.time() - t0, timed_out)
    finally:
        out.close()
        err.close()


class Interrupted(BaseException):
    """Raised from SIGTERM/SIGHUP handlers so that finally-blocks clean up."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        raise Interrupted(signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not main thread / unsupported
                pass


def is_windows() -> bool:
    return sys.platform.startswith("win")
