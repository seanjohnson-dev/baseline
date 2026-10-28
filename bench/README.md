# Benchmark

Small synthetic fixture repos with known answers, a harness that runs a tool against them N times,
and a scorer. It compares Baseline with three other open-source tools that answer part of the same
question: wasitme, flake-or-fault and preexisting-proof.

Published results: [results/scorecard.md](results/scorecard.md) (generated), raw per-trial files in
`results/<tool>-<eco>-<mode>.json`, machine load during the run in `results/load.txt`.

## What is in it

| file | what |
|---|---|
| `fixtures/make_fixtures.py` | writes six git repos into a directory (default `bench/_work/`, git-ignored) and the ground truth into `truth/` |
| `truth/{py,js,go}.json` | the expected case per test; `truth/commands.json` the test command per ecosystem |
| `harness.py` | runs one tool on one fixture repo N times, maps its output to labels, writes `results/<tool>-<eco>-<mode>.json` |
| `score.py` | turns every result file into `results/scorecard.json` and `results/scorecard.md` |
| `run.py` | small runner: makes the fixtures if missing, runs the harness for the tools/ecosystems/modes you pick, scores |
| `run_all.sh` | the full run that produced `results/`: all four tools, 3 ecosystems x 2 modes, 10 trials, load recorded |

Per ecosystem there are two repos with the same change: `<eco>-shop` (change committed on
`feature/discounts`, base `main`) and `<eco>-shop-dirty` (`main` checked out, same change uncommitted,
base `HEAD`). Each has 13 tests, one per case below (2 FLAKY, 3 ENV, 4 PASS):

| case | what the test does |
|---|---|
| NEW | passes on base, fails after the change |
| NEW_COLLECT | the change renames a module, so the test file fails to import / the package fails to compile |
| PRE | fails on base and after the change with the same error |
| PRE_CHANGED | fails on base, still fails after the change, with a different error (`assert 5 == 7` becomes `assert 6 == 7`) |
| FLAKY x2 | a 50 % random failure, and a race between a worker with random jitter and a fixed sleep; same code on both sides |
| ENV x3 | needs a module that is not installed, needs `DATABASE_URL` (the harness unsets it), needs a service on localhost:59999 |
| PASS x4 | two tests in files the change touches, two control tests |

## Prerequisites

- `python3` (3.9+) and `git` for everything; the Python fixtures need `pytest` importable by `python`.
- JS fixtures: `node` and `npm`. `make_fixtures.py` writes a `package.json` pinning `vitest` 3.2.7 and runs
  `npm install` once, which needs network. Offline, pass an existing `node_modules` with vitest 3.2.7 in it:
  `--node-modules DIR` (or `BENCH_NODE_MODULES=DIR`) copies it, and the `package-lock.json` next to it, instead
  of installing. The published results were made that way, from a `node_modules` installed earlier with the
  same pin.
- Go fixtures: `go` (the results were made with go 1.27.3; node 20.19.2; pytest 9.2.0). preexisting-proof on Go also needs `gotestsum` on `PATH` (`~/go/bin` is added).
- Rivals: Python 3.11+ for preexisting-proof; clones under `bench/rivals/` (git-ignored) or wherever
  `RIVALS_DIR` points. The commits used for the published results:

```
git clone https://github.com/ajwann/wasitme.git bench/rivals/wasitme && git -C bench/rivals/wasitme checkout 2f426ef
git clone https://github.com/code-with-rashid/flake-or-fault.git bench/rivals/flake-or-fault && git -C bench/rivals/flake-or-fault checkout 617855c
git clone https://github.com/mozzie49/preexisting-proof.git bench/rivals/preexisting-proof && git -C bench/rivals/preexisting-proof checkout c321dfe
```

| tool | commit | commit date |
|---|---|---|
| wasitme | 2f426ef811ffb2db3040746f63681a484bba2118 | 2026-10-09 |
| flake-or-fault | 617855cfea8ad44a8cafbc6a87495ff5d4cd07d9 | 2026-10-06 |
| preexisting-proof | c321dfe9c5fd49f1d5cc970e4b89e00c030a17d8 | 2026-10-23 |

## Running it

```
python3 bench/run.py --quick                                   # Baseline, Python fixtures, 3 trials, pytest only
BENCH_RESULTS=/tmp/bench-try python3 bench/run.py --tools baseline,wasitme --ecosystems py,go --trials 2
RIVALS_DIR=/path/to/rivals bench/run_all.sh                     # what produced results/ (TRIALS=n to change)
python3 bench/score.py                                         # re-score whatever is in results/
```

