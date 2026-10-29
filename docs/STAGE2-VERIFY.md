# Stage 2A verification (2026-10-28)

All numbers below were measured on this machine (8 cores) on 2026-10-28. Raw outputs (`bench.txt`,
`scorecard-*.txt`, `unittest-final.txt`, `validate.txt`, `live/`) were kept outside the repo (not published).

## Unit suite

```
$ python3 -m unittest discover -s tests
Ran 157 tests in 57.891s
OK
```

Before stage 2A: 117 tests. New: `test_hook.py` (24: a 70-case command-detection table, output
caps, recursion guard / off switch, never-crash, lean startup, trace log, dedup + session cap,
PostToolUse, a live hook run, the timeout path), `test_concurrency.py` (10: collision signs, a real
port collision between head and base redone serially, `--serial`/env switch with identical verdicts,
speculative base run cancelled, flips surfaced on base and with `--flaky-hunt` on head, base failure
not cutting head reruns short), stale-cleanup tests in `test_worktree.py` (6, incl. a SIGKILL'd child
and two live runs that must not touch each other), plugin assertions in `test_rename.py`.

## `claude plugin validate . --strict`

```
Validating marketplace manifest: /path/to/baseline/.claude-plugin/marketplace.json
ℹ Advice (does not change the verdict):
  ❯ /path/to/baseline/README.md: This README has no line that installs "baseline" from a marketplace. …
✔ Validation passed
```

`.claude-plugin/plugin.json --strict`, `skills/`, `commands/`: passed. (README is stage 2C.)

## Fixture scorecard (`scripts/verify_fixtures.py`, 10 trials × 3 ecosystems × 2 modes)

| run | wrong verdicts | false NEW on flaky | median s py / js / go (branch, dirty) |
|---|---|---|---|
| before (stage 1, `--no-cache`) | 0 | 0 | 3.32, 3.26 / 9.36, 9.76 / 3.73, 3.88 |
| after, default (`--no-cache`) | 0 | 0 | 2.15, 2.15 / 7.52, 7.41 / 2.71, 2.76 |
| after, base cache on (`--cache`) | 0 | 0 | 1.20, 1.20 / 4.22, 4.33 / 1.42, 1.36 |
| after, `--serial --no-cache` | 0 | 0 | 3.28, 3.29 / 9.62, 10.17 / 3.63, 3.87 |

Every deterministic case (NEW, NEW load error, PRE, PRE (error changed), ENV ×3, PASS ×2) got the
expected verdict in all 10 trials in each of the 18 eco/mode/variant cells, concurrent and serial
alike; the git state (`git status`, `git worktree list`) was unchanged after every trial.

### FLAKY cases: surfaced as FLAKY / silently absent (of 10), before → after

| case | before | after (no cache) | after (cache) | after (serial) |
|---|---|---|---|---|
| py branch random / timing | 4/6, 4/6 | 7/3, 4/6 | 7/3, 10/0 | 6/4, 7/3 |
| py dirty random / timing | 6/4, 6/4 | 8/2, 7/3 | 10/0, 4/6 | 6/4, 8/2 |
| js branch random / timing | 5/5, 5/5 | 6/4, 7/3 | 10/0, 10/0 | 6/4, 8/2 |
| js dirty random / timing | 5/5, 3/7 | 7/3, 7/3 | 10/0, 10/0 | 9/1, 7/3 |
| go branch random / timing | 5/5, 5/5 | 8/2, 6/4 | 10/0, 10/0 | 6/4, 7/3 |
| go dirty random / timing | 4/6, 5/5 | 8/2, 10/0 | 5/5, 10/0 | 8/2, 8/2 |
| **total** | **57/120** | **85/120** | **106/120** | **86/120** |

"Before" is the stage-1 scorecard (`--no-cache`). The cached column surfaces more because the cached
full base run often holds a failure of the flaky test, which makes it a `fixed` candidate whose
reruns then show it flipping. No flaky test was ever labelled NEW (or anything but FLAKY / absent).

## Speed (`scripts/bench_speed.py`, 10 trials each, wall clock of the whole CLI call)

