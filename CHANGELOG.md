# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-10-29

### Fixed

- A change that edits dependencies could be called "not your fault" (reported on Reddit). The base
  worktree links your `node_modules` and runs in your active Python environment, so when the change edits
  a dependency manifest or lockfile, base runs with the new dependencies. An upgrade that makes a test fail
  the same way on both commits was reported PRE-EXISTING, "No failure was caused by this change", exit 0.
  Now Baseline reads the diff between base and the working tree (committed, staged, unstaged and untracked
  files) for dependency manifests and lockfiles, by name at any depth. When one changed, every verdict
  that rests on base (PRE-EXISTING, PRE-EXISTING (error changed), ENVIRONMENT, FLAKY on base) is UNKNOWN,
  exit 3 unless a NEW forces 1, with the reason "Dependencies changed (<files>), so this can't be called
  pre-existing: base ran with the new dependencies, not the base commit's, ...". NEW stays NEW; FLAKY on
  head stays FLAKY, with a note. This holds per test, per load error, per check error and per part of a
  chained command, and on every run, also when the base result comes from the cache. On a pytest project
  whose branch bumps `greetlib==1.0` to `2.0` in `requirements.txt`: 0.1.0 said "1 pre-existing", exit 0;
  0.1.1 says "1 unknown" with the reason, exit 3.

### Added

- `deps_changed` at the top level of the JSON report (repo-relative paths, sorted, `[]` when none; the
  same list for a chain); a "Dependencies changed: ... — base ran with the new ones" line in the text
  report; the same in the "Agent:" line and in the hook's context.
- Files that count: `package.json`, `package-lock.json`, `npm-shrinkwrap.json`, `yarn.lock`,
  `pnpm-lock.yaml`, `pnpm-workspace.yaml`, `bun.lockb`, `bun.lock`; `*requirements*.txt|.in`, `.txt`/`.in`
  files in a `requirements/` directory, `*constraints*.txt`, `pyproject.toml`, `poetry.lock`, `Pipfile`,
  `Pipfile.lock`, `uv.lock`, `pdm.lock`, `setup.py`, `setup.cfg`; `Gemfile`, `Gemfile.lock`, `gems.rb`,
  `gems.locked`, `*.gemspec`. A `package.json` edit that only touches `scripts`, `description`, `keywords`,
  `author`, `contributors`, `license`, `homepage`, `repository`, `bugs` or `funding` doesn't count. Not
  counted: `go.mod`, `go.sum`, `go.work`, `Cargo.toml`, `Cargo.lock`, because each worktree builds from its
  own copy. This was checked with real offline runs: a Go module bump (v1.0.0 to v1.1.0, local file proxy)
  and a cargo git-dependency bump (tag v1 to v2, also with a shared `CARGO_TARGET_DIR`) are both NEW, with
  base passing on the old version.
- `tests/test_deps.py`, 18 tests: the detector (every file family, nested monorepo paths, files that must
  not count such as `requirements.md`, `src/package.json.bak`, `go.mod`, `Cargo.lock`; the `package.json`
  exemption; staged, unstaged, untracked and deleted manifests against a real repo); the decision table
  (every verdict with and without a dependency change, check errors, load errors, whole commands, surfaced
  flaky); report, "Agent:" and hook wording; end to end: a pytest `requirements.txt` bump on a branch
  (UNKNOWN for a changed library and for a removed one, NEW still NEW), the same repo without a dependency
  change (verdicts unchanged), a dirty tree (unstaged, staged, untracked new manifest), the base-result
  cache (second run a cache hit with the same verdict; the rule follows the diff, not the cache), a
  `package.json` + `package-lock.json` change and a scripts-only `package.json` edit with the stand-in jest,
  a chained npm script, and a Go `go.mod` bump through an offline module proxy (NEW).

### Changed

