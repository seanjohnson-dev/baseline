# Verdicts and how they are decided

`baseline check` answers, for every test that fails on HEAD (the working tree):
*did this change break it, or was it already broken / flaky / environmental?*
Every verdict carries the evidence it rests on (pass/fail counts per side,
normalised and raw error text from both sides, base and head shas, `dirty`)
and a one-sentence reason an agent can paste into its report.

| verdict (text) | JSON string | meaning | exit code |
|---|---|---|---|
| NEW | `new` | passes on base, fails on head (or the test/file is new and fails) | 1 |
| PRE-EXISTING | `pre_existing` | fails on base with the same normalised error | 0 |
| PRE-EXISTING (error changed) | `pre_existing_error_changed` | fails on base too, but with a different error — *still failing, check you didn't make it worse* | 0 (1 with `--strict-changed`) |
| FLAKY | `flaky` | passed and failed on the same code | 0 |
| ENVIRONMENT | `environment` | fails on base **and** head with an environment-class error | 0 |
| UNKNOWN | `unknown` | no trustworthy base result — **must never be reported as pre-existing** | 3 (unless a NEW forces 1) |

Exit codes: `0` no NEW failure · `1` at least one NEW failure (incl. NEW load
errors) · `2` tool/usage error (bad option, not a git repo, test command not
found, head run timed out) · `3` the base result could not be established
(cannot create the worktree, base run crashed or collected nothing, base
untrusted for some failure) — exit 3 is never a "pre-existing" claim.
`130`/`128+n` when interrupted by a signal (worktree is still removed).

Fixed tests (fail on every base run, pass on every head run) are listed under
`fixed`. Candidates come from the full base run and are rerun on both sides
(`--runs` each; 2× when the change's effect on them can't be analysed; 3× when
the change touched nothing they depend on — such a test can't have been fixed
by it, so it is most likely flaky). Candidates that pass on base or fail on
head go to `flaky_passing` instead; tests on base that no longer exist on head are listed under `missing`
(informational); passing tests are only counted.

## Units

A *unit* is what can fail:

* a test (`tests/test_cart.py::test_x`, `test/cart.test.js > suite > name`,
  `example.com/shop/cart::TestX`, …), or
* a **load error** (`kind: load_error`): a test file/package that could not be
  collected/compiled at all — pytest collection errors (junit `<error>` with
  "collection failure"), vitest/jest "suite failed to run", Go build/vet/setup
  failures (package id), cargo compile errors, rspec "error while loading".
  These are first-class failures and are never dropped. The tests inside a
  file that can't load are "not run" on head; a NEW load error lists them as
  `blocked_tests` (names taken from the base run).
* if the runner itself crashes before writing any report, the whole session
  is one load-error unit, `(test session)`.

## Sampling (how many runs a verdict rests on)

Runs are batched: each *round* is one invocation of the runner on one side
with every unit that still needs evidence, so cost grows with the number of
rounds, not the number of failing tests.

1. **Head, full run** with machine-readable reporting injected. Failing units = F.
   F empty → exit 0, "all passed".
2. **Head reruns** of F only, until a unit passes once (⇒ FLAKY, stop) or has
   failed `--runs` times (default 5, the full run counting).
3. **Base**: a temporary detached worktree at the base commit. If the head run
   took ≤ 60 s (`--full-base auto`), the full suite runs once on base (this
   gives `fixed`, `missing` and the `blocked_tests` names; it doubles as the
   first base sample). Then F is rerun on base:
   * **required** runs: `--runs` (5) per unit, early stop as soon as a unit
     has both passed and failed on base;
   * **optional confirmation** runs up to 2× `--runs` (10). They ride along for
     free in rounds that happen anyway; extra rounds just for confirmation
     are only spent while they add at most ~25 % to the elapsed time (min 1 s).
   * a NEW candidate whose dependencies can't be analysed gets the
     confirmation runs as required.
4. **Escalation**: a deterministic-looking verdict that a coin-flip flaky test
   could fake is *suspicious* and gets up to 3× `--runs` (15) on both sides:
   * NEW or error-changed although nothing the test depends on changed
     (static import/package closure analysis of the test vs. `git diff`;
     any changed config/lockfile/data file counts as "everything changed"), or
   * the failure text varies between runs of the same code.
   Closure analysis only ever adds runs; it never decides a verdict.
