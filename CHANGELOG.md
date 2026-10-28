# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- Whole-command runs are no longer called PRE-EXISTING from exit codes alone. A command Baseline can't read
  per error that fails on base too is now UNKNOWN (exit 3) with "can't tell" wording, never "No failure was
  caused by this change"; "same error" and "PRE-EXISTING (error changed)" now only come from comparing the
  same unit's error text.
- `npm test` / `npm run X` without such a script is a usage error ("no such npm script", exit 2) instead of a
  pre-existing failure.
- Load and setup failures classified ENVIRONMENT when they happen on both sides: `ERR_UNKNOWN_FILE_EXTENSION`
  for TypeScript on an old Node (the reason names the node version), "requires Node >= X", `sh: 1: x: not
  found`, npm's "Missing script". A `Cannot find module` for a source file path is no longer called a
  missing package.
- Wording: proper plurals ("1 test", "1 run"); "runs" instead of "test runs" for whole commands and checks;
  the command itself instead of "(test command)"; "check passed" / "`cmd` passed" instead of "all tests
  passed" for checks and other commands.
- The plugin's hook is no longer silent for `node --test` and check commands (see Added).

### Added

- Check commands compared as error sets: `tsc` (both output forms), eslint (stylish, or JSON, which is
  requested when no `--format` was given), `prettier --check`, `next build` type errors. One unit per error
  (file + code/rule + message, no line numbers); one run per side. Five new type errors on top of two old
  ones give 5 NEW and 2 PRE-EXISTING, exit 1.
- For type checks, git-ignored generated types (`.next/types`, `next-env.d.ts`) are copied into the base
  worktree; when base fails with errors head doesn't have, a note lists them and head's errors are still
  compared one by one.
- Whole-command failures carry their first and last error lines for head and base (JSON `error_raw`, text
  report).
- `node --test` runner: TAP reporter to a file (Node 20+), nested subtests, skip/todo, error text per test,
  test files that fail to load as load errors, reruns of only the failing tests via `--test-name-pattern`.
  Detected from a `package.json` test script that runs `node --test`, or from test files importing
  `node:test` (with a note when they load TypeScript and the node on PATH is too old).
- Hook: `node --test` (and npm scripts running it) gets a verdict like other runners; check commands get
  a one-line hint naming `baseline check -- <command>` when they fail.

### Changed (tests updated because the old behaviour was the bug)

- `test_cli`: a whole command failing on both sides (same or different exit code) now expects UNKNOWN /
  exit 3 instead of PRE-EXISTING / error changed with exit 0; the all-pass and NEW wording checks follow the
  new text.
- `test_parsers`: the generic unit id is the command, and its key is the error text, not "exit code N".
- `test_hook`: `npm run build` (a `tsc` script) is now a check command for the hook, not ignored.
- The `--strict-changed` CLI test used a whole command; whole commands can no longer be "error changed".

## [0.1.0] - 2026-10-28

Initial release.

### Added

- `baseline check [-- <test command>]`: runs the tests on the working tree, reruns only the failing ones,
  runs them on the base commit in a temporary git worktree, and gives each failure a verdict: NEW,
  PRE-EXISTING, PRE-EXISTING (error changed), FLAKY, ENVIRONMENT or UNKNOWN, with pass/fail counts per
  side, both errors, base and head shas. Text report, `--json`, and `.git/baseline/last.json`.
- Base selection: merge-base of HEAD with the default branch, HEAD on the default branch, or `--base REF`;
  uncommitted changes are compared against it.
- Runner adapters with per-test results: pytest, vitest, jest, go test, cargo test, rspec. Any other
  command gets a whole-command verdict by exit code. `baseline detect` prints the detected runner.
- Load errors (a test file that fails to import, a Go package or crate that fails to compile) are
  first-class failures, listed with the tests they block.
- Error normalisation (paths, line numbers, addresses, durations, timestamps, temp names, ports, pids)
  so the same failure on both sides compares equal; assertion values are kept, so a changed value is
  "error changed", not "pre-existing".
- Environment classes (missing module, executable, env var, config or credentials; service unavailable;
  DNS; resource exhaustion) reported as ENVIRONMENT only when the failure happens on base and head.
- Sampling rules: 5 head runs and 5 to 10 base runs per failing test by default; escalation to 15 per side
  when a verdict looks deterministic but nothing the test depends on changed (static import closure
  against `git diff`), or the error text varies.
- Base results cached per base sha, runner command and environment fingerprint (`--no-cache` to skip).
- Base worktree setup: `node_modules` mirrored with per-package symlinks (workspaces included), editable
  Python installs detected so base imports base code, `--copy-ignored GLOB` for `.env` and other ignored
  files (nothing copied by default).
- Head and base runs in parallel, with a collision guard that redoes a round serially when either side
  shows port, database or lock collision signs; `--serial` / `BASELINE_SERIAL=1` to turn it off.
- Flaky detection beyond head reruns: tests that flip between any runs that happened are reported FLAKY;
  `--flaky-hunt N` adds N full head runs.
- `baseline clean` and automatic removal of temp worktrees left behind by killed runs (owner pid and
  start time recorded; a live run's dirs are never touched).
- Claude Code plugin: `PostToolUseFailure` and `PostToolUse` hooks on Bash that recognise test commands
  (also when piped or chained), run the check within a time budget and give the agent the per-test
  verdicts; `failing-tests` skill; `/baseline` command. Dedup per session, a cap on checks per session,
  no recursion, `BASELINE_HOOK=off` to disable.
- `scripts/rename.py <newname>`: the name lives only in `baseline/brand.py`; one command renames the
  package, launcher, plugin files, docs and benchmark results.
- Benchmark (`bench/`): fixture generator for Python, JavaScript and Go, ground truth, harness, scorer and
  runner; results for Baseline and three other tools (wasitme, flake-or-fault, preexisting-proof).
- Docs: `docs/VERDICTS.md` (decision rules), `docs/PLUGIN.md`, `docs/live-demo.md`,
  `docs/STAGE2-VERIFY.md`.
