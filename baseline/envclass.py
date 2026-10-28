"""Classify an error as "environmental" (missing module/executable/env var/service/...).

Rules are deliberately narrow. A plain assertion failure is never environmental,
and a missing *first-party* module (one that lives in the repo) is a code
error, not an environment problem. Env-var rules require that the variable
really is unset in the environment the tests ran with.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

SourceReader = Callable[[str, int], Optional[str]]

ASSERTION_RE = re.compile(
    r"^(?:AssertionError: )?(?:assert |expected |expect\(|Expected[: ])|^AssertionError: expected ",
    re.I)

PY_NO_MODULE = re.compile(r"No module named '([^']+)'")
PY_SHARED_LIB = re.compile(r"(lib[\w.+-]+\.so[\w.]*): cannot open shared object file")
JS_NO_MODULE = re.compile(r"Cannot find (?:package|module) '([^']+)'")
VITE_RESOLVE = re.compile(r"Failed to (?:resolve import|load url) \"?([^\"\s]+)\"?")
GO_NO_MODULE = re.compile(r"no required module provides package (\S+)|cannot find package \"([^\"]+)\"|"
                          r"missing go\.sum entry for module providing package (\S+)|"
                          r"cannot find module providing package (\S+)")
RUBY_LOAD = re.compile(r"cannot load such file -- (\S+)")

EXEC_PATTERNS = [
    re.compile(r"exec: \"([^\"]+)\": executable file not found in \$PATH"),
    re.compile(r"executable file not found in \$(?:PATH|path)"),
    re.compile(r"spawn (\S+) ENOENT"),
    re.compile(r"(?:^|\s)([\w.-]+): command not found"),
    re.compile(r"C compiler \"(\w+)\" not found"),
    re.compile(r"Executable doesn't exist at (\S+)"),          # playwright browsers
]
PY_FILE_NOT_FOUND = re.compile(r"(?:FileNotFoundError|No such file or directory)[^'\n]*: '([^']+)'")
NODE_ENOENT_OPEN = re.compile(r"ENOENT: no such file or directory, (?:open|stat|access|lstat) '([^']+)'")
GO_OPEN_MISSING = re.compile(r"open (\S+): no such file or directory")

SERVICE_RE = re.compile(
    r"(?i:connection refused|actively refused|could not connect to server|connection to server at .* failed|"
    r"Can't connect to (?:local )?MySQL server|Connection reset by peer|server closed the connection unexpectedly|"
    r"Cannot connect to the Docker daemon|No route to host|Network is unreachable|Error 111 connecting|"
    r"Connect call failed)|\b(?:ECONNREFUSED|ECONNRESET|EHOSTUNREACH|ENETUNREACH)\b")
TIMEOUT_RE = re.compile(r"(?i:(?:connect|connection|dial)[^\n]{0,80}(?:timed out|timeout)|i/o timeout)|"
                        r"\bETIMEDOUT\b|\bConnectTimeout")
DNS_RE = re.compile(r"(?i:Name or service not known|nodename nor servname|Temporary failure in name resolution|"
                    r"no such host|getaddrinfo failed|Could not resolve host)|\b(?:ENOTFOUND|EAI_AGAIN)\b|"
                    r"NameResolutionError")
CREDS_RE = re.compile(r"NoCredentialsError|Unable to locate credentials|could not find default credentials|"
                      r"DefaultCredentialsError|NoCredentialProviders", re.I)
DISPLAY_RE = re.compile(r"cannot open display|Missing X server or \$DISPLAY", re.I)
RESOURCE_RE = re.compile(r"No space left on device|ENOSPC|Too many open files|EMFILE|Cannot allocate memory",
                         re.I)
CONFIG_NAME_RE = re.compile(
    r"(^|/)\.env(\.|$)|credential|secret|\.pem$|\.key$|\.crt$|\.p12$|token|\.netrc$|kubeconfig|"
    r"service[-_]?account|(^|/)config\.(json|ya?ml|toml|ini)$|settings\.(json|ya?ml|toml|ini|py)$|"
    r"(^|/)\.?[\w-]+rc$", re.I)
PERM_RE = re.compile(r"PermissionError: \[Errno 13\][^']*'([^']+)'|EACCES[^']*'([^']+)'|"
                     r"open (\S+): permission denied", re.I)

ENV_NAME = r"[A-Z][A-Z0-9]*_[A-Z0-9_]+"
ENV_PHRASE_RE = re.compile(
    r"\b(" + ENV_NAME + r")\b[^\n]{0,40}?\b(?i:is not set|not set|unset|is missing|missing|is required|required|"
    r"must be set|not defined|undefined|is empty|empty|not configured|not provided)\b|"
    r"(?i:environment variable|env var(?:iable)?|missing env)[^\n]{0,20}?\b(" + ENV_NAME + r")\b")
PY_KEYERROR_RE = re.compile(r"KeyError: '(" + ENV_NAME + r")'")
JS_ENV_SRC_RE = re.compile(r"(?:process\.env|import\.meta\.env)(?:\.([A-Za-z_]\w*)|\[\s*['\"]([A-Za-z_]\w*)['\"]\s*\])")
JS_FRAME_RE = re.compile(r"at (?:[^\n(]*\()?((?:/|[A-Za-z]:\\)[^\s():]+):(\d+):\d+\)?")
JS_UNDEFINED_RE = re.compile(r"^(?:TypeError|ReferenceError): ", re.M)
ENV_SUFFIX_RE = re.compile(r"_(URL|URI|DSN|HOST|PORT|KEY|TOKEN|SECRET|PASSWORD|PASS|USER|USERNAME|PATH|DIR|"
                           r"ENV|ID|REGION|ENDPOINT|BUCKET|DATABASE|DB|NAME)$")

NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns", "events", "fs", "http", "http2",
    "https", "net", "os", "path", "perf_hooks", "process", "querystring", "readline", "stream", "string_decoder",
    "timers", "tls", "tty", "url", "util", "v8", "vm", "worker_threads", "zlib", "module", "inspector",
}


class EnvContext:
    def __init__(self, root: str, env: Optional[Mapping[str, str]] = None,
                 source_reader: Optional[SourceReader] = None):
        self.root = root
        self.env = os.environ if env is None else env
        self.source_reader = source_reader or self._read_source

    def _read_source(self, path: str, line: int) -> Optional[str]:
        p = Path(path)
        if not p.is_absolute():
            p = Path(self.root) / p
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for i, text in enumerate(f, 1):
                    if i == line:
                        return text
        except OSError:
            return None
        return None

    def unset(self, name: str) -> bool:
        return not self.env.get(name)

    # -- first-party detection ------------------------------------------------
    def py_first_party(self, module: str) -> bool:
        top = module.split(".")[0]
        if not top:
            return True
        root = Path(self.root)
        for base in (root, root / "src", root / "lib", root / "tests", root / "test"):
            if (base / top).is_dir() or (base / (top + ".py")).is_file():
                return True
        return False

    def js_first_party(self, spec: str) -> bool:
        if spec.startswith((".", "/", "~", "@/", "#", "\\")) or re.match(r"^[A-Za-z]:[\\/]", spec):
            return True
        if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
            return True  # a builtin can't be "missing"; treat as code error
        name = self._pkg_name(spec)
        try:
            with open(Path(self.root) / "package.json", encoding="utf-8") as f:
                import json
                if json.load(f).get("name") == name:
                    return True
        except (OSError, ValueError):
            pass
        return False

    @staticmethod
    def _pkg_name(spec: str) -> str:
        parts = spec.split("/")
        return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _m(cls: str, detail: str) -> Dict[str, str]:
    return {"class": cls, "detail": detail}


def is_plain_assertion(key: str) -> bool:
    return bool(ASSERTION_RE.search(key.strip()))


def classify(key: str, raw: str, ctx: EnvContext) -> Optional[Dict[str, str]]:
    """Return {"class", "detail"} if the failure is environmental, else None.

    key: normalised "ExcType: message" (what is compared across sides);
    raw: the raw excerpt (traceback / stack / output) for supporting evidence.
    """
    key = key or ""
    raw = raw or ""
    text = key + "\n" + raw
    assertion = is_plain_assertion(key)

    # --- missing modules / packages ---------------------------------------------------------
    m = PY_NO_MODULE.search(key)
    if m:
        mod = m.group(1)
        if ctx.py_first_party(mod):
            return None
        return _m("missing_module", f"Python module '{mod}' is not installed")
    m = PY_SHARED_LIB.search(text)
    if m:
        return _m("missing_library", f"shared library {m.group(1)} not found")
    for rx in (JS_NO_MODULE, VITE_RESOLVE):
        m = rx.search(key)
        if m:
            spec = m.group(1)
            if ctx.js_first_party(spec):
                return None
            return _m("missing_module", f"package '{ctx._pkg_name(spec)}' is not installed")
    m = GO_NO_MODULE.search(text)
    if m:
        pkg = next(g for g in m.groups() if g)
        return _m("missing_module", f"Go module providing {pkg} is not available")
    m = RUBY_LOAD.search(key)
    if m and not m.group(1).startswith((".", "/")):
        return _m("missing_module", f"Ruby library '{m.group(1)}' is not installed")

    if assertion:
        # Never call a plain assertion failure environmental.
        return None

    # --- missing executables --------------------------------------------------------------
    for rx in EXEC_PATTERNS:
        m = rx.search(key)
        if m:
            name = m.group(1) if m.groups() and m.group(1) else "a required executable"
            return _m("missing_executable", f"executable {name} not found")
    m = PY_FILE_NOT_FOUND.search(key)
    if m:
        path = m.group(1)
        if "/" not in path and "\\" not in path and re.search(r"subprocess|Popen|_execute_child|spawn", raw):
            return _m("missing_executable", f"executable {path} not found")

    # --- env vars -------------------------------------------------------------------------
    m = PY_KEYERROR_RE.search(key)
    if m and ctx.unset(m.group(1)) and (re.search(r"environ|getenv", raw) or ENV_SUFFIX_RE.search(m.group(1))):
        return _m("missing_env_var", f"environment variable {m.group(1)} is not set")
    for m in ENV_PHRASE_RE.finditer(key):
        name = m.group(1) or m.group(2)
        if name and ctx.unset(name):
            return _m("missing_env_var", f"environment variable {name} is not set")
    if JS_UNDEFINED_RE.search(key):
        name = _js_env_from_source(raw, ctx)
        if name:
            return _m("missing_env_var", f"environment variable {name} is not set")

    # --- services / network ---------------------------------------------------------------
    if DNS_RE.search(key):
        return _m("dns_failure", "host name could not be resolved")
    if SERVICE_RE.search(key):
        return _m("service_unavailable", _service_detail(key))
    if TIMEOUT_RE.search(key):
        return _m("service_timeout", "connection to a service timed out")
    if CREDS_RE.search(key):
        return _m("missing_credentials", "cloud credentials are not configured")
    if DISPLAY_RE.search(key):
        return _m("missing_display", "no display available")
    if RESOURCE_RE.search(key):
        return _m("resource_exhausted", one(RESOURCE_RE.search(key).group(0)))

    # --- missing config / credential files ------------------------------------------------
    for rx in (PY_FILE_NOT_FOUND, NODE_ENOENT_OPEN, GO_OPEN_MISSING):
        m = rx.search(key)
        if m and CONFIG_NAME_RE.search(m.group(1)):
            return _m("missing_config", f"config/credential file {m.group(1)} is missing")
    m = PERM_RE.search(key)
    if m:
        path = next(g for g in m.groups() if g)
        if CONFIG_NAME_RE.search(path):
            return _m("permission_denied", f"no permission to read {path}")
    return None


def one(s: str) -> str:
    return " ".join(s.split())


def _service_detail(key: str) -> str:
    m = re.search(r"((?:\d{1,3}\.){3}\d{1,3}|localhost|\[[0-9a-f:]+\])[:.]?(\d{2,5}|<PORT>)?", key)
    if m:
        where = m.group(1) + (":" + m.group(2) if m.group(2) else "")
        return f"service at {where} is not reachable (connection refused)"
    return "a required service is not reachable (connection refused)"


def _js_env_from_source(raw: str, ctx: EnvContext) -> Optional[str]:
    """TypeError/ReferenceError whose top in-repo frame reads an unset process.env.X."""
    for fm in JS_FRAME_RE.finditer(raw):
        path, line = fm.group(1), int(fm.group(2))
        if "node_modules" in path:
            continue
        src = ctx.source_reader(path, line)
        if not src:
            continue
        for em in JS_ENV_SRC_RE.finditer(src):
            name = em.group(1) or em.group(2)
            if name and ctx.unset(name):
                return name
        return None  # only the first in-repo frame counts
    return None
