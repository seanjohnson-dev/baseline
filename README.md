# Baseline

For coding agents: when a test fails after your change, Baseline answers "did I break this, or was it
already broken?", per failing test, with the evidence.

## The problem

When a coding agent hits a failing test, "pre-existing" is the convenient answer, and it is often given
without anyone checking the base commit.

- A thread on r/ClaudeCode titled "Claude has used 'pre-existing' 712 times in 30 days". We could not open
  Reddit directly; the title and its figures (about 214 points and 131 comments, May 2026) come from a
  search-result snapshot.
- A dev.to post by jeff_pdc, 16 Oct 2026, an opinion and experience piece on an agent that calls a failing
  test pre-existing and merges anyway:
  <https://dev.to/jeff_pdc/when-the-agent-calls-the-failing-test-pre-existing-and-merges-anyway-4me8>
- [anthropics/claude-code#99078](https://github.com/anthropics/claude-code/issues/99078) (2 Oct 2026): the
  model called a security gap pre-existing without checking.
- [anthropics/claude-code#96463](https://github.com/anthropics/claude-code/issues/96463) (23 Sep 2026): the
  model reported a regression it had introduced itself as pre-existing.

Some people respond by writing hooks that block the word "pre-existing". That stops the word, not the
guess. The answer exists: run the failing test on the commit the change started from. It is tedious to do
by hand (a second checkout, dependencies, several runs because some tests are flaky), so it gets skipped.
Baseline does it.

## What it does

`baseline check` runs your test command, and for every failing test gives one verdict:

| verdict | meaning |
|---|---|
| NEW | passes on the base commit, fails with your change: you broke it |
| PRE-EXISTING | fails on the base commit with the same error |
| PRE-EXISTING (error changed) | failed on base too, but with a different error now: check you didn't make it worse |
| FLAKY | passed and failed on the same code |
| ENVIRONMENT | fails on base and head for an environment reason (missing module, env var, service down) |
| UNKNOWN | no trustworthy base result; never to be reported as pre-existing |

```
baseline check -- python -m pytest -q
```

Without a command after `--` it detects the runner (`baseline detect` shows what it found). The base is
the merge-base of HEAD with the default branch (HEAD itself when you are on it, so uncommitted changes
are compared with the last commit), or `--base REF`.

Each verdict carries its evidence: pass/fail counts on each side, the head error and, where relevant, the
base error, the base and head shas, and whether the tree was dirty. `--json` prints the same as JSON, and
the last report is always written to `.git/baseline/last.json`. Exit code 0: no NEW failure; 1: at least
one NEW; 2: usage or tool error; 3: the base result could not be established.

## Example

Real output, from the Python benchmark fixture (`bench/`) with the change committed on a branch:

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

## Install

### As a Claude Code plugin

The repository is not published yet; once it is, with `<owner>/<repo>` standing for its GitHub path:

```
claude plugin marketplace add <owner>/<repo>
claude plugin install baseline@baseline
```

From a local clone, for one session or to install:

```
claude --plugin-dir /path/to/baseline
claude plugin marketplace add /path/to/baseline && claude plugin install baseline@baseline
```

### As a command-line tool

Python 3.9+ and git; no other dependencies. Put `bin/` on your `PATH` (or call `bin/baseline` directly,
or `python3 -m baseline` from the repo root):

```
export PATH="/path/to/baseline/bin:$PATH"
baseline version
```

Runners read per test: pytest, vitest, jest, `node --test`, go test, cargo test, rspec (each needs its own
runner installed, as for your normal test run). Checks read per error: `tsc` (and `npm run typecheck`-style
scripts that run it), eslint, `prettier --check`, `next build`. Any other command is one unit for the whole
command: NEW if it passes on base and fails on head, UNKNOWN (exit 3) if it fails on both sides.

## Claude Code plugin

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

The plugin also has a `failing-tests` skill (the verdict vocabulary and when to run the check) and a
`/baseline [options] [-- <test command>]` command. Details: [docs/PLUGIN.md](docs/PLUGIN.md).

## With push-guard

[push-guard](https://github.com/seanjohnson-dev/push-guard) is a Claude Code plugin that runs `npm test`
before Claude runs `git push` and blocks the push if a test fails. Its next version, 0.5.0 (in progress,
not released yet), uses Baseline when it is installed, so only failures the push caused block it. From
the 0.5.0 README:

| the push's tests | without Baseline | with Baseline |
|---|---|---|
| all pass | goes ahead | goes ahead |
| a NEW failure (incl. a test file that no longer loads) | blocked | blocked, each NEW failure listed with its evidence |
| only failures that were already there (pre-existing, error changed, flaky, environment) | blocked | goes ahead, with a short list of what is still red and why |
| NEW and old failures together | blocked, all listed | blocked, only the NEW ones listed |
| Baseline can't say (exit 2 or 3, UNKNOWN, whole-command verdict, timeout) | blocked | blocked, with one line saying why Baseline wasn't used |

push-guard runs `baseline check --json --base <B> -- npm test`, with `<B>` where the pushed branch left its
upstream. It finds Baseline through `$BASELINE_BIN`, then `baseline` on `PATH`, then an installed
`baseline@<marketplace>` plugin, and checks that `baseline version` answers within 5 seconds. Its option
`baseline: off` turns this off.

## How it works

1. Run the test command on the working tree, with machine-readable reporting added (JUnit XML,
   `go test -json`, and so on). No failures: done.
2. Rerun only the failing tests on the working tree, up to 5 runs each; a test that passes once is FLAKY.
3. Create a temporary detached `git worktree` of the base commit in the system temp dir (your tree, index,
   stash and branches are never touched), mirror `node_modules`, and run the failing tests there 5 to 10
   times. Head and base run in parallel; if either side shows signs of a collision (port in use, database
   locked) that round is redone serially.
4. Compare normalised errors: paths, line numbers, durations, addresses, temp names and ports are
   stripped; assertion values are kept, so `assert 5 == 7` vs `assert 6 == 7` is "error changed".
5. Load errors (a test file that fails to import, a package that fails to compile) are failures in their
   own right, listed with the tests they block.
6. Flaky detection: a test that passed and failed on the same side in any of the runs is FLAKY, never NEW.
   A verdict that a coin-flip flaky test could fake (NEW although nothing the test imports changed) gets
   up to 15 runs per side.
7. Environment classes (missing module, executable, env var or config; service unreachable) make a
   failure ENVIRONMENT only when it fails that way on both sides.
8. Base results are cached per base sha, command and environment, so the next check only reruns head.

The rules, in full: [docs/VERDICTS.md](docs/VERDICTS.md).

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
  of 60; we have not checked whether that is its vitest parsing or our id matching). With one run per side, it called a flaky test new in 24 of 120 trials.
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
deterministic cases are expected and are not a measure of real-world accuracy. What they do show: whether
each case is handled at all, how often a flaky test is caught or mistaken for a new failure over repeated
trials, and what each tool costs on a 13-test suite. They do not cover large suites, monorepos,
order-dependent tests or shared external state. The rivals ran through a harness we wrote; flake-or-fault
and preexisting-proof are meant to be driven by an agent, which may use them differently. The machine
(8 cores) had another job running, and vitest pushes the load up by itself: the 1-minute load average,
taken before and after each tool/ecosystem/mode run, was between 0.29 and 10.40 (`bench/results/load.txt`
and per-run values in the scorecard). New cases are welcome as pull requests.

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
takes minutes will take several times that.

In the fresh benchmark run above, on a busier machine, the medians were 2.16 s (py), 7.96 s (js) and
2.7 s (go) per check (`bench/results/scorecard.md`).

## Limits

What it can't do, or does only partly:

- It needs git and a base commit to compare with. Outside a git repository it exits 2; in a repository
  with no commits yet it exits 3 ("no base to compare against").
- Tests that depend on shared external state (a database, a service, files outside the repo) see the same
  state from both sides, and head and base run in parallel by default. Collisions that print a known sign
  (port in use, database locked, lock busy) are redone serially; silent ones (two runs rewriting the same
  fixed temp file) are not caught. Use `--serial` for such suites.
- Per-test verdicts need pytest, vitest, jest (also `react-scripts test`, `craco test`), `node --test`, go
  test, cargo test or rspec; per-error verdicts need tsc, eslint, `prettier --check` or `next build`.
  Anything else (`make test`, `tox`, mocha, a shell chain like `node --test; npm run typecheck`) is one
  unit: NEW when base passes, or when every head run prints a `FAIL <file>` line that base doesn't;
  otherwise UNKNOWN when base fails too (exit 3), never PRE-EXISTING or ENVIRONMENT, even if the output
  contains environment-looking errors. Its `FAIL` / assertion lines and summary are shown for both sides
  but not compared per test. For those the plugin's hook only adds a one-line note. cargo and rspec are
  tested on captured reports, not end to end; jest end to end through a stand-in that writes jest's report
  and on one real Create React App suite.
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
  whole-command failures.
- PRE-EXISTING (error changed) is not a clean bill: the test was failing before, and the change altered how
  it fails. It exits 0 unless you pass `--strict-changed`.
- A test that already fails on base for one reason hides anything else the change breaks in it: if the
  error stays the same, it is PRE-EXISTING.
- A flaky test that passes in every run that happened is not seen (`--flaky-hunt N` looks harder). A bug
  the change introduces that fails only some of the time is reported FLAKY, not NEW.
- The base worktree gets your `node_modules` mirrored and your active Python environment; nothing is
  installed or built for the base commit. If the change edits dependencies, base runs with the new ones.
  Editable Python installs of the repo are detected and handled; other install modes may make the base
  run import head's code.
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
  to 15 each when escalated), with your permissions. Tests with side effects happen several times. There is
  no sandbox and no network isolation.
- The plugin's hook re-runs with Claude Code's environment plus what the shell line itself sets; exports
  from earlier Bash calls are not seen. One test command per shell line.
- Tests that depend on where the checkout lives (its path or directory name) fail in the base worktree,
  which sits in the system temp dir under the name `wt`; see the wasitme trial below.
- The collision guard has false alarms: a suite whose normal output contains a collision sign such as
  `File exists` runs serially after the first round. That costs time, not accuracy.
- Windows is best effort: no process-group kill, symlinks need rights.

What has not been tested: real-world repositories. Everything above was measured on the synthetic
fixtures in `bench/` and on this project's own test suite. We did try it on two small real open-source Python projects that were on this machine, in throwaway
clones, with bugs we introduced (results not in `bench/`; one run each):

- preexisting-proof (30 pytest tests): one bug committed as the "already broken" base, one more left
  uncommitted. Result: 1 NEW and 1 PRE-EXISTING, both right, 3.6 s.
- wasitme (10 pytest tests): one uncommitted bug. Result: the 2 failing tests NEW, right, in 14.1 s,
  because its tests run the tool as a subprocess, so the import analysis saw no link from the tests to the
  changed file and escalated to 15 runs per side. It also listed `test_plugin_json_complete` under
  "fixed": that test asserts that the checkout directory has the project's name, so it fails in the base
  worktree (named `wt`). A test like that, failing on head too, could be misjudged the same way.
- In both, the collision guard raised a false alarm (`File exists` in the suites' own expected output) and
  the rest of the check ran serially: slower, same verdicts.
- [reader3](https://github.com/karpathy/reader3) has no tests; `baseline detect` says so and exits 2.

No large suite, monorepo, JavaScript or Go project outside the fixtures has been tried.

## Licence and name

MIT, see [LICENSE](LICENSE). "Baseline" is a working name; `python3 scripts/rename.py <newname>` renames
the package, launcher, plugin, docs and benchmark files in one command.
