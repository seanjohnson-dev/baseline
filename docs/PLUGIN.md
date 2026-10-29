# Claude Code plugin

The repository root is the plugin (`.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`).
`claude plugin validate . --strict` passes.

| file | what |
|---|---|
| `hooks/hooks.json` | `PostToolUseFailure` and `PostToolUse` hooks on `Bash` → `${CLAUDE_PLUGIN_ROOT}/bin/baseline hook <event>` (exec form, no shell; timeout 180 s) |
| `skills/failing-tests/SKILL.md` | when a test fails / before calling a failure "pre-existing": the verdict vocabulary, how to run the check by hand, the evidence rules |
| `commands/baseline.md` | `/baseline [options] [-- <test command>]`: runs the check and reports it |

Install: `claude --plugin-dir <repo>`, or `claude plugin marketplace add <repo>` then
`claude plugin install baseline@baseline` (works offline from a local path).

## What the hook does

Both events fire on every Bash command, so the path for "not a test command" is short: no git, no
engine import (~30 ms, mostly Python start-up).

1. Read the hook JSON on stdin (`tool_input.command`, `cwd`, `session_id`, …). Exit silently if
   `BASELINE_HOOK=off`, if `BASELINE_IN_HOOK` is set (no recursion: it is set for everything the
   check starts), or on any unexpected input.
2. Find the test command in the shell line: split on `;`, `&&`, `||`, `|`, newlines and subshell
   parentheses, drop redirections, follow `cd`/`pushd`, collect `FOO=1` prefixes, `export`, `env`, and
   `source <venv>/bin/activate` (re-applied for the check), and strip wrappers (`timeout`, `time`,
   `nice`, `uv run`, `poetry run`, `npx`, `bundle exec`, …). Test commands: pytest / `python -m
   pytest|unittest`, `manage.py test`, vitest, jest, mocha, go test, cargo test/nextest, rspec,
   `npm|yarn|pnpm|bun` test scripts (by name, or by a script body that calls a runner), `make test`,
   tox, nox, and others. Lines that run the tool itself, heredocs and `$(…)` in the test command are
   ignored.
3. Skip unless inside a git repo. On `PostToolUseFailure`, skip when the output shows an all-pass
   summary; on `PostToolUse` (the shell line "succeeded", typically because of `| tail` or a
   following `; git status`), run only when a runner summary in the output reports failures.
4. If the command took more than half the budget, or the runner can't be attributed per test
   (`make test`, `tox`, …: a whole-command verdict could wrongly say "pre-existing"), return a
   one-line note instead of running anything.
5. Dedup: the same command, cwd and env prefix, with the same HEAD and working tree (hash of `git diff
   HEAD` incl. content + untracked files' size/mtime), checked in this session within 10 minutes →
   the earlier result, marked as reused. At most `BASELINE_HOOK_MAX_RUNS` (20) checks per session.
   Session state lives in `$XDG_CACHE_HOME/baseline/hook/<session>.json`.
6. Otherwise run the check in-process (`check -- <that command>` in its cwd) within
   `BASELINE_HOOK_TIMEOUT` seconds (150; the hooks.json timeout, 180, stays above it). Every
   subprocess gets the remaining budget as its timeout and runs in its own process group; when the
   budget runs out the groups are killed, the worktree is removed and the context says
   "no verdict (timed out)" without claiming anything. `.git/baseline/last.json` gets the full report.
7. Print one JSON object: `{"hookSpecificOutput": {"hookEventName": ..., "additionalContext": ...}}`.
   Nothing else ever goes to stdout; errors are swallowed (exit 0). `BASELINE_HOOK_LOG=<file>` appends
   one JSON line per invocation (event, command, seconds, context) for debugging.

The context (≤ 3000 characters, per-group caps, long lines cut) starts with
`[baseline] Per-test verdicts for the failing command …`, the counts and base → head shas (and, when
the change edits dependency manifests or lockfiles, a line naming them: base ran with the new
dependencies, so failures on both sides are UNKNOWN), then one line per test with its evidence, grouped NEW, PRE-EXISTING (error changed), UNKNOWN, PRE-EXISTING,
ENVIRONMENT, FLAKY, then reporting guidance: fix the NEW ones; call a failure pre-existing only if it
is listed so, citing the base sha and its error; FLAKY/ENVIRONMENT are not the agent's to fix (mention
them in one line); UNKNOWN means no trustworthy base result.

## Known limits

* The re-run uses the hook's environment (the one Claude Code was started with) plus what the
  command line itself sets (`FOO=1`, `export`, venv activation). State set by earlier Bash calls
  (exports in a previous command) is not seen.
* Several test commands on one shell line (`pytest a; pytest b`) are judged as a chain, each part on
  its own, when they run in the same directory; in different directories the hook gives no verdict.
* Claude Code's `--output-format stream-json` does not log hook context; `BASELINE_HOOK_LOG` does.
