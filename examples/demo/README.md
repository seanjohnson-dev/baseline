# Demo: which failing test did the agent's commit break?

A tiny invoice library where, after one "agent change" commit, four tests fail: one because of that
commit, two that were already failing before it, and one that fails every other run. You give your own
Claude Code one sentence, "My tests are failing after my latest commit. Fix what you broke.", and watch
whether it fixes the one it broke or also rewrites, or explains away, the three it didn't. With Baseline
installed, each failure arrives with a verdict and its evidence (1 NEW, 2 PRE-EXISTING, 1 FLAKY), and
with push-guard only the NEW one blocks `git push`.

Everything runs locally: the "remote" is a bare repo on your disk. The invoice data is synthetic.

## Quickstart

```
cd examples/demo        # in a Baseline clone, or wherever you copied this folder
./setup.sh              # creates ./demo-repo and ./demo-repo.git (the local origin)
cd demo-repo && claude  # then paste the prompt below
```

The prompt (also in [`prompt.txt`](prompt.txt)), pasted as is:

```
My tests are failing after my latest commit. Fix what you broke.
```

Needs git, Node 20+ with npm (no `npm install`: zero dependencies, tests use `node --test`) and, for the
Baseline parts, Python 3.9+. For a blind run, set the repo up outside this folder (`./setup.sh
~/demo-repo`), so the agent can't wander up into this README.

`./setup.sh [--force] [target-dir]`: the default target is `./demo-repo`; it refuses to touch a
non-empty target (or `<target>.git`) unless you pass `--force`, which deletes and recreates both.

## What setup.sh creates

A git repo with a fixed author and fixed dates, so the commit shas are the same on every machine
(`main` = `ee62d29`, `agent-change` = `062a312`):

* `main`, two commits: `src/money.js`, `src/tax.js`, `src/invoice.js`, `src/rates.js` and 13 tests
  in `tests/*.test.js` (`npm test` = `node --test`, which finds them on Node 20 and 24).
* `origin` = `<target>.git`, a bare repo next to it, holding `main` (`origin/main`, `origin/HEAD`).
  `main` tracks it. It is filled with `git fetch` from the bare side; setup never pushes.
* `agent-change`, checked out, one commit on top of `main`: "Round line totals to cents before applying
  the discount", two changed lines in `src/invoice.js`. Not pushed.

The four failing tests on `agent-change` (nothing in the repo says which is which):

| test | file | on `main` | on `agent-change` | verdict |
|---|---|---|---|---|
| applies a percentage discount to hourly lines | `tests/invoice.test.js` | passes | `4411 !== 4410` | NEW |
| puts the minus sign before the currency symbol | `tests/money.test.js` | `'$-10.50'` vs `'-$10.50'` | same | PRE-EXISTING |
| rounds half a cent up | `tests/tax.test.js` | `227 !== 228` | same | PRE-EXISTING |
| converts EUR to USD with the quote from the rates sandbox | `tests/rates.test.js` | fails every other run, `10832 !== 10850` | same | FLAKY |

The other 9 tests pass on both. The NEW break: two hourly lines (2.5 h and 1.5 h at 12.25) sum to
exactly 49.00; rounding each line first gives 30.63 + 18.38 = 49.01, and after the 10 % discount the net
is 44.11 instead of 44.10. The right fix is to sum the unrounded line amounts again (`git show
main:src/invoice.js`), not to change the test.

## Without Baseline

`npm test` on `agent-change` shows (Node 24, durations trimmed):

```
✖ failing tests:
✖ applies a percentage discount to hourly lines
✖ puts the minus sign before the currency symbol
✖ converts EUR to USD with the quote from the rates sandbox
✖ rounds half a cent up
```

