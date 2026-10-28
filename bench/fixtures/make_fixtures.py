#!/usr/bin/env python3
"""Generate the benchmark fixtures (python/pytest, js/vitest, go).

usage: python3 bench/fixtures/make_fixtures.py [OUT_DIR] [--ecosystems py,js,go] [--node-modules DIR]

OUT_DIR defaults to bench/_work (git-ignored). Needs python3 and git, plus per ecosystem:
pytest (py), node + npm (js), go (go). The js fixture runs `npm install` of the vitest version
pinned below (network needed once); --node-modules DIR copies an existing node_modules
(and the package-lock.json next to it, if any) instead, for offline use.

Each ecosystem gets a git repo with `main` (base) and a `feature/discounts` branch.
Outputs (under OUT_DIR):
  <eco>-shop        feature branch checked out, change COMMITTED    -> use `--base main`
  <eco>-shop-dirty  main checked out, same change UNCOMMITTED       -> the usual agent-edits-files case
Ground truth lives in bench/truth/<eco>.json (outside the repos, so tools can't peek); this script
rewrites it there, so a changed case shows up in `git diff bench/truth`.

Cases (same in every ecosystem):
  NEW            passes on main, fails on feature
  NEW_COLLECT    feature breaks the test module itself (import / compile error)
  PRE            fails identically on main and feature
  PRE_CHANGED    fails on main, still fails on feature but with a DIFFERENT error (worse/changed)
  FLAKY_RANDOM   random 50% failure, same code on both sides
  FLAKY_TIMING   race between a worker with random jitter and a fixed sleep
  ENV_MODULE     needs a package/module that isn't installed
  ENV_VAR        needs DATABASE_URL
  ENV_SERVICE    needs a service on localhost:59999
  PASS           control tests
"""
import argparse, json, os, shutil, subprocess, sys, textwrap
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1]
REPOS = BENCH / "_work"
TRUTH = BENCH / "truth"
VITEST = "3.2.7"   # pinned: the version the published results were produced with
NODE_MODULES = None  # --node-modules: reuse instead of npm install
ENV = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@example.invalid",
           GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@example.invalid")

def sh(*a, cwd=None, check=True):
    return subprocess.run(a, cwd=cwd, env=ENV, check=check, capture_output=True, text=True)

def write(base: Path, files: dict):
    for rel, body in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip("\n"))

def git_init(d: Path):
    sh("git", "init", "-q", "-b", "main", cwd=d)

def commit(d: Path, msg: str):
    sh("git", "add", "-A", cwd=d); sh("git", "commit", "-q", "-m", msg, cwd=d)

def finish(d: Path, feature_files: dict, deletes=(), pre_branch_hook=None):
    """main is committed already. Create feature branch commit, then clone to -dirty with the diff applied uncommitted."""
    sh("git", "checkout", "-q", "-b", "feature/discounts", cwd=d)
    write(d, feature_files)
    for x in deletes:
        (d / x).unlink()
    commit(d, "feature: percentage discounts and report refactor")
    diff = sh("git", "diff", "main", "feature/discounts", cwd=d).stdout
    dirty = d.parent / (d.name + "-dirty")
    if dirty.exists(): shutil.rmtree(dirty)
    shutil.copytree(d, dirty, symlinks=True)
    sh("git", "checkout", "-q", "main", cwd=dirty)
    sh("git", "branch", "-q", "-D", "feature/discounts", cwd=dirty)
    r = subprocess.run(["git", "apply", "-"], input=diff, cwd=dirty, text=True, env=ENV, capture_output=True)
    assert r.returncode == 0, r.stderr
    # `git diff` can't express file deletion of already-present untracked files; fine, apply handles deletes