5. Base results are cached under `.git/<name>/cache/<base-sha>-<hash>.json`
   (hash of tool version, runner command, project dir, environment
   fingerprint, installed-deps markers). Cached observations count as base
   samples; only trusted results are stored; `--no-cache` disables it.

With the defaults a failing test gets 5 head runs and 5–10 base runs. Under
the worst case for flakiness (a test that fails 50 % of the time and failed
in the first run), the chance of it being labelled NEW is (1/2)^(4+5) ≈ 0.2 %
with only the required runs, (1/2)^(4+9) ≈ 0.01 % with confirmation, and
~(1/2)^28 when the change didn't touch its dependencies (escalation). The
same numbers bound a coin-flip flaky test being labelled PRE-EXISTING. Real
flaky tests usually fail far less often, and the head reruns alone catch them.

## Decision (per unit, in this order)

1. **Flaky on head** — at least one head pass and one head fail ⇒ **FLAKY**,
   regardless of base. Never NEW.
2. **Base untrusted** (worktree failed, base run crashed/timed out, base
   collected zero tests although head collected some, a base rerun produced
   no results) ⇒ **UNKNOWN**.
3. **Flaky on base** — base both passed and failed while head failed every
   run ⇒ **FLAKY** (`flaky_side: base`): can't be attributed to the change.
4. **Passes on base** (every base run) ⇒ **NEW**. If the head error is
   environment-class, `env_suspect: true` (the change probably added a
   dependency/requirement the environment lacks).
