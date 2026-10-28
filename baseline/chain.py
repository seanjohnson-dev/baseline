"""Chained commands (`a && b; c`, or a package.json script that is one): split into parts, judge each part
with the ordinary machinery (see engine.run_chain), combine. A part that can't be split or re-run faithfully
(`||`, pipes, subshells, `$(...)`, `&`, redirects, unbalanced quotes) is never run: it is UNKNOWN.

Never judge only the first part: a chain whose parts weren't all judged must not come out clean."""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .runners import ASSIGN, split_env

MAX_DEPTH = 5           # npm run X -> script body -> npm run Y ...
NPM_LIKE = ("npm", "yarn", "pnpm", "bun")
AND, SEMI, OR = "&&", ";", "||"
LIST_SEPS = {SEMI, "\n", ";;"}
REDIRECTS = {">", ">>", "<", "<<", "<<<", ">&", "<&", "&>", "&>>", ">|", "<>"}
NOT_SCRIPTS = {"install", "i", "ci", "add", "remove", "uninstall", "exec", "dlx", "x", "init", "create", "publish",
               "link", "pack", "update", "upgrade", "outdated", "audit", "why", "list", "ls", "config", "set"}


@dataclass
class Part:
    text: str                       # as written (shown in reports)
    argv: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[Path] = None
    problem: str = ""               # non-empty: not run, UNKNOWN
    via: str = ""                   # the npm script(s) it came from

    def command(self) -> List[str]:
        return [f"{k}={v}" for k, v in self.env.items()] + list(self.argv)


def tokenize(text: str) -> Optional[List[str]]:
    lx = shlex.shlex(text, posix=True, punctuation_chars="();<>|&\n")
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    lx.commenters = "#"
    try:
        return list(lx)
    except ValueError:  # unbalanced quotes
        return None


def has_operators(tokens: Optional[List[str]]) -> bool:
    """Top-level `&&`, `;`, `||` or a newline: a list of commands, not one command."""
    return tokens is None or any(t in (AND, OR) or t in LIST_SEPS for t in tokens)


def script_name(argv: List[str]) -> Optional[str]:
    """`npm test` / `npm run X` / `yarn X` / `pnpm X` / `bun run X` -> X (else None)."""
    if len(argv) < 2 or os.path.basename(argv[0]) not in NPM_LIKE:
        return None
    tool, rest = os.path.basename(argv[0]), argv[1:]
    while rest and rest[0].startswith("-") and rest[0] not in ("--",):
        rest = rest[1:]  # `npm --silent run x`
    if not rest:
        return None
    if rest[0] in ("run", "run-script", "run-scripts"):
        return rest[1] if len(rest) > 1 and not rest[1].startswith("-") else None
    if rest[0] in ("test", "t", "tst") and tool != "bun":
        return "test"
    if tool in ("yarn", "pnpm") and rest[0] not in NOT_SCRIPTS:
        return rest[0]
    return None


def _scripts(pkg_dir: Path) -> Dict[str, str]:
    try:
        s = json.loads((pkg_dir / "package.json").read_text()).get("scripts") or {}
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _script_args(argv: List[str], name: str) -> List[str]:
    """Words after the script name (npm appends them to the script's body)."""
    try:
        k = argv.index(name, 1) + 1
    except ValueError:
        return []
    extra = argv[k:]
    return extra[1:] if extra[:1] == ["--"] else extra


def chained_script(argv: List[str], cwd: Path) -> Optional[str]:
    """The body of the package.json script argv runs, if that body is a chain."""
    env, rest = split_env(argv)
    name = script_name(rest)
    if name is None:
        return None
    body = _scripts(cwd).get(name)
    if not isinstance(body, str) or not body.strip():
        return None
    return body if has_operators(tokenize(body)) else None


def is_chain(argv: List[str], cwd: Path) -> bool:
    """A command that is several commands: operator words in it, or an npm script whose body chains."""
    if len(argv) == 1 and " " in argv[0]:
        toks = tokenize(argv[0])
        if has_operators(toks):
            return True
        argv = toks or []
    if any(a in (AND, OR, SEMI) for a in argv):
        return True
    return chained_script(argv, cwd) is not None


def expand(argv: List[str], cwd: Path, root: Path) -> List[Part]:
    """The parts of a chained command, flattened through npm scripts (MAX_DEPTH levels)."""
    if len(argv) == 1 and " " in argv[0]:
        text = argv[0]
    else:
        text = " ".join(a if a in (AND, OR, SEMI) else shlex.quote(a) for a in argv)
    return _parse(text, cwd, root, {}, 0, "", cwd)