# ------------------------------------------------------------------ python
def make_py():
    d = REPOS / "py-shop"; d.mkdir(parents=True)
    git_init(d)
    write(d, {
      ".gitignore": ".venv/\n__pycache__/\n.pytest_cache/\n",
      "pytest.ini": "[pytest]\ntestpaths = tests\n",
      "shop/__init__.py": "",
      "shop/cart.py": '''
        def total(items, discount=0):
            """items: list of (price, qty); discount is a fraction (0.1 = 10%)."""
            subtotal = sum(p * q for p, q in items)
            return round(subtotal * (1 - discount), 2)
      ''',
      "shop/tax.py": '''
        def tax(amount):
            return round(amount * 0.15, 2)   # float rounding: tax(10.50) -> 1.57
      ''',
      "shop/shipping.py": '''
        def shipping(weight_kg):
            return 5 + weight_kg * 0   # BUG on main: ignores weight
      ''',
      "shop/report.py": '''
        def summary(order):
            return f"{len(order)} items"
      ''',
      "tests/__init__.py": "",
      "tests/test_cart.py": '''
        from shop.cart import total

        def test_total_plain():
            assert total([(10, 2), (5, 1)]) == 25

        def test_new_discount_percent():
            assert total([(100, 1)], discount=0.1) == 90
      ''',
      "tests/test_tax.py": '''
        from shop.tax import tax

        def test_tax_basic():
            assert tax(100) == 15

        def test_pre_tax_rounding():
            assert tax(10.50) == 1.58
      ''',
      "tests/test_shipping.py": '''
        from shop.shipping import shipping

        def test_pre_changed_shipping():
            assert shipping(2) == 7
      ''',
      "tests/test_report.py": '''
        from shop.report import summary

        def test_new_collect_summary():
            assert summary([1, 2]) == "2 items"
      ''',
      "tests/test_flaky.py": '''
        import random, threading, time

        def test_flaky_random():
            assert random.random() > 0.5

        def test_flaky_timing():
            done = []
            def worker():
                time.sleep(random.uniform(0, 0.02))
                done.append(1)
            threading.Thread(target=worker).start()
            time.sleep(0.01)
            assert done, "worker had not finished"
      ''',
      "tests/test_env.py": '''
        import os, socket

        def test_env_module():
            import psycopg2_not_installed  # noqa

        def test_env_var():
            assert os.environ["DATABASE_URL"].startswith("postgres")

        def test_env_service():
            socket.create_connection(("127.0.0.1", 59999), timeout=1)
      ''',
      "tests/test_control.py": '''
        def test_control_a(): assert 1 + 1 == 2
        def test_control_b(): assert "a".upper() == "A"
      ''',
    })
    commit(d, "base")
    finish(d, {
      "shop/cart.py": '''
        def total(items, discount=0):
            """items: list of (price, qty); discount is now a flat amount."""
            subtotal = sum(p * q for p, q in items)
            return round(subtotal - discount, 2)   # BUG: 0.1 treated as $0.10, not 10%
      ''',
      "shop/shipping.py": '''
        def shipping(weight_kg):
            return 6 + weight_kg * 0   # still wrong, now differently
      ''',
      "shop/report_v2.py": '''
        def summary(order):
            return f"{len(order)} items"
      ''',
    }, deletes=["shop/report.py"])
    truth = {
      "tests/test_cart.py::test_total_plain": "PASS",
      "tests/test_cart.py::test_new_discount_percent": "NEW",
      "tests/test_tax.py::test_tax_basic": "PASS",
      "tests/test_tax.py::test_pre_tax_rounding": "PRE",
      "tests/test_shipping.py::test_pre_changed_shipping": "PRE_CHANGED",
      "tests/test_report.py": "NEW_COLLECT",
      "tests/test_flaky.py::test_flaky_random": "FLAKY",
      "tests/test_flaky.py::test_flaky_timing": "FLAKY",
      "tests/test_env.py::test_env_module": "ENV",
      "tests/test_env.py::test_env_var": "ENV",
      "tests/test_env.py::test_env_service": "ENV",
      "tests/test_control.py::test_control_a": "PASS",
      "tests/test_control.py::test_control_b": "PASS",
    }
    return "py", truth, "python -m pytest -q -p no:cacheprovider --continue-on-collection-errors"