5. **Fails on base too** — compare the normalised errors:
   * same error (any head key equals any base key; if a side's own text varies
     run to run, numbers are masked before comparing) ⇒ **PRE-EXISTING**, or
     **ENVIRONMENT** when both sides' errors are environment-class;
   * different errors, both environment-class ⇒ **ENVIRONMENT**;
   * base env-class but head not ⇒ **UNKNOWN** (base couldn't really run:
     e.g. an ignored `.env` that the worktree doesn't have — see `--copy-ignored`);
   * otherwise ⇒ **PRE-EXISTING (error changed)**, with a `similarity` score.
     A test whose file failed to load on base counts as failing on base with
     the error `[file failed to load] …`.
6. **Never ran on base**:
   * file/package doesn't exist at base, or the test name isn't in the base
     sources ⇒ **NEW** (introduced by this change);
   * a load-error file that exists on base and produced no error there ⇒ **NEW**;
   * skipped on base, or exists but didn't run ⇒ **UNKNOWN**.

Load errors follow the same rules with "loads fine" in place of "passes"
(a file that loads on base and fails to load on head is a NEW load error).

## Error normalisation

Compared text = exception type + message + assertion text (pytest: junit
`message` + exception type from the traceback; jest/vitest: the failure
message up to the stack; Go: the test's own output lines; cargo: the panic
message), **not** the stack. Normalised: ANSI codes, absolute repo/worktree/
temp path prefixes (repo-relative paths are kept), `file:line[:col]` and
`File "...", line N`, `0x…` addresses, durations (`in 0.12s`, `3ms`,
`(0.00s)`), timestamps and clock times, UUIDs, temp names (`tmpab12cd3`,
`pytest-of-*/pytest-N/...`), ephemeral ports (≥ 32768) after a host, `pid N`,
goroutine ids, whitespace. Assertion values are kept: `assert 1.57 == 1.58`
vs `assert 1.56 == 1.58` is an *error changed*.

There is deliberately no fuzzy `--similarity` matching: any fuzzy rule can
turn a real change into a false PRE-EXISTING. The `similarity` ratio is
reported for error-changed verdicts as information only.

## Environment classes

Narrow, per-language patterns; a plain assertion failure (`assert …`,
`expected … to …`) is never environmental:

* `missing_module` — Python `No module named 'x'`, JS `Cannot find package/module 'x'`
  / vite `Failed to resolve import`, Go `no required module provides package`,
  Ruby `cannot load such file`; only when *x* is third-party (a module that
  exists in the repo, or a relative import, is a code error).
* `missing_executable` — `executable file not found in $PATH`, `spawn x ENOENT`,
  `x: command not found`, `FileNotFoundError` from subprocess, missing Playwright browsers.
* `missing_env_var` — `KeyError: 'DB_URL'` via `os.environ`, messages like
  `DATABASE_URL not set` / `environment variable X …`, and JS
  `TypeError`/`ReferenceError` whose first in-repo frame reads `process.env.X`;
  always only if the variable really is unset in the environment the tests ran in.
* `service_unavailable` / `service_timeout` / `dns_failure` — connection refused/reset,
  `ECONNREFUSED`, `connect: connection refused`, `i/o timeout`, `ENOTFOUND`, `no such host`, …
* `missing_config` / `permission_denied` — missing or unreadable `.env*`,
  credential/secret/key/cert/config files; `missing_credentials` (cloud SDKs);
  `missing_display`; `resource_exhausted` (ENOSPC, EMFILE, ENOMEM).

## The base worktree

`git worktree add --detach <tmp>/wt <base>` in the system temp dir (hooks
disabled). The user's working tree, index, stash and branches are never
touched (no checkout/stash/reset; git runs with `GIT_OPTIONAL_LOCKS=0`). The
head run happens in the real working tree (pytest writes its `.pyc` files to
`.git/<name>/pycache`), the base run in the worktree, sequentially. The
worktree is removed in `finally` (SIGTERM/SIGHUP are turned into exceptions,
child process groups are killed), followed by `git worktree prune`; worktrees
left by a `kill -9` are removed on the next run.

* `node_modules` (root, the project dir and npm/yarn/pnpm workspace packages)
  is mirrored with per-package symlinks; workspace packages linked inside
  `node_modules` are re-pointed at the worktree's copy so base imports base code.
* Python: the active venv is used as is. An editable install of the repo
  (`.pth` / `__editable__` finder / `.egg-link` pointing into the repo) would
  make the base run import HEAD's code, so it is detected and each side gets
  its own copy of the mapped directories first on `PYTHONPATH`; absolute
  `PYTHONPATH` entries inside the repo are re-pointed at the worktree.
* Go: the module/build caches are shared; nothing else is needed.
* Untracked/ignored files (`.env`, generated files) are **not** copied by
  default (secrets). Use `--copy-ignored GLOB` (repeatable). When a base
  failure looks environmental and head's doesn't, the verdict is UNKNOWN and
  the report lists ignored files the worktree lacks.

## JSON output

`--json` prints only JSON; the same document is always written to
`.git/<name>/last.json`. Top level: `tool`, `version`, `schema` (1),
`exit_code`, `summary` (verdict counts + `tests_total/passed/failed/skipped`,
`load_errors`, `fixed`, `missing`), `base` (`sha`, `short`, `ref`, `how`),
`head` (`sha`, `short`, `dirty`), `runner` (`name`, `command`, `per_test`,
`cwd`), `policy`, `test_runs`, `cache` (`enabled`, `hit`), `duration_s`,
`results`, `fixed`, `flaky_passing`, `missing`, `warnings`, `notes`.

Each entry of `results`:

```json
{"id": "tests/test_cart.py::test_new_discount_percent", "kind": "test",
 "verdict": "new", "reason": "Passed all 10 runs on base but failed all 5 runs on head — this change broke it.",
 "head": {"runs": 5, "failed": 5, "passed": 0, "skipped": 0, "absent": 0, "cached_runs": 0,
          "error": "<normalised>", "error_raw": "<raw excerpt>", "message": "<raw short message>",
          "distinct_errors": 1},
 "base": {"...": "same shape"},
 "runs_total": 15, "env_class": null, "env_suspect": false, "flaky_side": null,
 "match": null, "similarity": 0.93, "blocked_tests": [], "closure_changed": true,
 "suspicious_escalated": false, "base_sha": "…", "head_sha": "…", "dirty": true}
```

`kind` is `test`, `load_error` or `command` (unknown runner). `match` is
`exact`/`loose` when both sides failed with the same error; `similarity` is
present for error-changed verdicts.

## Known limits

* A flaky test that passes in the first head run is not a failure and is not
  reported (if it failed in the full base run it shows up under
  `flaky_passing`, not `fixed`).
* Unknown runners: the command runs as is and only the whole command gets a
  verdict (by exit code; same exit code on base ⇒ PRE-EXISTING).
* jest/cargo/rspec support is parser-tested but not exercised end to end here;
  `missing` detection needs the full base run.
* Windows: best effort (no process-group kill, `node_modules` mirroring needs
  symlink rights, hooks path).