def _join(tokens: List[str]) -> str:
    out = []
    for t in tokens:
        plain = t in (AND, OR, SEMI, "|", "&", "(", ")") or t in REDIRECTS or \
            (" " not in t and all(c.isalnum() or c in "$=_-./:@%+,`'\"" for c in t))
        out.append(t if plain else shlex.quote(t))
    return " ".join(out).replace("$ (", "$(").replace(" \n ", "; ").strip()


def _lists(tokens: List[str]) -> List[List[str]]:
    out, cur = [], []
    for t in tokens:
        if t in LIST_SEPS:
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return [x for x in out if x]


def _problem(tokens: List[str]) -> str:
    """Why one simple command (between `&&`s) can't be re-run and read faithfully ('' if it can)."""
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("|", "|&"):
            return "its output is piped into another command (the exit status is the last command's)"
        if t == "&":
            return "it runs in the background (`&`)"
        if t in ("(", ")"):
            return "it uses a subshell or `$(...)`"
        if t in REDIRECTS:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            prv = tokens[i - 1] if i else ""
            if t == ">&" and nxt in ("1", "2") and prv in ("1", "2"):
                i += 2
                continue  # 2>&1 / 1>&2: output stays visible
            return "it redirects input or output (`" + t + "`)"
        if t.endswith("$") and tokens[i + 1:i + 2] == ["("]:
            return "it uses a subshell or `$(...)`"
        if "`" in t or "$" in t:
            return "it uses shell expansion (`$`/backticks), which can't be reproduced exactly"
        i += 1
    return ""


def _strip_dups(tokens: List[str]) -> List[str]:
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == ">&" and out and out[-1] in ("1", "2"):
            out.pop()
            i += 2
            continue
        out.append(tokens[i])
        i += 1
    return out


def _parse(text: str, cwd: Path, root: Path, env: Dict[str, str], depth: int, via: str, pkg: Path) -> List[Part]:
    tokens = tokenize(text)
    if tokens is None:
        return [Part(text.strip(), problem="unbalanced quotes: the command can't be split reliably", via=via)]
    parts: List[Part] = []
    env = dict(env)
    for lst in _lists(tokens):
        if OR in lst:
            parts.append(Part(_join(lst), problem="it uses `||`, so its exit status doesn't say whether the "
                                                  "commands before it passed", via=via, cwd=cwd))
            continue
        simple: List[List[str]] = [[]]
        for t in lst:
            if t == AND:
                simple.append([])
            else:
                simple[-1].append(t)
        for toks in simple:
            if not toks:
                continue
            shown = _join(toks)
            why = _problem(toks)
            if why:
                parts.append(Part(shown, problem=why, via=via, cwd=cwd))
                continue
            toks = _strip_dups(toks)
            assign, rest = split_env(toks)
            if not rest:
                continue  # `FOO=1` alone: a shell variable, not exported to later commands
            head = os.path.basename(rest[0])
            if head in ("true", ":"):
                continue
            if head == "export":
                for x in rest[1:]:
                    if ASSIGN.match(x):
                        k, v = x.split("=", 1)
                        env[k] = v
                continue
            if head == "cd":
                target = (cwd / os.path.expanduser(rest[1])).resolve() if len(rest) == 2 else None
                if target is None or not target.is_dir() or not _inside(target, root):
                    parts.append(Part(shown, problem="it changes directory in a way that can't be followed",
                                      via=via, cwd=cwd))
                    return parts  # the rest would run somewhere unknown
                cwd = target
                continue
            penv = {**env, **assign}
            name = script_name(rest)
            if name is not None:
                body = _scripts(cwd).get(name)
                if isinstance(body, str) and has_operators(tokenize(body)):
                    label = " ".join(rest[:3]) if rest[1:2] in (["run"], ["run-script"]) else " ".join(rest[:2])
                    if depth >= MAX_DEPTH:
                        parts.append(Part(shown, problem=f"npm scripts nested more than {MAX_DEPTH} deep",
                                          via=via, cwd=cwd))
                        continue
                    extra = _script_args(rest, name)
                    if extra:
                        body = body + " " + " ".join(shlex.quote(x) for x in extra)
                    parts += _parse(body, cwd, root, penv, depth + 1, (via + " > " if via else "") + label, cwd)
                    continue
            argv = list(rest)
            if via and "/" not in argv[0]:
                # npm puts node_modules/.bin on PATH for scripts; we run without npm
                b = pkg / "node_modules" / ".bin" / argv[0]
                if b.exists():
                    argv[0] = os.path.relpath(b, cwd)
            parts.append(Part(shown, argv, penv, cwd, via=via))
    return parts


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
