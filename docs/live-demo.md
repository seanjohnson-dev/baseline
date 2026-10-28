# Live test: a headless Claude Code session with the plugin

Date: 2026-10-28 (UTC 09:30–09:38). Claude Code 2.1.300, `--model sonnet --effort low`.
Raw logs (stream-json, hook traces, metadata) are kept outside the repo in `/workspace/stage2/live/`;
everything below is trimmed from them.

## Setup

A fresh scratch copy of the research fixture `py-shop` per run: the change is committed on
`feature/discounts` (head `dd29d84`), base = merge-base with `main` (`ae34f97`). Ground truth:
`test_new_discount_percent` NEW, `tests/test_report.py` NEW (load error: the change renamed
`shop/report.py`), `test_pre_tax_rounding` PRE-EXISTING, `test_pre_changed_shipping` PRE-EXISTING
(error changed), 3 ENVIRONMENT, 2 FLAKY. PATH starts with the research venv (pytest), `DATABASE_URL`
unset. Each session ran:

```
claude -p "Run the test suite with: python -m pytest -q -p no:cacheprovider. Tell me which failing tests are caused by the working-tree/branch change and which were already failing, then fix only what you broke." \
  --model sonnet --effort low --permission-mode acceptEdits --allowedTools Bash Read Edit \
  --output-format stream-json --verbose --plugin-dir /workspace/baseline        # (control: without --plugin-dir)
```

with `BASELINE_HOOK_LOG=<file>` (one JSON line per hook invocation) from run 4 on: Claude Code's
stream-json does not record hook context, so the trace is the direct evidence that a hook fired and
what it returned.

## What happened, run by run

| run | plugin code | what the agent did | hook |
|---|---|---|---|
| 1 | commit `0157c96` (PostToolUseFailure only) | `pytest -q 2>&1 \| tail -40; git status --short; git diff --stat …` — exit 0, then built its own base worktree by hand | **did not fire**: the shell line succeeded |
| 2 | + PostToolUse | loaded `baseline:baseline` via the Skill tool, got the *command's* text (skill and command had the same name), ran `bin/baseline check` itself | not needed (the tool's own CLI is ignored) |
| 3 | + skill renamed `failing-tests` | loaded the skill, ran `bin/baseline check`, fixed, re-ran pytest | not traced |
| probe | same | `ls /nonexistent`, then `pytest -q … \| tail -3` | see below |
| 4, 5 | same (final) | ran pytest itself first | **fired** on the first pytest call and again after the fix |

Run 1 is why the plugin has a `PostToolUse` hook too: agents almost always pipe or chain the test
command, which hides its exit code. Run 2 is why the skill is called `failing-tests`.

## (1) The hook fired and its context reached the agent

Trace of run 5 (`plugin5.hooklog`):

```
PostToolUse  2.464s  2346 chars  'python -m pytest -q -p no:cacheprovider 2>&1 | tail -30'
PostToolUse  0.000s     0 chars  'git show --stat HEAD | tail -15; ls shop tests; cat shop/cart.py; …'
PostToolUse  0.000s     0 chars  'git diff ae34f97 HEAD -- shop/cart.py shop/shipping.py; cat tests/…'
PostToolUse  1.154s  1956 chars  "git show ae34f97:shop/cart.py > shop/cart.py && printf '…' > shop/report.py && python -m pytest -q … | tail -15"
```

The context returned for the first call (verbatim):

```
[baseline] Per-test verdicts for the failing command `python -m pytest -q -p no:cacheprovider`: 2 NEW, 1 pre-existing, 1 pre-existing (error changed), 2 flaky, 3 environment. Base ae34f97 (merge-base of HEAD and main) vs head dd29d84; 12 tests, 120 test runs, 2s.

NEW — caused by the current change (2):
- tests/test_cart.py::test_new_discount_percent: AssertionError: assert 99.9 == 90 + where 99.9 = total([(100, 1)], discount=0.1) (base ae34f97: passed 10/10; head: failed 5/5)
- tests/test_report.py [file failed to load]: ModuleNotFoundError: No module named 'shop.report' (base ae34f97: loads 5/5; head: failed 5/5); 1 test(s) in it can't run
PRE-EXISTING, ERROR CHANGED — failed on base too, but with a different error now; check the change did not make it worse (1):
- tests/test_shipping.py::test_pre_changed_shipping: base ae34f97: AssertionError: assert 5 == 7 + where 5 = shipping(2) | head now: AssertionError: assert 6 == 7 + where 6 = shipping(2)
PRE-EXISTING — already failing on the base commit ae34f97 with the same error (1):
- tests/test_tax.py::test_pre_tax_rounding: base ae34f97 error: AssertionError: assert 1.57 == 1.58 + where 1.57 = tax(10.5) (failed base 10/10, head 5/5)
ENVIRONMENT — fails on base and head for an environmental reason (3):
- tests/test_env.py::test_env_module: Python module 'psycopg2_not_installed' is not installed (fails on base 10/10 and head 5/5)
- tests/test_env.py::test_env_service: a required service is not reachable (connection refused) (fails on base 10/10 and head 5/5)
- tests/test_env.py::test_env_var: environment variable DATABASE_URL is not set (fails on base 10/10 and head 5/5)
FLAKY — passed and failed on the same code (2):
- tests/test_flaky.py::test_flaky_random: head failed 2, passed 1 of 3 runs
- tests/test_flaky.py::test_flaky_timing: head failed 3, passed 1 of 4 runs

How to report these failures:
- Fix the NEW failures: they pass on the base commit and fail with the current changes.
- Call a failure pre-existing only if it is listed under PRE-EXISTING above, and cite base ae34f97 and its error.
- FLAKY and ENVIRONMENT failures are not caused by this change and are not yours to fix: leave them and mention them to the user in one line.
Details: .git/baseline/last.json, or run `/workspace/baseline/bin/baseline check -- python -m pytest -q -p no:cacheprovider`.
```

