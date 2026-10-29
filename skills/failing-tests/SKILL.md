---
name: failing-tests
description: Use when a test command fails, and before describing any failing test as "pre-existing", "unrelated", "already broken" or "flaky". Re-runs the failing tests on the base commit and labels each one NEW (this change broke it), PRE-EXISTING, FLAKY or ENVIRONMENT, with evidence.
---

# Which test failures did this change cause?

When a test command fails in a git repo, the plugin's hook usually adds a verdict report to the
conversation (it starts with `[baseline] Per-test verdicts`). If there is none, or it is stale, run the
check yourself from the repository:

```
"${CLAUDE_PLUGIN_ROOT}/bin/baseline" check -- <the exact test command>
```

e.g. `... check -- python -m pytest -q`, `... check -- npx vitest run`, `... check -- go test ./...`.
Without `-- <command>` it detects the runner (pytest, vitest, jest, go test, cargo test, rspec).
Base = merge-base of HEAD with main/master (or HEAD itself when you are on the default branch, so
uncommitted changes are what gets judged); `--base REF` overrides. It never touches the working tree,
index or branches: the base runs in a temporary worktree. `--json` prints the full JSON report.

## Verdicts

| verdict | meaning | what to do |
|---|---|---|
| NEW | passes on base, fails with the current changes | fix it; never call it pre-existing |
| PRE-EXISTING | fails on base with the same error | leave it; say "already failing on base <sha>: <error>" |
| PRE-EXISTING (error changed) | fails on base too, but differently now | check the change didn't make it worse |
| FLAKY | passed and failed on the same code | not caused by the change; mention it in one line |
| ENVIRONMENT | fails on base and head for an environmental reason (missing module, env var, service) | not a code bug; mention it in one line |
| UNKNOWN | no trustworthy base result (also when the change edits dependencies and it fails on base too: base ran with the new ones) | say so; do not guess; with "Dependencies changed", the dependency change may be the cause |

Exit codes: 0 no NEW failure · 1 at least one NEW · 2 usage error · 3 base could not be established
(never a "pre-existing" claim).

## Evidence rules

- Only call a failure pre-existing or unrelated when the report lists it as PRE-EXISTING, and quote the
  base sha and the base error from the report.
- Fix NEW failures before reporting the task as done; re-run the check after fixing.
- Do not "fix" PRE-EXISTING, FLAKY or ENVIRONMENT failures as part of this change unless the user asks.
- A load error (`[file failed to load]`) blocks every test in that file; fix it first.