`--quick` writes to `bench/_work/quick-results/` so it doesn't overwrite the published files; everything
else writes to `bench/results/` unless `BENCH_RESULTS` is set. `BENCH_WORK` moves the fixture repos,
`RIVALS_DIR` the rival clones, `BENCH_TOOL_BIN` points at a different Baseline launcher. The fixture repos
are reused between runs; delete `bench/_work/` to regenerate them.

How each tool is run (all from the fixture repo, `DATABASE_URL` unset):

- Baseline: `bin/baseline check --json --no-cache -- <test command>` (base cache off, so every trial does
  the full base work). The harness also checks that `git status`, `git worktree list`, branches and stash
  are the same before and after every trial.
- wasitme: `wasitme --fresh [--base main] -- <test command>`, text output parsed.
- flake-or-fault: `flakerun.py run` 10 times on head and 5 times on a base worktree the harness creates,
  then `flakerun.py classify --json`. This is the skill's script driven directly, without an agent.
- preexisting-proof: `preexisting.py capture` on a base worktree the harness creates and on head (JUnit
  via `--junitxml` / vitest junit reporter / gotestsum), then `preexisting.py compare --json`. Also
  without an agent.

flake-or-fault and preexisting-proof are Agent Skills meant to be driven by a coding agent; the harness
does the steps the skill tells the agent to do (make a base checkout, run the helper). The time it takes
to create the base worktree is included in their time.

## Scoring

Per trial, each of the 13 tests counts as correct or not:

- NEW: labelled new / introduced / broken-by-change and nothing else (preexisting-proof `new_failure`; also
  reported missing is fine).
- NEW_COLLECT: the test file (pytest, vitest) or package (Go) reported as a new failure.
- PRE: labelled pre-existing / `same_failure_observed_before`, nothing else.
- PRE_CHANGED: flagged as changed (Baseline `pre_existing_error_changed`, preexisting-proof
  `changed_failure`) or as new; both make the agent look at it. Labelled plain pre-existing is wrong:
  it lets the agent wave through a failure the change altered.
- FLAKY: labelled flaky. Second column FLAKY_NOT_NEW: the flaky test was not labelled new (no label,
  or any other label, is fine). A flaky test called NEW sends the agent to "fix" code that is fine.
- ENV: labelled as an environment problem, distinct from pre-existing.
- PASS: not reported at all.

Label mapping: wasitme `INTRODUCED` = new, `PRE-EXISTING` = pre, `FIXED` = fixed. flake-or-fault
`broken-by-change` = new, `pre-existing` = pre, `flaky` = flaky (`order-dependent`, `consistent-failure`,
`inconclusive` kept as their own labels, i.e. wrong for every case). preexisting-proof `new_failure` = new,
`changed_failure` = changed, `same_failure_observed_before` = pre, `now_passing` = fixed,
`missing_*`/`other_missing` = missing. Baseline's verdicts map one to one. Test ids are matched by name
token (the harness's `match()`).

"not a goal" marks a case the tool's own README does not claim: wasitme does not claim error-changed
detection, flakiness or environment classes; flake-or-fault does not claim error-changed or environment
classes; preexisting-proof does not claim flakiness or environment classes. The number is still shown.
FLAKY_NOT_NEW is never marked "not a goal": calling a flaky test new is a false alarm for any tool that
reports new failures.

Time is the median wall-clock seconds of one complete run of the tool (base checkout, base runs, head
runs, reruns), pooled over trials, ecosystems and modes in the "all" row.

## Caveats

The fixtures are small synthetic repos written by the Baseline author alongside the tool. They measure
the cases we thought of, and the tool was developed against them, so a perfect score on them is expected
and says little about real-world accuracy. They do show that each case is handled at all, how often a
flaky test is caught or mislabelled over 10 trials, and what each tool costs in time on a 13-test suite.
They do not show behaviour on large suites, monorepos, slow or order-dependent tests, tests that share
external state, or failures whose message differs only in ways our normaliser does or doesn't handle.
Rivals were run through a harness written by us, not by their authors; flake-or-fault and
preexisting-proof are skills meant to be driven by an agent, and an agent using them may do better or
worse than the scripted steps here. Timings come from one 8-core machine that had another job running
(see `results/load.txt`).

New cases are welcome: add a test to `fixtures/make_fixtures.py`, its expected case to `truth/`, and open
a PR with the re-run results.