Both rows were measured in one session on the same fixture copies; "before" is `git archive f6756f8`
(stage 1) run through the same script. Load average was ~0.7 at the start; vitest itself drives it
to ~5 during the JS runs.

| eco / mode | before median (max) | after median (max) | after `--serial` | after, base cached |
|---|---|---|---|---|
| py branch | 3.16 (3.64) | **2.12** (2.27) | 2.91 | 1.09 |
| py dirty | 3.16 (3.63) | **2.10** (2.76) | 3.11 | 1.19 |
| js branch | 10.01 (16.01) | **7.36** (9.77) | 9.26 | 3.91 |
| js dirty | 9.87 (11.87) | **7.24** (7.62) | 9.68 | 4.33 |
| go branch | 3.74 (3.89) | **2.76** (2.87) | 3.94 | 1.30 |
| go dirty | 3.73 (5.09) | **2.65** (2.76) | 3.77 | 1.25 |

The JS target (≤ 5 s for a cold check) was **not reached**: 7.2–7.4 s. Why, and what was tried:

* A vitest invocation costs ~0.72 s here no matter how few tests it runs (`vitest --version` alone
  0.22 s); `--pool=threads` saves ~0.08 s but changes isolation semantics, `--isolate=false`,
  `--no-file-parallelism`, `--configLoader runner` don't help; Node 20 has no compile cache.
* Every verdict needs ≥ 5 head runs and 5–10 base runs, i.e. ~13 vitest invocations for this
  fixture. Two at once take ~1.0 s, three ~1.4 s: the box is CPU-bound at ~2–2.5 invocations/s, so
  even perfect scheduling lands around 6 s. A second base worktree was estimated at ~6.8 s and not
  built.
* Going below that would mean fewer confirmation runs. Concurrency at first did exactly that (the
  optional base runs are budgeted at 25 % of elapsed time, and elapsed time shrank: 8 → 6 base runs
  per test, 6.1 s); the budget is now measured against serial-equivalent time, so verdicts rest on as
  many runs as in stage 1 (8 base runs per JS test, 10 for py/go). `--base-runs 5` trades that
  evidence for speed if someone wants it.

What made the difference: the full base run starts alongside head's full run (cancelled if head
passes), and head reruns and base rounds run concurrently, with the collision guard. Python
start-up (~60 ms), git calls and worktree creation (~30 ms) were measured and are not where the time
goes; the hook's non-test path is ~30 ms and imports no engine code.

## Live headless test

See `docs/live-demo.md`: with the plugin, the hook fired on the agent's own test runs (2.4 s, then
1.2 s with the base cached), the context reached the agent, its answers matched the verdicts (both
NEW fixed, nothing pre-existing "fixed", flaky/env left and mentioned, base sha cited), and a failing
`ls /nonexistent` produced no hook output. Found and fixed during it: PostToolUseFailure alone never
fired because agents pipe/chain test commands (now also PostToolUse), and a skill/command name clash.

## Secrets scan

`grep` over all tracked and new files for the home-directory path, the user's email, API-key/token/private-key
patterns, cloud keys and e-mail addresses (other than `noreply`/`example.invalid`): nothing. The only
local paths were in `docs/live-demo.md` (the exact command and the verbatim hook output) and in the
stage-1 `SPEC.md`; since 0.1.1 they read `/path/to/baseline` and `<research>`. `.gitignore` now also ignores `.claude/` and
`*.hooklog`.

## Known gaps

* JS cold check ~7.3 s, not ≤ 5 s (above).
* The collision guard only sees collisions that leave a sign in the output; two runs rewriting the
  same fixed temp file silently can still disturb a concurrent round (`--serial` then).
* The hook re-runs with Claude Code's environment plus what the command line sets; state from earlier
  Bash calls is not seen. One test command per shell line.
* Marketplace install was verified in an isolated config dir only (no session could run there: not
  logged in); the live sessions used `--plugin-dir`.
* Live test covered the py fixture only; JS/Go go through the same engine (scorecard above).
