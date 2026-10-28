#!/usr/bin/env bash
# Regenerates the node:test TAP fixtures in this directory from a real `node --test` run.
#
#   ./capture.sh NODE [TAG]     e.g. ./capture.sh "$(command -v node)" v24
#
# Writes TAG-base.tap, TAG-head.tap (the flaky test fails), TAG-head-subset.tap (a rerun with
# --test-name-pattern) and TAG-head-rerun.tap (the flaky test passes).
# The project is built in /tmp/nodetest-fixture: the paths in the TAP point there.
#
# Scenarios (all inside describe() suites, some nested):
#   invoice > discounts > applies a percentage           passes on base, fails on head (NEW)
#   invoice > discounts > stacked > caps at 100%         same, three suites deep (NEW)
#   invoice > keeps legacy rounding                      deleted on head (missing on head)
#   format > rounds half a cent up                       fails on both, same error (PRE-EXISTING)
#   format > puts the minus sign first                   fails on both, error changed
#   rates > quotes from the sandbox                      fails, then passes on head (FLAKY)
#   ledger > posts a debit / posts a credit              the suite's before hook throws on both sides
#   archive > monthly > writes a file                    the suite's after hook throws on head only
set -euo pipefail
node=${1:?usage: capture.sh NODE [TAG]}
tag=${2:-$("$node" -p '"v" + process.versions.node.split(".")[0]')}
out=$(cd "$(dirname "$0")" && pwd -P)
dir=/tmp/nodetest-fixture
rm -rf "$dir"
mkdir -p "$dir"

src() {  # src base|head
  local disc=$'(v, p) => Math.max(0, v * (1 - p / 100))' neg=$'(c) => `$-${c.toFixed(2)}`' leftover=false
  if [ "$1" = head ]; then
    disc=$'(v, p) => Math.max(0, v * (1 + p / 100))'
    neg=$'(c) => `-$${c}`'
    leftover=true
  fi
  cat >"$dir/invoice.mjs" <<EOF
export const total = (xs) => xs.reduce((a, b) => a + b, 0);
export const discount = $disc;
export const roundHalf = (x) => Math.round(x * 100) / 100;
export const negative = $neg;
export const ledgerSeed = () => { throw new Error('ledger seed is missing account 4000'); };
export const archiveCleanup = () => { if ($leftover) throw new Error('archive temp dir was not removed'); };
EOF
  cat >"$dir/invoice.test.mjs" <<'EOF'
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { total, discount, roundHalf, negative, ledgerSeed, archiveCleanup } from './invoice.mjs';

describe('invoice', () => {
  it('totals lines', () => assert.equal(total([1, 2]), 3));
  describe('discounts', () => {
    it('applies a percentage', () => assert.equal(discount(100, 10), 90));
    it('applies a fixed amount', () => assert.equal(discount(100, 0), 100));
    describe('stacked', () => {
      it('caps at 100%', () => assert.equal(discount(100, 150), 0));
    });
  });
  it('keeps legacy rounding', () => assert.equal(roundHalf(1.234), 1.23));
});

describe('format', () => {
  it('rounds half a cent up', () => assert.equal(roundHalf(1.005), 1.01));
  it('puts the minus sign first', () => assert.equal(negative(5), '-$5.00'));
});

describe('rates', () => {
  it('quotes from the sandbox', () => assert.equal(process.env.FLAKY_PASS ? 1.085 : 1.0832, 1.085));
});

describe('ledger', () => {
  before(() => ledgerSeed());
  it('posts a debit', () => assert.ok(true));
  it('posts a credit', () => assert.ok(true));
});

describe('archive', () => {
  after(() => archiveCleanup());
  describe('monthly', () => {
    it('writes a file', () => assert.ok(true));
  });
});
EOF
  if [ "$1" = head ]; then  # the legacy rounding test is deleted on head
    sed -i "/keeps legacy rounding/d" "$dir/invoice.test.mjs"
  fi
}

run() {  # run OUTFILE [node option]
  (cd "$dir" && env $FLAKY "$node" --test --test-reporter=tap --test-reporter-destination="$1" ${2:+"$2"} \
    invoice.test.mjs >/dev/null 2>&1) || true
}

FLAKY=
src base
run "$out/$tag-base.tap"
src head
run "$out/$tag-head.tap"
# a rerun of one test the way Baseline does it (the test and its ancestors): on Node 20 the tests that
# don't match are listed as skipped, and the ledger suite's before hook still runs and fails
run "$out/$tag-head-subset.tap" '--test-name-pattern=^(?:invoice|discounts|applies\ a\ percentage)$'
FLAKY=FLAKY_PASS=1
run "$out/$tag-head-rerun.tap"
echo "wrote $out/$tag-{base,head,head-subset,head-rerun}.tap with node $("$node" --version)"
