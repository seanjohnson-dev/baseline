# Baseline — stage 1 spec

The name is final: Baseline (CLI, package, plugin, marketplace, command: `baseline`). The rule below still
holds, so a rename stays one command if it is ever needed.

Tool for coding agents (Claude Code, Codex, Cursor) and humans. When tests fail after the agent's change, one command
answers per failing test: **did my change break this, or was it already broken (or flaky / environment)?** with proof.
Bar: top-level, genuinely useful, not an average product. Quality of verdicts and robustness matter more than features.

## Name handling (IMPORTANT)
The product name must live in ONE place: `brand.py` (NAME, CLI name, env-var prefix, cache dir name, tmp prefix).
Provide `scripts/rename.py <newname>` that renames the package dir, entry point and every string, and a unit test that proves it on a temp copy.
Never hard-code the name elsewhere (tests may import it from brand.py).

## Language / install
Python 3.9+ **standard library only**, single package dir + thin launcher `bin/<name>` (also runnable as `python3 -m <pkg>`). No pip deps. Works on Linux/macOS (Windows best-effort, note gaps).

## CLI
`baseline check [options] [-- <test command>]`
- If no command is given, auto-detect from repo: pytest (pytest.ini/pyproject/tox/setup.cfg/conftest/tests), vitest, jest (package.json deps/config), go (go.mod), bonus: cargo test, rspec.
- Options: `--base <ref>` (default: merge-base of HEAD with the default branch (origin/HEAD, main, master) ; if HEAD is on the default branch / no divergence, base = HEAD, i.e. compare the uncommitted working tree against the last commit); `--runs N` (rerun budget, default 5); `--json` (JSON only to stdout); `--format text|json|both`; `--no-cache`; `--keep-worktree`; `--timeout`; `--cwd`.
- Cases to support: (a) change COMMITTED on a feature branch (base = merge-base with main); (b) change UNCOMMITTED in the working tree on main (base = HEAD); (c) both, committed + dirty (base = merge-base; head = working tree incl. untracked files).
- Also `baseline detect` (prints detected runner/command) and `baseline version`.
- Default output: readable plain-English report to stdout, plus full JSON written to `.git/<name>/last.json` (inside .git so the working tree stays clean). `--json` prints JSON.

## Verdicts (per failing test, and per unit that failed to load)
NEW | PRE-EXISTING | PRE-EXISTING (error changed) | FLAKY | ENVIRONMENT. (Use stable machine strings in JSON, e.g. `new`, `pre_existing`, `pre_existing_error_changed`, `flaky`, `environment`.)
Each verdict carries evidence: normalised+raw error excerpt on head and on base, pass/fail counts per side, number of runs the verdict rests on, base sha and head sha (+ `dirty: true/false`), and a one-sentence plain-English reason an agent can paste into its report.
Also list fixed tests (fail on base, pass on head) in a separate `fixed` list; passing tests are not listed (only counts).

Decision logic (design it carefully, document it in docs/VERDICTS.md):
1. Run the full suite once on HEAD (working tree) with reporter flags injected. Failing tests = F.
2. If F empty → exit 0, "all passed" (still print counts).
3. Rerun ONLY the failing tests on head a few times (early stop: stop as soon as the test has both passed and failed ⇒ FLAKY; stop after N consistent failures). Flaky-on-head tests that fail on head *and pass at least once on head* are FLAKY — never NEW — regardless of base. (Cheap: a test that failed 3x in a row stops.) Rerun budget default 5 total runs per failing test, with the first (full) run counting.
4. Run those same tests on BASE (temp worktree) – with the base result cached per (base sha, runner command, env fingerprint). Base also gets reruns when it passes inconsistent results.
5. Compare: base passes consistently ⇒ NEW (if head fails consistently). base fails and head fails ⇒ PRE-EXISTING if normalised error texts match, else PRE-EXISTING (error changed) — which is NOT a clean bill: it must be flagged as "still failing, but differently – check you didn't make it worse" and counted as blocking? Decide: it does NOT cause non-zero exit but is reported prominently (make exit-code policy flag `--strict-changed` to count it).
6. ENVIRONMENT: error text matches environment classes (missing module/package/executable, missing env var, connection refused/timeout to a service, DNS failure, missing file/permission on config/credentials, etc., per language) AND the test fails on base too with an env-class error. (If a test fails with an env-class error only on head, base passed ⇒ NEW with `env_suspect: true` hint — the agent probably introduced an unresolvable import/dependency.) Never classify a plain assertion failure as ENVIRONMENT.
7. Units that fail to load (pytest collection errors, vitest/jest import / suite-failed-to-run errors, go build/compile/vet failures, cargo compile errors) are first-class failing units with id = file (or package) and kind=`load_error`. NEVER drop silently. If the same unit failed to load on base with the same error → PRE-EXISTING; passed/ran fine on base → NEW. The individual tests inside a module that can't load are "not run" on head: list them as `blocked_tests` on the NEW load error (names from base's run when available) so the agent sees the blast radius.
8. Tests that exist on base but are missing from head (deleted/renamed) are reported as `missing` (informational, not failures), but if the whole file failed to load, see 7.
9. Exit code: 0 = no NEW failures; 1 = at least one NEW failure; 2 = tool/usage error; 3 = could not establish baseline (e.g. cannot create worktree, base run crashed) — document that this must never be reported as PRE-EXISTING. Only NEW (incl. NEW load errors) → 1.
10. Safety: when the base run cannot be trusted (timeout, runner crash, zero tests collected on base though head collected some), say so (`verdict: unknown`/ warning) rather than guessing.

