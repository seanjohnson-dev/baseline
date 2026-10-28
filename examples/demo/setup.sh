#!/usr/bin/env bash
# Creates the demo repo: a small invoice-totals library (synthetic data) on `main`,
# one commit on `agent-change` (checked out), and a local bare remote `<target>.git`
# as `origin`. Nothing leaves this machine.
#
#   ./setup.sh [--force] [target-dir]      (default target: ./demo-repo)
set -euo pipefail

force=0
target=""
for arg in "$@"; do
  case "$arg" in
    --force) force=1 ;;
    -h|--help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "setup.sh: unknown option $arg" >&2; exit 2 ;;
    *) [ -z "$target" ] || { echo "setup.sh: one target dir only" >&2; exit 2; }; target="$arg" ;;
  esac
done
target="${target:-./demo-repo}"
target="${target%/}"
remote="$target.git"

command -v git >/dev/null || { echo "setup.sh: git is not on PATH" >&2; exit 2; }

nonempty() { [ -e "$1" ] && [ -n "$(ls -A "$1" 2>/dev/null || echo x)" ]; }
for d in "$target" "$remote"; do
  if nonempty "$d"; then
    if [ "$force" = 1 ]; then
      abs=$(cd "$d" 2>/dev/null && pwd -P || echo "$d")
      case "$abs" in
        /|"$HOME"|"$(pwd -P)"|"$(cd "$(dirname "$0")" && pwd -P)")
          echo "setup.sh: refusing to delete $abs" >&2; exit 2 ;;
      esac
      rm -rf -- "$d"
    else
      echo "setup.sh: $d exists and is not empty (use --force to replace it)" >&2
      exit 1
    fi
  fi
done

mkdir -p "$target"
target_abs=$(cd "$target" && pwd -P)
remote_abs="$target_abs.git"

# Fixed identity and dates, so every setup produces the same commit shas.
g() {
  git -C "$target_abs" -c user.name="Demo Dev" -c user.email="dev@example.com" \
    -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"
}
commit() {
  g add -A
  GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" g commit -q --no-verify -m "$2"
}
put() {
  mkdir -p "$target_abs/$(dirname "$1")"
  cat > "$target_abs/$1"
}

git -c init.defaultBranch=main init -q "$target_abs"
git -C "$target_abs" symbolic-ref HEAD refs/heads/main

# ---------------------------------------------------------------- main, commit 1
put .gitignore <<'EOF'
node_modules/
*.log
EOF

put package.json <<'EOF'
{
  "name": "invoice-totals",
  "version": "1.2.0",
  "private": true,
  "description": "Invoice totals: line amounts, discounts, tax and currency formatting.",
  "main": "src/invoice.js",
  "scripts": {
    "test": "node --test"
  },
  "engines": {
    "node": ">=20"
  }
}
EOF

put README.md <<'EOF'
# invoice-totals

Invoice totals for the billing service: line amounts, percentage discounts, tax and
currency formatting. Amounts are kept in cents. All data in the tests is made up.

```
npm test
```
EOF

put src/money.js <<'EOF'
'use strict';

const SYMBOLS = { USD: '$', EUR: '€', GBP: '£' };

function toCents(amount) {
  return Math.round(amount * 100);
}

