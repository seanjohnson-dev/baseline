# Baseline

When a test fails after your coding agent's change, Baseline tells you, per failing test and with
evidence, whether the change broke it or it was already broken.

## In 30 seconds

Coding agents often call a failing test "pre-existing" without checking. The real answer means running
that test on the commit the change started from: a second checkout, a few reruns because some tests are
flaky, then comparing the errors. People skip it because it's tedious. Baseline does it for you.

`baseline check -- <your test command>` runs your tests, reruns only the failing ones, runs those same
tests on the base commit in a temporary git worktree outside your repo, and gives every failure one
verdict: NEW (you broke it), PRE-EXISTING, FLAKY or ENVIRONMENT. When it can't tell, it says UNKNOWN
instead of guessing. Each verdict comes with pass/fail counts on both sides, both errors and both commit
shas.

As a Claude Code plugin it does this on its own: when the agent runs a test command and it fails, a hook
adds the verdicts to the agent's context, so the agent fixes what it broke and stops guessing about the
rest.

## Install

### Claude Code plugin

At the Claude Code prompt:

```
/plugin marketplace add seanjohnson-dev/baseline
/plugin install baseline@baseline
```

Or from a shell: `claude plugin marketplace add seanjohnson-dev/baseline && claude plugin install baseline@baseline`.

This adds the hook, a `failing-tests` skill and a `/baseline` command. Python 3.9+ and git must be on
`PATH`; there is nothing else to install.

### Command line

Python 3.9+ and git, standard library only, no `pip install`. Clone the repo and put `bin/` on your
`PATH`:

```
git clone https://github.com/seanjohnson-dev/baseline
export PATH="$PWD/baseline/bin:$PATH"
baseline version
```

### No install at all

From a clone, call the launcher directly, or load the plugin for one session only:

```
/path/to/baseline/bin/baseline check -- <test command>
python3 -m baseline check -- <test command>        # from the repo root
claude --plugin-dir /path/to/baseline             # plugin for this session only
```

## Usage

```
baseline check -- python -m pytest -q             # judge every failing test
baseline check -- npx vitest run
baseline check -- npm test                        # also chained npm scripts, part by part
baseline check -- npm run typecheck               # tsc, eslint, prettier --check: error by error
baseline check                                    # detect the runner (baseline detect shows it)
baseline check --base origin/main -- go test ./...
baseline check --json -- npx jest                 # JSON on stdout
```

The base is the merge-base of HEAD with the default branch. When you are on the default branch it is
HEAD itself, so your uncommitted changes are what gets judged. `--base REF` overrides it. The last report
is always written to `.git/baseline/last.json`. In Claude Code, `/baseline [options] [-- <test command>]`
runs the same check and reports it.

## The verdicts

| verdict | meaning | exit code |
|---|---|---|
| NEW | passes on the base commit, fails with your change: you broke it | 1 |
| PRE-EXISTING | fails on the base commit with the same error | 0 |
| PRE-EXISTING (error changed) | failed on base too, but differently now: check you didn't make it worse | 0 (1 with `--strict-changed`) |
| FLAKY | passed and failed on the same code | 0 |
| ENVIRONMENT | fails on base and on head with the same environment error (missing module, env var, service down) | 0 |
| UNKNOWN | Baseline can't tell; never to be reported as pre-existing | 3 |

Exit codes: 0 no NEW failure, 1 at least one NEW, 2 usage or tool error, 3 the base result could not be
established.