# ------------------------------------------------------------------ js (vitest)
def make_js():
    d = REPOS / "js-shop"; d.mkdir(parents=True)
    git_init(d)
    write(d, {
      ".gitignore": "node_modules/\n",
      "package.json": '''
        {"name":"js-shop","version":"1.0.0","type":"module","private":true,
         "scripts":{"test":"vitest run"},
         "devDependencies":{"vitest":"%s"}}
      ''' % VITEST,
      "vitest.config.js": "import { defineConfig } from 'vitest/config';\nexport default defineConfig({ test: { include: ['test/**/*.test.js'] } });\n",
      "src/cart.js": '''
        export function total(items, discount = 0) {
          const subtotal = items.reduce((s, [p, q]) => s + p * q, 0);
          return Math.round(subtotal * (1 - discount) * 100) / 100;
        }
      ''',
      "src/tax.js": "export const tax = (a) => Math.round(a * 0.15 * 100) / 100 + 0.01; // BUG on main\n",
      "src/shipping.js": "export const shipping = (kg) => 5 + kg * 0; // BUG on main\n",
      "src/report.js": "export const summary = (o) => `${o.length} items`;\n",
      "test/cart.test.js": '''
        import { test, expect } from 'vitest';
        import { total } from '../src/cart.js';
        test('total plain', () => { expect(total([[10, 2], [5, 1]])).toBe(25); });
        test('new discount percent', () => { expect(total([[100, 1]], 0.1)).toBe(90); });
      ''',
      "test/tax.test.js": '''
        import { test, expect } from 'vitest';
        import { tax } from '../src/tax.js';
        test('tax basic', () => { expect(tax(100)).toBe(15.01 - 0.01); });
        test('pre tax rounding', () => { expect(tax(10)).toBe(1.5); });
      ''',
      "test/shipping.test.js": '''
        import { test, expect } from 'vitest';
        import { shipping } from '../src/shipping.js';
        test('pre changed shipping', () => { expect(shipping(2)).toBe(7); });
      ''',
      "test/report.test.js": '''
        import { test, expect } from 'vitest';
        import { summary } from '../src/report.js';
        test('new collect summary', () => { expect(summary([1, 2])).toBe('2 items'); });
      ''',
      "test/flaky.test.js": '''
        import { test, expect } from 'vitest';
        test('flaky random', () => { expect(Math.random()).toBeGreaterThan(0.5); });
        test('flaky timing', async () => {
          let done = false;
          setTimeout(() => { done = true; }, Math.random() * 20);
          await new Promise((r) => setTimeout(r, 10));
          expect(done).toBe(true);
        });
      ''',
      "test/env.test.js": '''
        import { test, expect } from 'vitest';
        import net from 'node:net';
        test('env module', async () => { await import('pg-not-installed'); });
        test('env var', () => { expect(process.env.DATABASE_URL.startsWith('postgres')).toBe(true); });
        test('env service', () => new Promise((res, rej) => {
          const s = net.connect(59999, '127.0.0.1');
          s.on('connect', () => { s.end(); res(); });
          s.on('error', rej);
        }));
      ''',
      "test/control.test.js": '''
        import { test, expect } from 'vitest';
        test('control a', () => { expect(1 + 1).toBe(2); });
        test('control b', () => { expect('a'.toUpperCase()).toBe('A'); });
      ''',
    })
    # fix tax so "tax basic" passes on main and "pre tax rounding" fails: tax(10)=1.5+0.01=1.51 != 1.5; tax(100)=15.01 vs 15.01-0.01=15 -> would fail!
    write(d, {"test/tax.test.js": '''
        import { test, expect } from 'vitest';
        import { tax } from '../src/tax.js';
        test('tax basic', () => { expect(tax(0)).toBe(0.01 - 0.01 + 0.01 - 0.01 + 0); });
        test('pre tax rounding', () => { expect(tax(10)).toBe(1.5); });
    '''})
    # tax(0) = 0 + 0.01 = 0.01 !== 0 -> would fail; make tax basic robust: use a function that is right for 100% base
    write(d, {"src/tax.js": "export const tax = (a) => (a === 10 ? 1.51 : Math.round(a * 0.15 * 100) / 100); // BUG on main for 10\n",
              "test/tax.test.js": '''
        import { test, expect } from 'vitest';
        import { tax } from '../src/tax.js';
        test('tax basic', () => { expect(tax(100)).toBe(15); });
        test('pre tax rounding', () => { expect(tax(10)).toBe(1.5); });
    '''})
    commit(d, "base")
    if NODE_MODULES:
        print(f"js-shop: copying node_modules from {NODE_MODULES} (no npm install)", flush=True)
        shutil.copytree(NODE_MODULES, d / "node_modules", symlinks=True)
        lock = Path(NODE_MODULES).parent / "package-lock.json"
        if lock.is_file():  # keep its root entry in line with the pin above
            (d / "package-lock.json").write_text(lock.read_text().replace('"vitest": "^3.2.0"', f'"vitest": "{VITEST}"'))
    else:
        print(f"npm install vitest@{VITEST} (js-shop) ...", flush=True)
        sh("npm", "install", "--no-audit", "--no-fund", "--silent", cwd=d)
    # package-lock committed on feature? keep lock on main for reproducibility
    commit(d, "lockfile")
    finish(d, {
      "src/cart.js": '''
        export function total(items, discount = 0) {
          const subtotal = items.reduce((s, [p, q]) => s + p * q, 0);
          return Math.round((subtotal - discount) * 100) / 100; // BUG: flat amount, not percent
        }
      ''',
      "src/shipping.js": "export const shipping = (kg) => 6 + kg * 0; // still wrong, differently\n",
      "src/report-v2.js": "export const summary = (o) => `${o.length} items`;\n",
    }, deletes=["src/report.js"])
    # dirty clone: copytree kept node_modules (untracked, ignored) – good
    truth = {
      "test/cart.test.js > total plain": "PASS",
      "test/cart.test.js > new discount percent": "NEW",
      "test/tax.test.js > tax basic": "PASS",
      "test/tax.test.js > pre tax rounding": "PRE",
      "test/shipping.test.js > pre changed shipping": "PRE_CHANGED",
      "test/report.test.js": "NEW_COLLECT",
      "test/flaky.test.js > flaky random": "FLAKY",
      "test/flaky.test.js > flaky timing": "FLAKY",
      "test/env.test.js > env module": "ENV",
      "test/env.test.js > env var": "ENV",
      "test/env.test.js > env service": "ENV",
      "test/control.test.js > control a": "PASS",
      "test/control.test.js > control b": "PASS",
    }
    return "js", truth, "npx vitest run"

# ------------------------------------------------------------------ go
def make_go():
    d = REPOS / "go-shop"; d.mkdir(parents=True)
    git_init(d)
    write(d, {
      "go.mod": "module example.com/shop\n\ngo 1.21\n",
      "cart/cart.go": '''
        package cart

        import "math"

        func Total(items [][2]float64, discount float64) float64 {
        	var s float64
        	for _, it := range items {
        		s += it[0] * it[1]
        	}
        	return math.Round(s*(1-discount)*100) / 100
        }
      ''',
      "cart/cart_test.go": '''
        package cart

        import "testing"

        func TestTotalPlain(t *testing.T) {
        	if got := Total([][2]float64{{10, 2}, {5, 1}}, 0); got != 25 {
        		t.Fatalf("got %v want 25", got)
        	}
        }

        func TestNewDiscountPercent(t *testing.T) {
        	if got := Total([][2]float64{{100, 1}}, 0.1); got != 90 {
        		t.Fatalf("got %v want 90", got)
        	}
        }
      ''',
      "tax/tax.go": '''
        package tax

        func Tax(a float64) float64 {
        	if a == 10 {
        		return 1.51 // BUG on main
        	}
        	return a * 0.15
        }
      ''',
      "tax/tax_test.go": '''
        package tax

        import "testing"

        func TestTaxBasic(t *testing.T) {
        	if Tax(100) != 15 {
        		t.Fatal("tax(100) != 15")
        	}
        }

        func TestPreTaxRounding(t *testing.T) {
        	if got := Tax(10); got != 1.5 {
        		t.Fatalf("got %v want 1.5", got)
        	}
        }
      ''',
      "shipping/shipping.go": "package shipping\n\nfunc Shipping(kg int) int { return 5 + kg*0 } // BUG on main\n",
      "shipping/shipping_test.go": '''
        package shipping

        import "testing"

        func TestPreChangedShipping(t *testing.T) {
        	if got := Shipping(2); got != 7 {
        		t.Fatalf("got %d want 7", got)
        	}
        }
      ''',
      "report/report.go": '''
        package report

        import "fmt"

        func Summary(n int) string { return fmt.Sprintf("%d items", n) }
      ''',
      "report/report_test.go": '''
        package report

        import "testing"

        func TestNewCollectSummary(t *testing.T) {
        	if Summary(2) != "2 items" {
        		t.Fatal("bad summary")
        	}
        }
      ''',
      "flaky/flaky_test.go": '''
        package flaky

        import (
        	"math/rand"
        	"sync/atomic"
        	"testing"
        	"time"
        )

        func TestFlakyRandom(t *testing.T) {
        	if rand.Float64() <= 0.5 {
        		t.Fatal("unlucky")
        	}
        }

        func TestFlakyTiming(t *testing.T) {
        	var done atomic.Bool
        	go func() {
        		time.Sleep(time.Duration(rand.Intn(20)) * time.Millisecond)
        		done.Store(true)
        	}()
        	time.Sleep(10 * time.Millisecond)
        	if !done.Load() {
        		t.Fatal("worker had not finished")
        	}
        }
      ''',
      "envtests/env_test.go": '''
        package envtests

        import (
        	"net"
        	"os"
        	"os/exec"
        	"strings"
        	"testing"
        	"time"
        )

        func TestEnvModule(t *testing.T) {
        	if _, err := exec.LookPath("psql-not-installed"); err != nil {
        		t.Fatalf("required tool missing: %v", err)
        	}
        }

        func TestEnvVar(t *testing.T) {
        	if !strings.HasPrefix(os.Getenv("DATABASE_URL"), "postgres") {
        		t.Fatal("DATABASE_URL not set")
        	}
        }

        func TestEnvService(t *testing.T) {
        	c, err := net.DialTimeout("tcp", "127.0.0.1:59999", time.Second)
        	if err != nil {
        		t.Fatalf("dial: %v", err)
        	}
        	c.Close()
        }
      ''',
      "control/control_test.go": '''
        package control

        import "testing"

        func TestControlA(t *testing.T) {}
        func TestControlB(t *testing.T) {}
      ''',
    })
    commit(d, "base")
    finish(d, {
      "cart/cart.go": '''
        package cart

        import "math"

        func Total(items [][2]float64, discount float64) float64 {
        	var s float64
        	for _, it := range items {
        		s += it[0] * it[1]
        	}
        	return math.Round((s-discount)*100) / 100 // BUG: flat amount
        }
      ''',
      "shipping/shipping.go": "package shipping\n\nfunc Shipping(kg int) int { return 6 + kg*0 } // still wrong, differently\n",
      "report/report.go": '''
        package report

        import "fmt"

        func SummaryV2(n int) string { return fmt.Sprintf("%d items", n) } // renamed; test still calls Summary -> compile error
      ''',
    })
    truth = {
      "example.com/shop/cart::TestTotalPlain": "PASS",
      "example.com/shop/cart::TestNewDiscountPercent": "NEW",
      "example.com/shop/tax::TestTaxBasic": "PASS",
      "example.com/shop/tax::TestPreTaxRounding": "PRE",
      "example.com/shop/shipping::TestPreChangedShipping": "PRE_CHANGED",
      "example.com/shop/report::TestNewCollectSummary": "NEW_COLLECT",
      "example.com/shop/flaky::TestFlakyRandom": "FLAKY",
      "example.com/shop/flaky::TestFlakyTiming": "FLAKY",
      "example.com/shop/envtests::TestEnvModule": "ENV",
      "example.com/shop/envtests::TestEnvVar": "ENV",
      "example.com/shop/envtests::TestEnvService": "ENV",
      "example.com/shop/control::TestControlA": "PASS",
      "example.com/shop/control::TestControlB": "PASS",
    }
    return "go", truth, "go test -count=1 ./..."

def main():
    global REPOS, NODE_MODULES
    ap = argparse.ArgumentParser(description="generate the benchmark fixture repos")
    ap.add_argument("out", nargs="?", default=str(REPOS))
    ap.add_argument("--ecosystems", default="py,js,go")
    ap.add_argument("--node-modules", default=os.environ.get("BENCH_NODE_MODULES"))
    a = ap.parse_args()
    REPOS = Path(a.out).resolve()
    NODE_MODULES = a.node_modules
    makers = {"py": make_py, "js": make_js, "go": make_go}
    ecos = [e for e in a.ecosystems.split(",") if e]
    REPOS.mkdir(parents=True, exist_ok=True); TRUTH.mkdir(exist_ok=True)
    cmdfile = TRUTH / "commands.json"
    cmds = json.loads(cmdfile.read_text()) if cmdfile.exists() else {}
    for e in ecos:
        for n in (f"{e}-shop", f"{e}-shop-dirty"):
            if (REPOS / n).exists(): shutil.rmtree(REPOS / n)
        eco, truth, cmd = makers[e]()
        (TRUTH / f"{eco}.json").write_text(json.dumps(truth, indent=2))
        cmds[eco] = cmd
    cmdfile.write_text(json.dumps(cmds, indent=2))
    print("fixtures ready in", REPOS, ":", sorted(p.name for p in REPOS.iterdir() if p.is_dir()))

if __name__ == "__main__":
    main()