- With a dependency change, the "Agent:" line no longer says "No failure was caused by this change" and
  the hook no longer says "None of these failures was caused by the current change", also when only
  FLAKY-on-head failures are left (the new versions may be why they flip; the hook's FLAKY line says so).
- Docs: README (verdict table, Safety, How it works, Limits), `docs/VERDICTS.md` (new section "When the
  change edits dependencies", exit code 3, JSON), `SPEC.md`, `docs/PLUGIN.md`, the `failing-tests` skill.
  Local paths in `docs/live-demo.md`, `docs/STAGE2-VERIFY.md` and `SPEC.md` now read `/path/to/baseline`
  and `<research>`.
- Version 0.1.1. The base-result cache key includes the version, so 0.1.0 cache files are not reused.

No existing test asserted the old behaviour for a dependency change, so none was changed.

## [0.1.0] - 2026-10-28

Initial release. Everything below was written before the first public release.

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
- `examples/demo/`: a reproducible local demo (`setup.sh` builds a Node repo with a local bare remote whose
  last commit leaves 1 NEW, 2 pre-existing and 1 flaky failure; `prompt.txt`; `verify.sh` checks the
  verdicts on fresh setups and after the fix).

- `react-scripts test`, `craco test`, `react-app-rewired test`, `rescripts test` are the jest runner (per-test
  verdicts via `--json --outputFile`, `--watchAll=false`, reruns of failing tests only with
  `--runTestsByPath` and an escaped `-t` pattern), directly, through npx/yarn/pnpm, with a `VAR=x` prefix, or
  as an npm script that is one (a chained script is judged part by part, see Fixed).
- Auto-detection picks the project's `test` script when it runs a known runner (CRA included), detects jest
  from `jest.config.*` or a `jest` key in package.json without a direct dependency, and lists other suites
  it found (`jest.api.config.js`, `test:*` scripts) in an "also found" note.
- Hook speaks for `react-scripts test` / `craco test` (also piped to `tail`, with `CI=true` prefixes) and npm
  scripts whose body runs them.
- `--strict-changed` end-to-end test (a pytest failure whose error text changes), and regression tests for
  all of the above (`tests/test_mixed.py`, with a stand-in `react-scripts`/`jest` that writes jest's report).

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

### Changed

- Faster checks with many failing tests, same verdicts: base confirmation runs are capped at `--runs`
  (5) by default, also for NEW tests whose dependencies can't be analysed (was up to 2× `--runs`, 10);
  `--confirm-runs N` / `BASELINE_CONFIRM_RUNS=N` raises the cap. Suspicious verdicts still escalate to
  `--max-runs` (15) on both sides. Head reruns run as two concurrent copies (redone serially on any
  collision sign, never with `--serial`). On a 2,333-test jest suite: 26 NEW 48 s -> 36 s (5,002 -> 4,872
  test runs), 5 old failures incl. a flaky one 48 s -> 22 s; identical verdicts and exit codes. New test:
  `tests/test_confirm.py` (a counting stand-in jest: invocations and test runs bounded, a flaky test stays
  FLAKY).
- The warning for a `--base` outside HEAD's history now says what it means and only appears then: diverged
  branches ("`--base origin/main` is not part of this branch's history (the branches have diverged) ... use
  `--base <merge-base sha>`"), a base ahead of HEAD, or unrelated history. `tests/test_git.py`
  `test_explicit_base_not_ancestor_warns` asserted the old wording ("not an ancestor") and was updated.
- Every child runs with `CI=true` (set when unset or false-like) and stdin from /dev/null, so watch modes
  (Create React App's `npm test` without CI) and prompts can't hang a check.
- Whole commands cost fewer runs: head twice, base once when both fail (was twice each); a second base run
  only when the answer would be NEW.
- Tests updated because they asserted the old behaviour: `test_envload` (`command not found` / TS on old Node
  as a whole command on both sides: now UNKNOWN with the hint, exit 3), `test_decide` ("different env errors"
  now UNKNOWN), `test_hook` (`npx ...` keeps `npx` in the re-run command; `format_context` with an UNKNOWN
  no longer says "None of these failures was caused").
- Tests replaced because they asserted the first-part-only bug: `test_parsers.DetectionTest.
  test_npm_test_resolves_script` (`NODE_ENV=x vitest && echo` was "vitest, first part, with a note"; now a
  chain) and `test_mixed.CraDetectTest.test_from_command_forms` (`npm run t2` = `craco test && echo done`
  was jest; now a chain). New: `tests/test_chain.py`.

### Changed (tests updated because the old behaviour was the bug)

- `test_cli`: a whole command failing on both sides (same or different exit code) now expects UNKNOWN /
  exit 3 instead of PRE-EXISTING / error changed with exit 0; the all-pass and NEW wording checks follow the
  new text.
- `test_parsers`: the generic unit id is the command, and its key is the error text, not "exit code N".
- `test_hook`: `npm run build` (a `tsc` script) is now a check command for the hook, not ignored.
- The `--strict-changed` CLI test used a whole command; whole commands can no longer be "error changed".

### Fixed (bugs found by testing on real projects before this release)

- Critical: a chained command or npm script (`CI=true react-scripts test --watchAll=false && jest --config
  jest.api.config.js && npm run check:a`) was cut to its first part and reported "all tests passed; nothing
  to attribute", exit 0, so a break in a later part (an api suite) passed as clean. A chain is now split at
  top-level `&&`, `;` and newlines (quotes respected; `npm run X` / `yarn X` / `pnpm X` parts whose script is
  itself a chain are expanded, up to 5 levels; `VAR=x` prefixes, `export` and `cd <dir>` carried to the later
  parts) and EVERY part is judged on its own by the ordinary check (per test, per error or whole command),
  on head and on base, also parts the shell would skip after an earlier `&&` failure. Combined: NEW in any
  part is NEW (exit 1), else any UNKNOWN is UNKNOWN (exit 3). A part with `||`, a pipe, a subshell or
  `$(...)`, `$`/backtick expansion, `&`, a redirect (other than `2>&1`) or unbalanced quotes is not run and
  is UNKNOWN, so a chain is only clean when every part was judged. The text report lists every part
  (`[part 2: jest --config jest.api.config.js] ...`) and labels each result with its part; the JSON adds
  `runner.parts` and `part`/`part_command` on each result (see docs/VERDICTS.md). The hook does the same,
  and an agent command line with several test commands (`pytest a; pytest b`) is judged as a chain instead
  of by its first test command.

- False clean on whole commands: a command Baseline can't read per test that failed on both sides was called
  ENVIRONMENT (exit 0, "No failure was caused by this change") as soon as both outputs contained
  environment-looking text, even when head failed in suites base passes (seen on a Create React App suite
  with one test needing a missing module). A whole command is now never ENVIRONMENT, PRE-EXISTING or FLAKY
  on base: NEW when every head run prints a `FAIL <file>`-style line that no base run does (listed), else
  UNKNOWN (exit 3) with any head-only `FAIL` lines and the environment text as hints.
- ENVIRONMENT per test needs the same error on base: different errors on the two sides, both
  environment-looking, are UNKNOWN (were ENVIRONMENT). A test that fails on head and is absent from base's
  run of the same file is NEW (was UNKNOWN when its name appeared elsewhere in the base file).
- The "Agent:" line and the hook text no longer say "No failure was caused by this change" / "None of these
  failures was caused by the current change" next to an UNKNOWN or a whole-command result.
- eslint JSON is found inside surrounding notices (a Browserslist "caniuse-lite is outdated" line on stderr,
  npm warnings, banners); check commands capture stdout and stderr apart and parse stdout first. A failed
  check whose output can't be read says so ("could not be read") instead of "0 lint errors on head", and its
  unit is named by the command instead of `(command)`.
- Hook: `npx jest ...` (and `npx vitest`, `pnpm exec`, `yarn dlx`) re-runs through the launcher, so a runner
  that only exists in `node_modules/.bin` is found (it was stripped to `jest`, "not found", and the hook went
  silent). When the hook declines a failed test command it now says "No verdict: <reason>; run `baseline
  check -- <command>`" instead of nothing (command not found, no git state, internal error).
- Whole-command proof lines: `FAIL <file>`, `●`/`✕`, `Error…`/`AssertionError`, `Expected`/`Received` first and
  the final summary (`Tests: 26 failed, …`) last, before console noise from passing tests.
- `node --test` with `describe()` suites was mis-read: a failing suite was dropped from head's results and
  reported as "Missing on head (exist on base; deleted or renamed?)" although it still existed. Now each test
  inside a suite is judged on its own (id `suite > nested > test`), suites aren't counted as tests, and a
  suite-level `before`/`after` hook failure is reported on the tests of that suite.