In run 5 the agent never ran the check itself or anything on the base commit (its tool calls: pytest,
the skill, `git show`/`git diff`, the fix + pytest), yet its answer quotes base results that only the
hook context contained, e.g. "fails on base `ae34f97` with the same error, `assert 1.57 == 1.58`" and
"they pass and fail on the same code". In the probe run the only test output the agent saw was
`ERROR tests/test_report.py … Interrupted: 1 error during collection … 1 error in 0.05s`, and its
answer listed all nine verdicts with base `ae34f97`, saying "A baseline hook added per-test verdicts
for the failures".

## (2) The final answer matches the verdicts

Run 5, final answer (trimmed):

> I fixed the two failures this branch caused. The five that still fail were failing before it, and I
> left them alone. … The baseline check against base `ae34f97` now reports no new failures.
>
> **Caused by the branch, now fixed** — `test_new_discount_percent` (restored the percentage
> discount in `shop/cart.py`); `tests/test_report.py` failed to load because the commit renamed
> `shop/report.py` to `shop/report_v2.py` (added a re-exporting `shop/report.py`).
>
> **Already failing before the branch (not touched)** — `test_pre_tax_rounding`: fails on base
> `ae34f97` with the same error, `assert 1.57 == 1.58`. `test_pre_changed_shipping`: fails on base
> `ae34f97` with `assert 5 == 7`, and on the branch with `assert 6 == 7` … it is not worse, so I left it.
> `tests/test_env.py` (three tests): environment problems, not code bugs. `tests/test_flaky.py` (two
> tests): they pass and fail on the same code.

Run 4 said the same (it restored `cart.py` and `git mv`'d the module back) and also flagged the
error-changed shipping test for a decision. Both NEW failures were fixed in runs 2–5; no pre-existing,
environment or flaky test was "fixed"; every PRE-EXISTING claim cites `ae34f97` and the base error.
`git worktree list` showed only the main worktree after every run.

## (3) A failing non-test command produces no hook output

Probe run, trace: `PostToolUseFailure 0.0s 0 chars 'ls /nonexistent'` — the hook ran, decided it is
not a test command, and printed nothing; the agent saw only `ls: cannot access '/nonexistent': No
such file or directory`.

## Control: the same prompt without the plugin

Both control runs (sonnet, same prompt) also got this small fixture right: they saw the collection
error, built a base worktree by hand (`git worktree add … ae34f97`; control 2 used the fixed path
`/tmp/base_wt`), ran the suite there once, compared, and fixed the two NEW failures. The differences:

* Single runs on each side. Control 1: "`test_flaky_random` … passed on base and failed on both of my
  runs on the branch. That is random-number luck, not the branch." — the pattern of a NEW failure,
  dismissed because of the test's name. Control 2 filed `test_flaky_random` under "already failing on
  the base commit" (in its base run it happened to fail). With the plugin, every verdict rests on 5 head
  and up to 10 base runs and the flaky ones are shown flipping.
* They created and removed git worktrees in the user's repo themselves; with the plugin nothing but
  the tool's own temp worktree is created.

Speed and cost were about the same: plugin runs 19–27 s and $0.17–0.18 (runs 4, 5), control runs
16–27 s and $0.17–0.18. On a suite where a by-hand base run is slow or the change is bigger, the
by-hand comparison gets more expensive; that was not measured here.

## Usage per run (from the CLI's result event)

| run | duration | turns | total_cost_usd | input / cache-write / cache-read / output tokens |
|---|---|---|---|---|
| plugin 1 | 20.6 s | 4 | 0.2069 | 8 / 44,992 / 128,248 / 1,407 |
| plugin 2 | 31.8 s | 6 | 0.1658 | 10 / 33,696 / 182,061 / 1,277 |
| plugin 3 | 24.1 s | 6 | 0.1695 | 12 / 34,624 / 184,022 / 1,257 |
| probe | 19.9 s | 3 | 0.1447 | 6 / 32,400 / 92,384 / 588 |
| plugin 4 | 19.2 s | 6 | 0.1671 | 8 / 35,377 / 142,485 / 1,137 |
| plugin 5 | 26.6 s | 7 | 0.1793 | 10 / 36,348 / 189,108 / 1,500 |
| control 1 | 27.1 s | 6 | 0.1719 | 12 / 33,553 / 227,317 / 1,490 |
| control 2 | 15.7 s | 6 | 0.1828 | 12 / 35,550 / 228,271 / 1,779 |

## Installing through the marketplace file

With an isolated config dir (`CLAUDE_CONFIG_DIR=/tmp/…`, so the user's own Claude Code setup was not
changed), `claude plugin marketplace add /workspace/baseline` and `claude plugin install
baseline@baseline` both succeeded offline; `claude plugin details baseline@baseline` listed the two
hooks (PostToolUseFailure, PostToolUse), the `failing-tests` skill and the `baseline` command, and the
installed copy kept `bin/baseline` executable. A session under that isolated config could not run
("Not logged in"), and installing into the real user config would have enabled the hook for every
session on this machine, so the sessions above used `--plugin-dir`.
