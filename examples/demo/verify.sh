#!/usr/bin/env bash
# Checks the demo end to end, locally (no network, no push):
#   1. N fresh setups (default 5): `baseline check -- npm test` on agent-change must exit 1 with
#      exactly 1 NEW (the discount test), 2 PRE-EXISTING (minus sign, half-cent tax), 1 FLAKY
#      (rates sandbox; never NEW), nothing else.
#   2. One more fresh setup with the NEW break fixed and committed: `baseline check --base origin/main
#      -- npm test` (the comparison push-guard makes) must exit 0 with the 2 PRE-EXISTING + 1 FLAKY.
#
#   ./verify.sh [N]       needs git, node 20+ and npm on PATH, and Baseline: $BASELINE_BIN,
#                         `baseline` on PATH, or ../../bin/baseline when run inside the Baseline repo.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd -P)
n=${1:-5}

if [ -n "${BASELINE_BIN:-}" ]; then baseline=$BASELINE_BIN
elif command -v baseline >/dev/null; then baseline=$(command -v baseline)
elif [ -x "$here/../../bin/baseline" ]; then baseline="$here/../../bin/baseline"
else echo "verify.sh: Baseline not found (set BASELINE_BIN or put baseline on PATH)" >&2; exit 2
fi
command -v node >/dev/null || { echo "verify.sh: node is not on PATH" >&2; exit 2; }
major=$(node -p 'process.versions.node.split(".")[0]')
[ "$major" -ge 20 ] || { echo "verify.sh: node 20+ needed, found $(node --version)" >&2; exit 2; }
echo "baseline: $baseline ($("$baseline" version)), node $(node --version)"

work=$(mktemp -d "${TMPDIR:-/tmp}/demo-verify.XXXXXX")
trap 'rm -rf "$work"' EXIT

# assert <repo> <exit code> <broken|fixed>: compares the repo's last report with the expected verdicts.
assert() {
  node - "$1/.git/baseline/last.json" "$2" "$3" <<'JS'
const fs = require('node:fs');
const [file, code, mode] = process.argv.slice(2);
const report = JSON.parse(fs.readFileSync(file, 'utf8'));
const NEW = ['applies a percentage discount to hourly lines'];
const PRE = ['puts the minus sign before the currency symbol', 'rounds half a cent up'];
const FLAKY = ['converts EUR to USD with the quote from the rates sandbox'];
const name = (id) => id.split(' > ').pop();
const ids = (verdict) => report.results.filter((r) => r.verdict === verdict).map((r) => name(r.id)).sort();
const want = mode === 'broken' ? 1 : 0;
const problems = [];
const expect = (what, got, exp) => {
  if (JSON.stringify(got) !== JSON.stringify(exp)) problems.push(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(exp)}`);
};
expect('exit code', Number(code), want);
expect('report exit_code', report.exit_code, want);
expect('NEW', ids('new'), mode === 'broken' ? NEW : []);
expect('PRE-EXISTING', ids('pre_existing'), PRE);
expect('FLAKY', ids('flaky'), FLAKY);
expect('other verdicts', report.results.filter((r) => !['new', 'pre_existing', 'flaky'].includes(r.verdict))
  .map((r) => `${r.verdict}: ${r.id}`), []);
expect('missing', report.missing, []);
const f = report.results.find((r) => FLAKY.includes(name(r.id)));
const how = f ? `flaky: head ${f.head.failed}/${f.head.runs} failed, base ${f.base.failed}/${f.base.runs} failed` +
  (f.surfaced ? ', surfaced' : '') : 'flaky: not reported';
if (problems.length) { console.log(`FAIL (${how})\n    ` + problems.join('\n    ')); process.exit(1); }
console.log(`ok   (${how})`);
JS
}

pass=0
for i in $(seq 1 "$n"); do
  repo="$work/broken-$i"
  "$here/setup.sh" "$repo" >/dev/null || { echo "setup $i failed"; continue; }
  (cd "$repo" && "$baseline" check -- npm test >"$work/report-$i.txt" 2>&1)
  code=$?
  printf 'broken branch, fresh setup %s: ' "$i"
  if assert "$repo" "$code" broken; then pass=$((pass + 1)); else cat "$work/report-$i.txt"; fi
  [ "$i" = 1 ] && cp "$work/report-$i.txt" "$work/first-report.txt"
done

# The fix: sum the unrounded line amounts again (main's src/invoice.js), committed on agent-change.
repo="$work/fixed"
fixed=0
"$here/setup.sh" "$repo" >/dev/null
git -C "$repo" show main:src/invoice.js >"$repo/src/invoice.js"
git -C "$repo" -c user.name="Demo Dev" -c user.email="dev@example.com" -c commit.gpgsign=false \
  commit -q --no-verify -am "Sum unrounded line amounts before applying the discount"
(cd "$repo" && "$baseline" check --base origin/main -- npm test >"$work/report-fixed.txt" 2>&1)
code=$?
printf 'after the fix, fresh setup: '
if assert "$repo" "$code" fixed; then fixed=1; else cat "$work/report-fixed.txt"; fi

echo
echo "Report from fresh setup 1:"
sed 's/^/  /' "$work/first-report.txt" 2>/dev/null
echo
echo "Report after the fix:"
sed 's/^/  /' "$work/report-fixed.txt"
echo
echo "verify: broken branch $pass/$n passed; after the fix $fixed/1 passed"
[ "$pass" = "$n" ] && [ "$fixed" = 1 ]