Each one as it appears in the report (from the Python benchmark fixture in `bench/`, full output
[below](#full-example)):

```
NEW (this change broke these):
  tests/test_cart.py::test_new_discount_percent — passed on base 10/10, fails on head 5/5
      head: AssertionError: assert 99.9 == 90 + where 99.9 = total([(100, 1)], discount=0.1)

PRE-EXISTING (already failing on base, same error):
  tests/test_tax.py::test_pre_tax_rounding — fails on base 10/10 and head 5/5
      head: AssertionError: assert 1.57 == 1.58 + where 1.57 = tax(10.5)

PRE-EXISTING (error changed) — still failing, but differently; check you didn't make it worse:
  tests/test_shipping.py::test_pre_changed_shipping — fails on base 10/10 and head 5/5
      base: AssertionError: assert 5 == 7 + where 5 = shipping(2)
      head: AssertionError: assert 6 == 7 + where 6 = shipping(2)

FLAKY (passed and failed on the same code):
  tests/test_flaky.py::test_flaky_random — head failed 1, passed 1 of 2

ENVIRONMENT (fails on base and head for environmental reasons):
  tests/test_env.py::test_env_var — fails on base 10/10 and head 5/5
      head: KeyError: 'DATABASE_URL'
      env: environment variable DATABASE_URL is not set
```

UNKNOWN, from a throwaway repo where `./check.sh` (a script Baseline can't read per test) fails on both
commits:

```
UNKNOWN (no trustworthy base result — do NOT call these pre-existing):
  ./check.sh — fails on base 1/1 and head 2/2
      head: FAIL smoke: login page returned 500
      base: FAIL smoke: login page returned 500
      why: Can't tell: the command fails on base too and baseline can't read its output per test or error, so the exit code proves nothing either way; compare the error lines yourself (head and base below) — do not call this pre-existing or environmental.
```

The full rules are in [docs/VERDICTS.md](docs/VERDICTS.md).

## Tested on real projects

Before launch, Baseline and push-guard were run on two private projects:

- a production React app (Create React App) with 2,333 tests in its web suite and 680 in its API suite;
- a TypeScript marketing site with 5 `node:test` tests and `typecheck`, `lint` and `format:check` scripts.

Testing found real bugs, including two false cleans. Each one led to a fix with a regression test (see
[CHANGELOG.md](CHANGELOG.md)). Times are wall clock on one shared 8-core machine, so they move when other
jobs run.

| what happened | before the fix | after the fix |
|---|---|---|
| A change adds 5 new type errors on top of the 2 old ones (`npm run typecheck`) | called PRE-EXISTING, "No failure was caused by this change", exit 0 | **5 NEW** (the old one listed as pre-existing), exit 1 |
| A change adds one badly formatted file (`npm run format:check`) | PRE-EXISTING, exit 0 | **1 NEW, 1 pre-existing**, exit 1 |
| New failures on top of old ones in a whole Create React App suite: head had 2 extra failing suites that base passes | ENVIRONMENT, exit 0, "No failure was caused by this change" (a false clean) | **26 NEW** + 2 pre-existing + 1 flaky + 2 environment, exit 1 (25.1s) |
| A new lint error among 95 old ones (`eslint`) | UNKNOWN, exit 3, and the summary said "0 lint errors on head" (a Browserslist notice broke the parser) | **1 NEW, 95 pre-existing**, exit 1 (11.4s) |
| `npm run test:all` chains the web suite, an api suite and 2 check scripts; the break is only in the api part | "all 2333 tests passed ... nothing to attribute", exit 0 (only the first part was judged) | **1 NEW** in part 2, exit 1 (21.0s) |
| One broken `node:test` test committed on a branch | one verdict for the whole command, no test name | **1 NEW** with the test's name, passed on base 10/10, fails on head 5/5 |
| A TypeScript test can't load on Node 20 | PRE-EXISTING "same error" | **ENVIRONMENT**, naming Node v20.19.2 and the Node >= 22.6 it needs |
| `npm test` in a project with no `test` script | npm's missing-script error shown as a pre-existing failure | "no such npm script: 'test'", exit 2 (usage error) |
| `baseline check` with no command on the Create React App project | "could not detect a test runner", exit 2 | detected, 26 NEW, 18.1s |
| push-guard on a push that breaks only the api code (local bare remote, real headless Claude session) | pushed silently | **blocked**, naming the api test that passed 5/5 runs on base and fails 5/5 on HEAD; remote unchanged; Baseline answered in 23.9s |
| push-guard with `CI` unset (the normal case) on a project whose `npm test` starts watch mode | hung: Baseline's 10-minute limit ran out, then push-guard started a second 10-minute `npm test`; the session was cut off after about 13 minutes | **blocked in 86s** with "26 failures are new with this push" (Baseline took 70.1s of that) |
| Live Claude Code session, failing `npx jest` with the plugin installed | the hook said nothing; the agent wrote "None of this was confirmed by running anything on main" | the hook returned "1 NEW" 4.5s after the jest failure, and "26 NEW" with test names after the web run (34.2s); the agent answered "the hook says this is new" |

Speed on the 2,333-test web suite:

| check | before | after |
|---|---|---|
| fresh per-test check, 26 new failures | 61.5s (5,002 test runs) | 35.7s (4,872 test runs) |
| same suite, only old failures | 110.4s | 24.0s |

A plain run of that suite takes 9.7s, so a fresh check costs several times a plain run.

It also got things right from the start on the 680-test API suite: a new break, only old failures, an
old failure whose error changed (shown side by side), new on top of old, and an uncommitted change, in
2.6s to 7.2s per check. After every run `git status` was clean, with no leftover worktrees or temp
directories.

One thing to read carefully: on the web suite Baseline labelled some tests FLAKY ("passed and failed on
the same code") while the machine was busy with other jobs. Run alone, those tests passed 10 of 10
times. They are load-sensitive: the label was accurate, but it doesn't mean they fail at random.

Other numbers:

- Unit tests: Baseline 226 (157, then 185, 212 and 226 over four rounds of fixes, all passing);
  push-guard 165 (run with `claude plugin test`).
- Benchmark quick run (3 trials, Python): NEW 6/6, NEW_COLLECT 6/6, PRE 6/6, PRE_CHANGED 6/6, ENV 18/18,
  PASS 24/24, FLAKY-never-NEW 12/12, FLAKY detected 7/12 (58%). The last round also gave 0 wrong verdicts
  on JavaScript and Go (FLAKY-never-NEW 36/36 over all three).
- Tokens: in a small 4-pair test (same prompt, with and without the plugin, one run per cell), the runs
  with Baseline were 6-10% cheaper (single runs, within normal noise); both reached the right answer in 4
  of 4. This does not show a saving: one run per cell is too few, and without the plugin the agent got
  to the right answer anyway by checking out the parent commit itself.

## With push-guard

[push-guard](https://github.com/seanjohnson-dev/push-guard) is my earlier Claude Code plugin: it runs
the project's tests before Claude runs `git push` and blocks the push if a test fails. From 0.5.0, when
Baseline is installed, push-guard blocks only on NEW failures, the ones this push caused, and lists each
with its evidence. Old, flaky and environment failures let the push through with a short note. Whenever
Baseline can't answer clearly (UNKNOWN, exit 2 or 3, a whole-command result, a timeout), push-guard
blocks on any failure as before and says why it didn't use Baseline. Chained scripts such as `test:all`
are read part by part: a NEW failure in any part blocks.

| the push's tests | without Baseline | with Baseline |
|---|---|---|
| all pass | goes ahead | goes ahead |
| a NEW failure (incl. a test file that no longer loads) | blocked | blocked, each NEW failure listed with its evidence |
| only failures that were already there (pre-existing, error changed, flaky, environment) | blocked | goes ahead, with a short list of what is still red and why |
| NEW and old failures together | blocked, all listed | blocked, only the NEW ones listed |
| Baseline can't say | blocked | blocked, with one line saying why Baseline wasn't used |

Install order: Baseline first, then push-guard, then start a new Claude Code session. push-guard looks
for Baseline once per session (`$BASELINE_BIN`, then `baseline` on `PATH`, then an installed
`baseline@<marketplace>` plugin). Its option `baseline: off` turns this off.

## Safety: what it never does

- It never commits, stashes, resets, checks out or pushes, and never edits your working tree, index,
  stash or branches. Its own files go inside `.git/baseline/` (the last report, the base-result cache,
  and pytest's `.pyc` files).
- Your test command does run in your working tree, several times, as you. Tests with side effects happen
  several times.
- The base commit runs in a temporary detached `git worktree` in the system temp directory, outside your
  repo, with git hooks turned off. It is removed when the check ends, also on Ctrl-C, SIGTERM or SIGHUP,
  followed by `git worktree prune`. Leftovers of a killed run are cleaned up at the start of the next one,
  and `baseline clean` lists or removes them by hand.
- No network and no telemetry: Baseline itself makes no network calls. Your tests may.
- Python standard library only, no dependencies to install.
- `node_modules`: nothing is installed for the base commit. The worktree gets its own `node_modules`
  directory whose entries are symlinks to the packages in your real `node_modules` (workspace packages
  are pointed at the worktree's copy instead, and the cache folders `.vite`, `.vitest`, `.cache` and
  `.vite-temp` are not linked). So a base run that writes inside an installed package's own directory
  writes into your real `node_modules`. If the change edits dependencies, base runs with the new ones.
- Ignored and untracked files (`.env` and the like) are not copied into the worktree unless you pass
  `--copy-ignored GLOB`.

## Full example

Real output from the Python benchmark fixture (`bench/`), with the change committed on a branch:

```
$ baseline check -- python -m pytest -q -p no:cacheprovider
baseline: 2 NEW, 1 pre-existing, 1 pre-existing (error changed), 2 flaky, 3 environment  (base ca7b808 → head 5b9b2c4, 2.3s, 118 test runs)
  12 tests on head: 4 passed, 8 failed, 1 file(s)/package(s) failed to load; runner pytest; base = merge-base of HEAD and main
NEW (this change broke these):
  tests/test_cart.py::test_new_discount_percent — passed on base 10/10, fails on head 5/5
      head: AssertionError: assert 99.9 == 90 + where 99.9 = total([(100, 1)], discount=0.1)
  tests/test_report.py [load error] — loads on base 5/5, fails on head 5/5
      head: ModuleNotFoundError: No module named 'shop.report'
      blocked: tests/test_report.py::test_new_collect_summary
PRE-EXISTING (error changed) — still failing, but differently; check you didn't make it worse:
  tests/test_shipping.py::test_pre_changed_shipping — fails on base 10/10 and head 5/5
      base: AssertionError: assert 5 == 7 + where 5 = shipping(2)
      head: AssertionError: assert 6 == 7 + where 6 = shipping(2)
PRE-EXISTING (already failing on base, same error):
  tests/test_tax.py::test_pre_tax_rounding — fails on base 10/10 and head 5/5
      head: AssertionError: assert 1.57 == 1.58 + where 1.57 = tax(10.5)
ENVIRONMENT (fails on base and head for environmental reasons):
  tests/test_env.py::test_env_module — fails on base 10/10 and head 5/5
      head: ModuleNotFoundError: No module named 'psycopg2_not_installed'
      env: Python module 'psycopg2_not_installed' is not installed
  tests/test_env.py::test_env_service — fails on base 10/10 and head 5/5
      head: ConnectionRefusedError: [Errno 111] Connection refused
      env: a required service is not reachable (connection refused)
  tests/test_env.py::test_env_var — fails on base 10/10 and head 5/5
      head: KeyError: 'DATABASE_URL'
      env: environment variable DATABASE_URL is not set
FLAKY (passed and failed on the same code):
  tests/test_flaky.py::test_flaky_random — head failed 1, passed 1 of 2
  tests/test_flaky.py::test_flaky_timing — head failed 2, passed 1 of 3
Agent: Fix the 2 NEW failure(s): they pass on base and fail with this change; do not call them pre-existing. 1 failure(s) were already failing on base but now fail differently — check you didn't make them worse before calling them pre-existing. The other 6 failure(s) are pre-existing, flaky or environmental (proof above); report them as such rather than fixing them as part of this change.
Details (JSON): .git/baseline/last.json
```

## What it reads

- Per test: pytest, vitest, jest (also `react-scripts test`, `craco test`), `node --test`, go test, cargo
  test, rspec. Each needs its own runner installed, as for your normal test run.
- Per error: `tsc` (and `npm run typecheck`-style scripts that run it), eslint, `prettier --check`,
  `next build` type errors.
- Chained commands and npm scripts (`a && b; c`, also `test:all`-style scripts): split, and each part
  judged on its own. NEW in any part is NEW. A part it can't split or read safely (one with `||`, a pipe,
  `$(...)` or a redirect) is UNKNOWN, never skipped.
- Anything else (`make test`, `tox`, mocha) is one unit for the whole command: NEW when base passes, or
  when every head run prints a `FAIL <file>` line that base doesn't; otherwise UNKNOWN (exit 3) when base
  fails too. Never PRE-EXISTING or ENVIRONMENT.

## The Claude Code plugin

The plugin adds hooks on `PostToolUseFailure` and `PostToolUse` for Bash. When the agent's shell line
contains a test command (also piped, chained, after `cd`, behind `timeout`, `uv run`, `npx`, and so on)
and the output shows failing tests, the hook runs the check in that directory and gives the agent the
per-test verdicts as extra context. From the live test in [docs/live-demo.md](docs/live-demo.md)
(trimmed; the agent had run `python -m pytest -q -p no:cacheprovider 2>&1 | tail -30`):

```
[baseline] Per-test verdicts for the failing command `python -m pytest -q -p no:cacheprovider`: 2 NEW, 1 pre-existing, 1 pre-existing (error changed), 2 flaky, 3 environment. Base ae34f97 (merge-base of HEAD and main) vs head dd29d84; 12 tests, 120 test runs, 2s.

NEW — caused by the current change (2):
- tests/test_cart.py::test_new_discount_percent: AssertionError: assert 99.9 == 90 + where 99.9 = total([(100, 1)], discount=0.1) (base ae34f97: passed 10/10; head: failed 5/5)
- tests/test_report.py [file failed to load]: ModuleNotFoundError: No module named 'shop.report' (base ae34f97: loads 5/5; head: failed 5/5); 1 test(s) in it can't run
...
How to report these failures:
- Fix the NEW failures: they pass on the base commit and fail with the current changes.
- Call a failure pre-existing only if it is listed under PRE-EXISTING above, and cite base ae34f97 and its error.
- FLAKY and ENVIRONMENT failures are not caused by this change and are not yours to fix: leave them and mention them to the user in one line.
```

In that run the agent fixed both NEW failures, left the others alone, and cited the base sha and error for
each failure it called pre-existing. Two control runs without the plugin also fixed the two NEW failures on
this small fixture, comparing one run per side by hand. One saw the random flaky test pass on base and fail
twice on the branch (the pattern of a NEW failure) and put it down to "random-number luck" because of its
name; the other filed it as already failing on base, because it happened to fail in its one base run.

Bounds:

- Not a test command: the hook exits in about 30 ms (no git calls, no engine import) and prints nothing.
  Outside a git repo it also prints nothing.
- Time: the check gets 150 s (`BASELINE_HOOK_TIMEOUT`); the hook's own timeout is 180 s. On timeout the
  child processes are killed, the worktree removed, and the agent is told there is no verdict.
- No recursion: everything the check starts runs with `BASELINE_IN_HOOK` set, and the hook ignores it.
- Dedup: the same command on the same HEAD and working tree within 10 minutes reuses the earlier result;
  at most 20 checks per session (`BASELINE_HOOK_MAX_RUNS`).
- Runners that can only get a whole-command verdict (`make test`, `tox`) get a one-line note, not a check.
- Check commands (`tsc`, `npx tsc`, eslint, `prettier --check`, `next build`, and `npm|pnpm|yarn|bun run
  typecheck|type-check|lint|format:check|check|build` or any script whose body runs one of them): when they
  fail, a one-line hint naming the exact `baseline check -- <command>` to run. They are not re-run inside
  the hook (a build can take minutes). When they pass, the hook says nothing.
- Off switch: `BASELINE_HOOK=off`. Errors in the hook are swallowed; it never blocks the tool call.

Details: [docs/PLUGIN.md](docs/PLUGIN.md).

## How it works

1. Run the test command on the working tree, with machine-readable reporting added (JUnit XML,
   `go test -json`, and so on). No failures: done.
2. Rerun only the failing tests on the working tree, up to 5 runs each; a test that passes once is FLAKY.
3. Create a temporary detached `git worktree` of the base commit in the system temp dir (your tree, index,
   stash and branches are never touched), link `node_modules` (see Safety), and run the failing tests
   there 5 to 10 times. Head and base run in parallel; if either
   side shows signs of a collision (port in use, database locked) that round is redone serially.
4. Compare normalised errors: paths, line numbers, durations, addresses, temp names and ports are
   stripped; assertion values are kept, so `assert 5 == 7` vs `assert 6 == 7` is "error changed".
5. Load errors (a test file that fails to import, a package that fails to compile) are failures in their
   own right, listed with the tests they block.
6. Flaky detection: a test that passed and failed on the same side in any of the runs is FLAKY, never NEW.
   A verdict that a coin-flip flaky test could fake (NEW although nothing the test imports changed) gets
   up to 15 runs per side.
7. Environment classes (missing module, executable, env var or config; service unreachable) make a
   failure ENVIRONMENT only when it fails the same way on both sides.
8. Base results are cached per base sha, command and environment, so the next check only reruns head.

## Benchmark

`bench/` holds six small fixture repos (Python/pytest, JavaScript/vitest, Go; the change committed on a
branch, and the same change uncommitted), 13 tests each with a known answer, and a harness that runs four
tools against them: Baseline and three other open-source projects that each solve part of this problem.

- [wasitme](https://github.com/ajwann/wasitme) (commit 2f426ef, 2026-10-09) is a clean Claude Code plugin:
  it runs the test command on your tree and on the base commit in a temporary worktree, sorts failures into
  introduced / pre-existing / fixed, and has a post-failure hook. It is the fastest of the four.
- [flake-or-fault](https://github.com/code-with-rashid/flake-or-fault) (commit 617855c, 2026-10-06) is an
  Agent Skill and the only other tool here that looks for flakiness (and for order-dependent tests, which
  the fixtures don't cover); it reruns the suite many times and comes with a root-cause guide for fixing
  flaky tests.
- [preexisting-proof](https://github.com/mozzie49/preexisting-proof) (commit c321dfe, 2026-10-23) is an
  Agent Skill that keeps JUnit "receipts" of a run (command, source state, report hashes) and compares
  them; it distinguishes changed failures from unchanged ones and refuses a pre-existing classification
  when the run scope, command or declared environment differ.

Results: N = 10 trials per ecosystem and mode for every tool, same machine, one session; each cell is
correct verdicts / (trials x tests in that case) over all 3 ecosystems x 2 modes.

| tool | NEW | NEW_COLLECT | PRE | PRE_CHANGED | FLAKY | FLAKY_NOT_NEW | ENV | PASS | median s per run |
|---|---|---|---|---|---|---|---|---|---|
| Baseline | 60/60 | 60/60 | 60/60 | 60/60 | 95/120 | 120/120 | 180/180 | 240/240 | 2.7 |
| wasitme | 60/60 | 0/60 | 49/60 | 0/60 (not a goal) | 0/120 (not a goal) | 96/120 | 0/180 (not a goal) | 240/240 | 0.44 |
| flake-or-fault | 60/60 | 40/60 | 60/60 | 0/60 (not a goal) | 119/120 | 120/120 | 0/180 (not a goal) | 240/240 | 2.88 |
| preexisting-proof | 60/60 | 60/60 | 40/60 | 60/60 | 0/120 (not a goal) | 94/120 | 0/180 (not a goal) | 240/240 | 0.61 |

Median seconds per run, by ecosystem:

| tool | py | js | go |
|---|---|---|---|
| Baseline | 2.16 | 7.96 | 2.7 |
| wasitme | 0.41 | 2.26 | 0.43 |
| flake-or-fault | 2.88 | 16.34 | 2.69 |
| preexisting-proof | 0.6 | 2.53 | 0.6 |

Scoring, in short (full rules in [bench/README.md](bench/README.md) and the scorecard header): NEW and
NEW_COLLECT must be reported as new; PRE as pre-existing; PRE_CHANGED counts if flagged as changed or new,
not if called plain pre-existing; FLAKY must be labelled flaky, and FLAKY_NOT_NEW counts the trials where
a flaky test was not called new; ENV must be labelled as environment, apart from pre-existing; PASS must
not be reported. "not a goal" marks a case the tool's README does not claim to handle; the number is
shown anyway. Time is the median wall-clock seconds of one complete run of the tool.

What the misses are (per-trial labels are in `bench/results/*.json`):

- Baseline missed a flaky test in 25 of 120 trials: in those trials the test passed in every run that
  happened, so it was not reported at all. It never labelled a flaky test anything but FLAKY.
  flake-or-fault, which always runs the suite 10 times on head, caught 119 of 120 (once it said
  pre-existing). Baseline only reruns what failed; `--flaky-hunt N` adds full runs.
- wasitme does not read a file that fails to import or a package that fails to compile as a failure
  (NEW_COLLECT 0/60), labels environment failures pre-existing, and on the JavaScript fixture listed some
  tests as both fixed and introduced (the pre-existing test in 11 of 20 trials, environment tests in 34
  of 60; we have not checked whether that is its vitest parsing or our id matching). With one run per
  side, it called a flaky test new in 24 of 120 trials.
- flake-or-fault labels the error-changed test plain pre-existing, environment failures pre-existing, and
  on Go does not report the package that fails to compile (NEW_COLLECT 40/60, all 20 misses on Go).
- preexisting-proof held back the pre-existing verdict on every Go trial (`same_signature_unverified`:
  the gotestsum JUnit report's declared test/failure counts didn't match its test cases once the report
  package failed to compile), which is cautious rather than wrong; it called a flaky test new in 26 of
  120 trials and labelled environment failures pre-existing, changed or unverified.

Reproduce (prerequisites, the rival `git clone` commands and offline JS setup are in
[bench/README.md](bench/README.md)):

```
python3 bench/run.py --quick
RIVALS_DIR=/path/to/rivals bench/run_all.sh
```

Caveats. The fixtures are small synthetic repos written by Baseline's author alongside the tool, so they
measure the cases we thought of, and Baseline was developed against them; its full marks on the
deterministic cases are expected and are not a measure of real-world accuracy (for that, see
[Tested on real projects](#tested-on-real-projects)). What they do show: whether each case is handled at
all, how often a flaky test is caught or mistaken for a new failure over repeated trials, and what each
tool costs on a 13-test suite. They do not cover monorepos, order-dependent tests or shared external
state. The rivals ran through a harness we wrote; flake-or-fault and preexisting-proof are meant to be
driven by an agent, which may use them differently. The machine (8 cores) had another job running, and
vitest pushes the load up by itself: the 1-minute load average, taken before and after each
tool/ecosystem/mode run, was between 0.29 and 10.40 (`bench/results/load.txt` and per-run values in the
scorecard). New cases are welcome as pull requests.

## Speed

A check costs more than one test run: every verdict rests on several runs per side. Measured on the
benchmark fixtures (13 tests each), 10 trials, wall clock of the whole `baseline check` call, from
[docs/STAGE2-VERIFY.md](docs/STAGE2-VERIFY.md) ("before" is the first version, which ran head and base one
after the other):

| fixture | before, median (max) s | now, median (max) s | now, base cached |
|---|---|---|---|
| py branch | 3.16 (3.64) | 2.12 (2.27) | 1.09 |
| py dirty | 3.16 (3.63) | 2.10 (2.76) | 1.19 |
| js branch | 10.01 (16.01) | 7.36 (9.77) | 3.91 |
| js dirty | 9.87 (11.87) | 7.24 (7.62) | 4.33 |
| go branch | 3.74 (3.89) | 2.76 (2.87) | 1.30 |
| go dirty | 3.73 (5.09) | 2.65 (2.76) | 1.25 |

Most of the gain is from running head and base at the same time. JavaScript is still slow: one vitest
start costs about 0.7 s here and a check needs about 13 of them, so a cold check on the JS fixture takes
7.2 to 7.4 s against a target of 5 s that was not reached. `--base-runs 5` trades evidence for speed. The
cost grows with the number of rerun rounds, not the number of failing tests, but a suite whose single run
takes minutes will take several times that. On a big real suite, see the speed table under
[Tested on real projects](#tested-on-real-projects).

## Limits

What it can't do, or does only partly:

- It needs git and a base commit to compare with. Outside a git repository it exits 2; in a repository
  with no commits yet it exits 3 ("no base to compare against").
- It is slower than a plain run. A fresh per-test check on the 2,333-test web suite took 35.7s against
  9.7s for a plain run.
- Whole commands it can't read per test or per error are UNKNOWN when they fail on both sides, not
  guessed (see [What it reads](#what-it-reads)). For those the plugin's hook only adds a one-line note.
  cargo and rspec are tested on captured reports, not end to end; jest end to end through a stand-in that
  writes jest's report and on one real Create React App suite.
- Tests that depend on shared external state (a database, a service, files outside the repo) see the same
  state from both sides, and head and base run in parallel by default. Collisions that print a known sign
  (port in use, database locked, lock busy) are redone serially; silent ones (two runs rewriting the same
  fixed temp file) are not caught. Use `--serial` for such suites.
- Every command runs with `CI=true` and stdin from /dev/null, and jest gets `--watchAll=false`, so watch
  modes (Create React App's `npm test` starts one without `CI`) don't hang. A command that waits for input
  gets end-of-file.
- Auto-detection (`check`/`detect` without a command) picks the project's `test` script when it runs a
  known runner (`react-scripts test` included), else a vitest/jest dependency or config (`jest.config.*`,
  a `jest` key in package.json). Other suites it finds (a `jest.*.config.js`, `test:*` scripts) are listed
  in a note ("also found: ..."), not run: a project with a web suite and an api suite needs one check per
  suite.
- Check errors are matched by file + code/rule + message, without line numbers. An error whose message
  stays the same but moves to a different place in the same file is still PRE-EXISTING; a second copy of
  an existing error in the same file counts as NEW.
- `next build` is parsed only for its "Type error" blocks; other build failures with readable errors are
  whole-command failures. `npm run build` was not re-run on the real projects after the last round of
  fixes.
- PRE-EXISTING (error changed) is not a clean bill: the test was failing before, and the change altered how
  it fails. It exits 0 unless you pass `--strict-changed`.
- A test that already fails on base for one reason hides anything else the change breaks in it: if the
  error stays the same, it is PRE-EXISTING.
- A flaky test that passes in every run that happened is not seen (`--flaky-hunt N` looks harder). A bug
  the change introduces that fails only some of the time is reported FLAKY, not NEW. Tests that only fail
  under load are reported FLAKY too.
- The base worktree links your `node_modules` (see Safety) and uses your active Python environment;
  nothing is installed or built for the base commit. If the change edits dependencies, base runs with the
  new ones. Editable Python installs of the repo are detected and handled; other install modes may make
  the base run import head's code.
- Untracked and ignored files (`.env`, generated files) are not copied into the base worktree unless you
  pass `--copy-ignored GLOB`; a base failure that looks environmental then gives UNKNOWN, with the missing
  files listed. One exception: for `tsc` and `next build`, git-ignored generated types (`.next/types`,
  `next-env.d.ts`) are copied by default, because a fresh checkout fails the type check without them. Build
  output (`dist/`, `build/`) is never copied automatically: on base it would be head's code. When base
  still fails with errors head doesn't have, head's own errors are compared one by one anyway (an error
  base doesn't report is NEW) and a note lists base's extra errors.
- node:test: TypeScript test files need Node >= 22.6 (`--experimental-strip-types` below 23.6). On an older
  node they fail to load on both sides and are reported ENVIRONMENT with the node version (as a whole
  command, e.g. through a shell script, UNKNOWN with the node version as a hint).
- ENVIRONMENT is per test (or per load error) and needs the same error on base: a test that fails with an
  environment error only on head, or with a different one on base, is NEW or UNKNOWN.
- Monorepos and workspaces: npm/yarn/pnpm workspace packages in `node_modules` are re-pointed at the base
  copy; other layouts are not specially handled. Run it from the directory the tests run in (or `--cwd`).
- It runs your test command several times (5 times on head and 5 to 10 times on base per failing test, up
  to 15 each when escalated), with your permissions. There is no sandbox and no network isolation.
- The plugin's hook re-runs with Claude Code's environment plus what the shell line itself sets; exports
  from earlier Bash calls are not seen. Several test commands on one shell line are judged as a chain
  when they run in the same directory; in different directories the hook gives no verdict.
- Tests that depend on where the checkout lives (its path or directory name) fail in the base worktree,
  which sits in the system temp dir under the name `wt`.
- The collision guard has false alarms: a suite whose normal output contains a collision sign such as
  `File exists` runs serially after the first round. That costs time, not accuracy.
- Windows is untested (best effort: no process-group kill, symlinks need rights).

Real-world testing so far is the two private projects above plus two small open-source Python projects
with bugs we introduced (preexisting-proof, 30 pytest tests: 1 NEW and 1 PRE-EXISTING, both right; wasitme,
10 pytest tests: the 2 failing tests NEW, right). No monorepo, Go or Rust project outside the fixtures has
been tried.

## Why it exists

When a coding agent hits a failing test, "pre-existing" is the convenient answer, and it is often given
without anyone checking the base commit.

- A thread on r/ClaudeCode (May 2026) by a user who counted 712 uses of "pre-existing" across 139 sessions
  in 30 days (their count, mostly Opus 4.7):
  <https://www.reddit.com/r/ClaudeCode/comments/1t2bgg0/my_claudemd_says_every_error_is_yours_to_fix_not/>
- A dev.to post by jeff_pdc, 16 Oct 2026, on an agent that calls a failing test pre-existing and merges
  anyway:
  <https://dev.to/jeff_pdc/when-the-agent-calls-the-failing-test-pre-existing-and-merges-anyway-4me8>
- [anthropics/claude-code#99078](https://github.com/anthropics/claude-code/issues/99078) (2 Oct 2026): a
  security-relevant gap the model called pre-existing without checking the code.
- [anthropics/claude-code#96463](https://github.com/anthropics/claude-code/issues/96463) (23 Sep 2026): a
  regression the model's own fix introduced, which it then reported as a pre-existing gap.

Telling the agent not to doesn't settle it: the CLAUDE.md in that Reddit thread already said "Every error
is yours to fix - not label, not defer." A rule against the word stops the word at best, not the guess.

## Licence and name

MIT, see [LICENSE](LICENSE). The name is Baseline. It lives in one place, `baseline/brand.py`, and
`python3 scripts/rename.py <newname>` renames the package, launcher, plugin, docs and benchmark files in
one command.