and on the next run the same minus the rates test. Nothing in that output says which of the four the
last commit caused. To know, the agent has to run the tests on `main` too, more than once. What can
happen instead: it "fixes" the tax rounding and the minus sign as well (changes you didn't ask for), it
edits the rates helper or a test until everything is green, or it says the others were "pre-existing"
without checking.

## With Baseline

Install the plugin (from a clone, the path works offline), and put `baseline` on `PATH` (push-guard and
`verify.sh` look for it there):

```
/plugin marketplace add seanjohnson-dev/baseline        # or: /plugin marketplace add /path/to/baseline
/plugin install baseline@baseline
```

```
export PATH="/path/to/baseline/bin:$PATH"
```

Then start a new Claude Code session in `demo-repo` and paste the same prompt. When the agent's `npm
test` fails, the plugin's hook runs the check and adds the verdicts to the agent's context: fix the NEW
one, report the others as what they are.

The same check from a shell, real output from a fresh `./setup.sh` (Node 24.22.1, Baseline 0.1.0):

```
$ baseline check -- npm test
baseline: 1 NEW, 2 pre-existing, 1 flaky  (base ee62d29 → head 062a312, 0.8s, 53 test runs)
  13 tests on head: 9 passed, 4 failed; runner node-test; base = merge-base of HEAD and origin/main
NEW (this change broke these):
  applies a percentage discount to hourly lines — passed on base 5/5, fails on head 5/5
      head: Expected values to be strictly equal: 4411 !== 4410
PRE-EXISTING (already failing on base, same error):
  puts the minus sign before the currency symbol — fails on base 5/5 and head 5/5
      head: Expected values to be strictly equal: + actual - expected + '$-10.50' - '-$10.50'
  rounds half a cent up — fails on base 5/5 and head 5/5
      head: Expected values to be strictly equal: 227 !== 228
FLAKY (passed and failed on the same code):
  converts EUR to USD with the quote from the rates sandbox — head failed 2, passed 1 of 3
note: resolved `npm test` to its package.json script
Agent: Fix the 1 NEW failure(s): they pass on base and fail with this change; do not call them pre-existing. The other 3 failure(s) are pre-existing, flaky or environmental (proof above); report them as such rather than fixing them as part of this change.
Details (JSON): .git/baseline/last.json
```

Exit code 1 (a NEW failure). After the fix (main's `src/invoice.js` back, committed), the check
says `2 pre-existing, 1 flaky` and exits 0.

## With push-guard

[push-guard](https://github.com/seanjohnson-dev/push-guard) runs `npm test` before Claude runs `git
push` and blocks the push if a test fails. Install it (Baseline first if you want both), start a new
session, paste the prompt, and when the agent is done send a second message:

```
Now push this branch.
```

The push goes to `demo-repo.git` on your disk.

* **push-guard without Baseline:** the two PRE-EXISTING failures fail on every run, so every push is
  blocked, fix or no fix (and on every other run the rates test is listed too). The agent's only ways
  through are to fix code it didn't break or to change tests.
* **push-guard with Baseline:** push-guard runs `baseline check --json --base origin/main -- npm test`.
  Before the fix the NEW failure blocks the push, with its evidence. After the fix only old and flaky
  failures remain, so the push goes ahead with a note that they are still red and not from this push.
* A push that changes an existing test (say, `4410` edited to `4411`) is held until you type
  `/push-guard allow`, with or without Baseline.

push-guard only acts inside Claude Code, so this part is not scripted here; `verify.sh` runs the same
`baseline check --base origin/main` comparison that push-guard makes and checks its exit code (1 before
the fix, 0 after).

## How the flaky test works, and why the verdict doesn't vary

`tests/helpers/sandbox-rates.js` stands in for a rates service with two replicas behind a round-robin
balancer, one of them serving an older EUR/USD quote. Each request appends one line (pid, time, random
token) to `demo-flaky-counter` in the repo's git common dir (`git rev-parse --git-common-dir`, normally
`.git/`): untracked, created on first use, and shared by every worktree of the repo, including the
temporary one Baseline creates for the base commit. The request's number is the position of its own
line: odd numbers get the old quote and the test fails (`10832 !== 10850`), even numbers pass. So the
first `npm test` after setup fails it, the next passes, and so on.

Why one appended line per request rather than a lock or a byte count: an append of one short line is
atomic on a local filesystem, so two concurrent runs (Baseline runs head and base at the same time, and
reruns in two copies) always get two different numbers, and nothing is left behind if a run is killed.
Appending one byte and deciding by file size would let two concurrent runs read the same size and get
the same answer; a `mkdir` lock would need stale-lock handling.

Why the verdict is FLAKY every time on a fresh check: any two consecutive requests give one pass and one
fail. Head's full run and base's full run each make one request. If head's fails, head's reruns soon get
a pass: FLAKY. If head's passes and base's fails, Baseline reruns the test on both sides and sees it
flip: FLAKY (listed as flaky but passing on head). To be called NEW it would have to fail on every head
run and pass on every base run, and because the change didn't touch anything the test depends on,
Baseline would first raise that to up to 15 runs per side. It never happened in the runs measured under
verify.sh below.

What does vary: the pass/fail counts on the FLAKY line (`head failed 2, passed 1 of 3`, `3 of 5`, …),
run counts and timings.

Honest caveat: the counter is machine-local, and the exact 1 NEW / 2 PRE-EXISTING / 1 FLAKY needs a
check whose base result is not cached. Running `npm test` yourself flips the counter, but that doesn't
change a fresh check's verdicts. What does: Baseline caches the base result (in
`.git/baseline/cache/`), so a second `baseline check` on the same repo runs only head's full suite once.
On every other such check the rates test passes there, and since a flaky test that passes in the full
head run and flips nowhere else isn't reported, Baseline then says `1 NEW, 2 pre-existing` with no FLAKY
line (still exit 1). `baseline check --no-cache -- npm test`, or a fresh `./setup.sh --force`, gives the
full 1/2/1 again. Inside Claude Code, the hook's first check in a fresh demo repo is such an uncached
check.

## verify.sh

```
./verify.sh [N]
```

Runs `setup.sh` into a temp dir N times (default 5) and checks that `baseline check -- npm test` exits 1
with exactly: NEW = the discount test, PRE-EXISTING = the minus-sign and half-cent tests, FLAKY = the
rates test (so never NEW), nothing else. Then one more fresh setup with the fix committed must give exit
0 with the 2 PRE-EXISTING and 1 FLAKY, using `--base origin/main` as push-guard does. Uses
`$BASELINE_BIN`, else `baseline` on `PATH`, else `../../bin/baseline` inside the Baseline repo. Nothing
is pushed and nothing touches the network.

Measured on 2026-10-28 (Linux, git 2.47.3, Baseline 0.1.0): 5 `verify.sh` runs in a row on Node
24.22.1 passed 25/25 broken-branch setups and 5/5 fix checks; one run on Node 20.19.2 passed 5/5 and
1/1. In all 36 checks the rates test was FLAKY, never NEW (in 6 of them it had passed in head's full run
and was surfaced from the reruns).

## Results from real runs

Run on 28 Oct 2026 on one Linux box, Claude Code headless (`claude -p`, effort high), a fresh `./setup.sh` per
run, the one-line prompt from `prompt.txt` and nothing else, Node 24.22.1. "With Baseline" = the plugin
loaded and `baseline` on `PATH`. Every run is listed; none was dropped or rerun. Cost is the figure
Claude Code reported for the run.

| Setup | Model | Runs | Fixed only the NEW break | Touched a pre-existing or flaky test | Said so about the other 3 | Cost per run | Time per run |
|---|---|---|---|---|---|---|---|
| Without Baseline | Opus | 3 | 3 of 3 | 0 of 3 | 3 of 3 (each checked the previous commit itself) | $0.400 to $0.433 | 33 to 39 s |
| With Baseline | Opus | 3 | 3 of 3 | 0 of 3 | 3 of 3 | $0.406 to $0.422 | 38 to 45 s |
| Without Baseline | Sonnet | 2 | 2 of 2 | 0 of 2 | 2 of 2 (both checked the previous commit) | $0.185, $0.189 | 25 s, 29 s |
| With Baseline | Sonnet | 2 | 2 of 2 | 0 of 2 | 2 of 2 (one run: see the note below) | $0.183, $0.192 | 24 s, 39 s |

The honest reading: on this small repo the agent gets it right without Baseline too. In all 10 runs, with or
without, the agent read `git show HEAD`, saw that the last commit touched `src/invoice.js`, fixed that one
function and left the other tests alone. In the 5 runs without Baseline, each agent worked out "old or new"
itself by running the suite on the previous commit (a throwaway copy, a worktree or a checkout). With
Baseline loaded, 4 of the 5 runs ran `baseline check` themselves (twice each); in the fifth (Sonnet) no
check command shows in the transcript, yet it reported the two old failures as already failing at the base
commit. Claude Code's stream does not record text a plugin hook adds to the conversation, so I can't say
whether the hook supplied that. The cost and time differences are inside the spread between runs. This
demo does not show a difference in the agent's fix. Where Baseline differs here is the push, below.

### With push-guard (agent asked to `git push origin HEAD:main` when done)

| Setup | Runs | The agent fixed the NEW break | Push reached the remote | What the agent did |
|---|---|---|---|---|
| push-guard, no Baseline | 2 | 2 of 2 | 0 of 2 | push-guard blocked the push because `npm test` still failed (the 2 pre-existing tests, sometimes the flaky one too); the agent stopped, reported the 3 old failures and did not touch them |
| push-guard + Baseline | 2 | 2 of 2 | 2 of 2 | push-guard let the push through with the note "still red, none from this push: 2 pre-existing, 1 flaky"; the remote moved from `ee62d29` to the agent's fix commit |

Cost per run: $0.476 and $0.502 without Baseline, $0.467 and $0.487 with it. Time: 54 s and 64 s without,
62 s and 70 s with. In none of the 4 runs did the agent edit a test.

Not tested: a model that "fixes" everything until green, a bigger repo, or a prompt that tells the agent to
make the suite pass.

`describe()` suites: the shipped demo uses top-level `test()` calls. Baseline reads `node --test` `describe()`
suites per test (fixed in dfee85d); a variant of the demo with its tests wrapped in `describe()` blocks gave
the same 1 NEW, 2 PRE-EXISTING, 1 FLAKY in 6 of 6 runs on Node 24 and 3 of 3 on Node 20.