function groupThousands(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function formatMoney(cents, currency = 'USD') {
  const symbol = SYMBOLS[currency];
  if (!symbol) throw new Error(`Unsupported currency: ${currency}`);
  const whole = Math.trunc(cents / 100);
  const fraction = String(Math.abs(cents % 100)).padStart(2, '0');
  return `${symbol}${groupThousands(whole)}.${fraction}`;
}

module.exports = { toCents, formatMoney };
EOF

put src/tax.js <<'EOF'
'use strict';

function taxFor(netCents, rate) {
  if (rate < 0) throw new RangeError('tax rate must not be negative');
  return Math.round(netCents * rate);
}

module.exports = { taxFor };
EOF

put src/invoice.js <<'EOF'
'use strict';

const { toCents } = require('./money');
const { taxFor } = require('./tax');

function lineAmount({ unitPrice, quantity }) {
  if (quantity < 0) throw new RangeError('quantity must not be negative');
  return unitPrice * quantity;
}

function invoiceTotal({ lines, discountPercent = 0, taxRate = 0 }) {
  const subtotal = lines.reduce((sum, line) => sum + lineAmount(line), 0);
  const net = toCents(subtotal * (1 - discountPercent / 100));
  const tax = taxFor(net, taxRate);
  return { net, tax, total: net + tax };
}

module.exports = { lineAmount, invoiceTotal };
EOF

put tests/money.test.js <<'EOF'
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { toCents, formatMoney } = require('../src/money');

test('converts decimal amounts to cents', () => {
  assert.equal(toCents(19.99), 1999);
  assert.equal(toCents(0.1 + 0.2), 30);
});

test('formats dollars with thousands separators', () => {
  assert.equal(formatMoney(123450), '$1,234.50');
  assert.equal(formatMoney(100000005), '$1,000,000.05');
});

test('formats euros and pounds', () => {
  assert.equal(formatMoney(999, 'EUR'), '€9.99');
  assert.equal(formatMoney(25000, 'GBP'), '£250.00');
});

test('puts the minus sign before the currency symbol', () => {
  assert.equal(formatMoney(-1050, 'USD'), '-$10.50');
});
EOF

put tests/tax.test.js <<'EOF'
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { taxFor } = require('../src/tax');

test('applies the standard 17.5% rate', () => {
  assert.equal(taxFor(10000, 0.175), 1750);
});

test('zero-rated amounts pay no tax', () => {
  assert.equal(taxFor(4999, 0), 0);
});

test('rounds half a cent up', () => {
  assert.equal(taxFor(1300, 0.175), 228);
});
EOF

put tests/invoice.test.js <<'EOF'
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { invoiceTotal } = require('../src/invoice');

test('sums line amounts', () => {
  const invoice = invoiceTotal({
    lines: [
      { unitPrice: 19.99, quantity: 3 },
      { unitPrice: 4.5, quantity: 2 },
    ],
  });
  assert.deepEqual(invoice, { net: 6897, tax: 0, total: 6897 });
});

test('applies a percentage discount to hourly lines', () => {
  const invoice = invoiceTotal({
    lines: [
      { unitPrice: 12.25, quantity: 2.5 },
      { unitPrice: 12.25, quantity: 1.5 },
    ],
    discountPercent: 10,
  });
  assert.equal(invoice.net, 4410);
});

test('adds tax after the discount', () => {
  const invoice = invoiceTotal({
    lines: [{ unitPrice: 200, quantity: 1 }],
    discountPercent: 10,
    taxRate: 0.175,
  });
  assert.deepEqual(invoice, { net: 18000, tax: 3150, total: 21150 });
});

test('rejects negative quantities', () => {
  assert.throws(
    () => invoiceTotal({ lines: [{ unitPrice: 5, quantity: -1 }] }),
    RangeError,
  );
});
EOF

commit "2026-10-01T09:00:00+00:00" "Invoice totals: money, tax and discount handling"

# ---------------------------------------------------------------- main, commit 2
put src/rates.js <<'EOF'
'use strict';

async function quoteFor(service, from, to) {
  if (from === to) return { from, to, rate: 1 };
  const quote = await service.quote(from, to);
  if (!(quote && quote.rate > 0)) throw new Error(`No rate for ${from}->${to}`);
  return quote;
}

function convert(cents, rate) {
  return Math.round(cents * rate);
}

module.exports = { quoteFor, convert };
EOF

put tests/helpers/sandbox-rates.js <<'EOF'
'use strict';

// In-process stand-in for the rates sandbox. The sandbox runs two replicas
// behind a round-robin balancer and one of them still serves an older
// EUR/USD quote. The request count is kept in the git directory, so every
// checkout of this repository (worktrees too) shares the same balancer.

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const QUOTES = [
  { from: 'EUR', to: 'USD', rate: 1.0832, asOf: '2026-09-30' },
  { from: 'EUR', to: 'USD', rate: 1.085, asOf: '2026-10-01' },
];

function counterFile() {
  const dir = execFileSync('git', ['rev-parse', '--git-common-dir'], {
    cwd: __dirname,
    encoding: 'utf8',
  }).trim();
  return path.join(path.resolve(__dirname, dir), 'demo-flaky-counter');
}

// One appended line per request; the request's number is the position of its
// own line. Appends are atomic, so concurrent test runs get distinct numbers.
function nextRequestNumber() {
  const file = counterFile();
  const token = `${process.pid}.${Date.now()}.${Math.random().toString(36).slice(2)}`;
  fs.appendFileSync(file, token + '\n');
  return fs.readFileSync(file, 'utf8').split('\n').indexOf(token) + 1;
}

function sandboxRates() {
  return {
    async quote(from, to) {
      const quote = QUOTES[(nextRequestNumber() + 1) % 2];
      if (quote.from !== from || quote.to !== to) return null;
      return { ...quote };
    },
  };
}

module.exports = { sandboxRates };
EOF

put tests/rates.test.js <<'EOF'
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { quoteFor, convert } = require('../src/rates');
const { sandboxRates } = require('./helpers/sandbox-rates');

test('converts at a given rate', () => {
  assert.equal(convert(10000, 1.085), 10850);
});

test('converts EUR to USD with the quote from the rates sandbox', async () => {
  const quote = await quoteFor(sandboxRates(), 'EUR', 'USD');
  assert.equal(convert(10000, quote.rate), 10850);
});
EOF

commit "2026-10-02T11:20:00+00:00" "Currency conversion with quotes from the rates sandbox"

# ---------------------------------------------------------------- local remote
git -c init.defaultBranch=main init -q --bare "$remote_abs"
git -C "$remote_abs" symbolic-ref HEAD refs/heads/main
git -C "$remote_abs" fetch -q "$target_abs" main:main
git -C "$target_abs" remote add origin "$remote_abs"
git -C "$target_abs" fetch -q origin
git -C "$target_abs" branch -q --set-upstream-to=origin/main main
git -C "$target_abs" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main

# ---------------------------------------------------------------- agent-change
g checkout -q -b agent-change

put src/invoice.js <<'EOF'
'use strict';

const { toCents } = require('./money');
const { taxFor } = require('./tax');

function lineAmount({ unitPrice, quantity }) {
  if (quantity < 0) throw new RangeError('quantity must not be negative');
  return unitPrice * quantity;
}

function invoiceTotal({ lines, discountPercent = 0, taxRate = 0 }) {
  const subtotal = lines.reduce((sum, line) => sum + toCents(lineAmount(line)), 0);
  const net = Math.round(subtotal * (1 - discountPercent / 100));
  const tax = taxFor(net, taxRate);
  return { net, tax, total: net + tax };
}

module.exports = { lineAmount, invoiceTotal };
EOF

commit "2026-10-03T15:45:00+00:00" "Round line totals to cents before applying the discount"

echo "Demo repo ready: $target_abs"
echo "  branch agent-change (checked out), main = $(git -C "$target_abs" rev-parse --short main), origin = $remote_abs"
echo "Next: cd $target && claude   (then paste prompt.txt)"