## Runner integration
Inject machine-readable output itself: pytest `--junitxml` (+ `-p no:cacheprovider`, keep user's args, use `--continue-on-collection-errors` so one broken module doesn't hide the rest; parse collection errors from junit `<error>` testcase entries for the module), vitest `--reporter=json` (+ `--outputFile`), jest `--json --outputFile`, go `go test -json -count=1`, cargo `cargo test` with libtest json if available else text parsing, rspec `--format json`. If the user supplied a command (`-- npx vitest run`), detect the runner from it and add flags; if it can't be recognised, run it as-is and fall back to exit code + a clear "can't attribute per-test" message (unknown runner ⇒ whole-command verdict only).
Rerunning only failing tests: pytest node ids, vitest/jest `-t` filter with file path (be careful with regex escaping), `go test -run '^Name$' pkg`, cargo test name filter. Package-level go build failures: rerun the package.
Parameterised ids and duplicate names must be handled.

## Error normalisation
Normalise for comparison: absolute/worktree/temp path prefixes (the base run happens in a different dir!), repo-relative paths kept, line numbers in traceback frames (optional but test it), memory addresses 0x..., durations/timings (`in 0.12s`, `3ms`), timestamps, uuids, random ints in ports/pids, ANSI codes, `object at 0x`, temp dir names. Keep assertion values like `assert 1.57 == 1.58` meaningful — different values DO count as an "error changed". Provide `--similarity` fuzzy fallback only if it can't create false PRE-EXISTING; default is exact-after-normalisation on the *error message + exception type + the assertion text*, not on the full stack.

## Base run in a temp worktree
`git worktree add --detach <tmp> <base>` in a temp dir (outside the repo). Never modify the user's working tree, index, stash, or branches (no checkout/stash/reset!). For dirty head, the head run happens in the real working tree (as the user's test command would); base in the worktree.
Dependencies: node_modules → symlink (or copy-on-write) from the real repo (and nested packages if workspaces – at least root), Python venv is found via PATH/VIRTUAL_ENV so nothing to do, but if the project is installed editable from the real tree (`pip install -e .`) the base worktree would import HEAD code: detect this risk and make the base run import its own code (e.g. prepend worktree to PYTHONPATH / set cwd, `rootdir`), and verify with a test. Go: GOFLAGS=-mod=mod no; just run in worktree, share module cache. Untracked-but-needed files (e.g. .env, generated files, ignored config) — copy `.env*`/other ignored top-level files that exist in the real tree? Decide conservatively: do not copy secrets by default; add `--copy-ignored <glob>`; document.
Cache base results under `.git/<name>/cache/<base-sha>-<hash-of-cmd+env>.json`; invalidate when base sha changes. Always clean up worktree (try/finally, signal handlers, `git worktree prune`), including on Ctrl-C; unit-test cleanup. Run concurrency: don't run base and head concurrently unless safe (ports!). Default sequential.
Timeouts for every subprocess; process-group kill.

## Output (plain English example)
```
baseline: 2 NEW, 1 pre-existing, 1 pre-existing (error changed), 2 flaky, 3 environment  (base 3f2a1c9 → head a81d7e0+dirty, 7.4s, 41 test runs)
NEW (you broke these):
  tests/test_cart.py::test_new_discount_percent — passed on base 3/3, fails on head 3/3
      head: AssertionError: assert 99.9 == 90
...
```
Keep it scannable; first line summary; agents will read it. Include a short "Agent: ..." instruction line at the end e.g. "Fix the NEW failures; do not call them pre-existing."

## Tests, docs, repo hygiene
- `tests/` with a solid unittest (stdlib; pytest-compatible) suite: normaliser, parsers for each runner (using canned junit/json/go-json samples incl. real ones captured from /workspace/baseline-research fixtures), decision logic (table-driven, incl. flaky/changed/env/load-error), git base resolution, worktree lifecycle and cleanup, cache, CLI exit codes, rename script. Aim for meaningful coverage of edge cases, not padding. Integration tests that build tiny temp git repos with pytest (skip if pytest absent) and go (skip if go absent).
- `docs/VERDICTS.md`, a minimal README stub (stage 2 will write the real README/plugin/hook — do NOT build a Claude Code plugin, hooks, or push-guard integration now, and do NOT write marketing copy).
- LICENSE MIT "Copyright (c) 2026 Sean Johnson", .gitignore.
- Do not create a GitHub repo, push, or contact the network (other than nothing). No telemetry.
- Git: commits authored as Sean Johnson <275664475+seanjohnson-dev@users.noreply.github.com> (repo-local config already set). Make several sensible commits.

## Acceptance (the orchestrator verifies this independently — you should self-verify first)
Fixtures: /workspace/baseline-research/fixtures (read make_fixtures.py, harness.py, truth/*.json; repos/{py,js,go}-shop = change committed on feature/discounts with base main; repos/{py,js,go}-shop-dirty = same change uncommitted on main). Test commands: truth/commands.json. Environment for them: PATH=/workspace/baseline-research/.venv/bin:$HOME/go/bin:$PATH and DATABASE_URL unset. Expected verdict for every truth case (NEW, NEW_COLLECT→NEW load error, PRE, PRE_CHANGED→PRE-EXISTING (error changed), FLAKY (random + timing; NEVER NEW), ENV ×3 (module, env var, service) →ENVIRONMENT, PASS ⇒ not reported). Target: 100% over repeated trials (10 each, 3 ecosystems, both modes). Run the repos from a COPY if you need to write; never mutate the fixture repos' tracked state (the check itself must not change `git status` or `git worktree list` afterwards — assert this in an integration test).
Speed target: comparable to rivals (0.5–3 s py/go, ~2.5 s vitest) plus a small flake-rerun overhead.
