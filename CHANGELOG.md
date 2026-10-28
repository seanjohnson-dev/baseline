# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

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
