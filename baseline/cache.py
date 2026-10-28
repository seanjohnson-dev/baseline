"""Base-result cache: .git/<name>/cache/<base-sha>-<hash(cmd+env)>.json

Stores base-side observations per unit (and the full base run summary), so an
agent's edit/check loop does not rebuild the same base again and again. The key
covers the base commit, the runner command, the tool version and an
environment fingerprint (env vars, installed deps), so a cached result is only
reused under the same conditions. Only trusted results are stored.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .model import Obs

VOLATILE_ENV = {
    "PWD", "OLDPWD", "SHLVL", "_", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID", "COLUMNS",
    "LINES", "DISPLAY", "WINDOWID", "TMUX", "TMUX_PANE", "STY", "SSH_CLIENT", "SSH_CONNECTION", "SSH_TTY",
    "SSH_AUTH_SOCK", "PS1", "PS2", "PROMPT", "RPROMPT", "HISTFILE", "HISTSIZE", "LS_COLORS", "COLORTERM",
    "SECURITYSESSIONID", "XPC_SERVICE_NAME", "XPC_FLAGS", "__CF_USER_TEXT_ENCODING", "Apple_PubSub_Socket_Render",
    "VSCODE_GIT_IPC_HANDLE", "VSCODE_INJECTION", "GIT_ASKPASS", "OSLogRateLimit", "LESS", "PAGER", "EDITOR",
    "VISUAL", "MAIL", "LOGNAME", "INVOCATION_ID", "JOURNAL_STREAM", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_ID",
    "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_SESSION_CLASS", "MOTD_SHOWN", "WT_SESSION", "WT_PROFILE_ID",
}
VOLATILE_PREFIXES = ("CLAUDE_CODE_SSE", "ITERM_", "KITTY_", "WEZTERM_", "ALACRITTY_", "GNOME_TERMINAL_",
                     "KONSOLE_", "VTE_", "TERMINAL_")
KEEP_FILES = 40


def env_fingerprint(env: Mapping[str, str], extra_files: Iterable[Path] = (), ignore_prefix: str = "") -> str:
    h = hashlib.sha256()
    for k in sorted(env):
        if k in VOLATILE_ENV or k.startswith(VOLATILE_PREFIXES) or (ignore_prefix and k.startswith(ignore_prefix)):
            continue
        h.update(k.encode() + b"=" + env[k].encode("utf-8", "replace") + b"\0")
    for f in extra_files:
        try:
            st = f.stat()
            h.update(f"{f}:{st.st_mtime_ns}:{st.st_size}\0".encode())
        except OSError:
            h.update(f"{f}:missing\0".encode())
    return h.hexdigest()


class BaseCache:
    def __init__(self, cache_dir: Path, base_sha: str, key_material: str, enabled: bool = True):
        self.dir = cache_dir
        self.enabled = enabled
        digest = hashlib.sha256(key_material.encode("utf-8", "replace")).hexdigest()[:16]
        self.path = cache_dir / f"{base_sha}-{digest}.json"
        self.data: Dict = {"version": 1, "base_sha": base_sha, "obs": {}, "full": None}
        self.hit = False
        if enabled and self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                if d.get("version") == 1 and d.get("base_sha") == base_sha:
                    self.data = d
                    self.hit = True
            except (OSError, ValueError):
                pass

    def obs(self, unit_id: str) -> List[Obs]:
        if not self.enabled:
            return []
        return [Obs.from_dict(o) for o in self.data["obs"].get(unit_id, [])]

    def add(self, unit_id: str, obs: Obs) -> None:
        self.data["obs"].setdefault(unit_id, []).append(obs.to_dict())

    @property
    def full(self) -> Optional[dict]:
        return self.data.get("full") if self.enabled else None

    def set_full(self, full: dict) -> None:
        self.data["full"] = full

    def save(self) -> None:
        if not self.enabled:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".tmp{os.getpid()}")
            self.data["saved_at"] = time.time()
            tmp.write_text(json.dumps(self.data))
            os.replace(tmp, self.path)
            self._prune()
        except OSError:
            pass

    def _prune(self) -> None:
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass
