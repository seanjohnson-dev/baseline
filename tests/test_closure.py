import tempfile
import unittest
from pathlib import Path

from _support import mod, write

c = mod("closure")


class ClosureTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_python(self):
        write(self.root, {
            "shop/__init__.py": "", "shop/cart.py": "from .util import x\n", "shop/util.py": "x = 1\n",
            "shop/tax.py": "", "tests/__init__.py": "", "tests/conftest.py": "",
            "tests/test_cart.py": "from shop.cart import total\n", "tests/test_flaky.py": "import random\n",
            "tests/test_dyn.py": "import importlib\n",
        })
        self.assertTrue(c.python_changed(self.root, "tests/test_cart.py", {"shop/util.py"}))
        self.assertFalse(c.python_changed(self.root, "tests/test_flaky.py", {"shop/util.py"}))
        self.assertTrue(c.python_changed(self.root, "tests/test_flaky.py", {"tests/conftest.py"}))
        self.assertTrue(c.python_changed(self.root, "tests/test_flaky.py", {"pytest.ini"}), "config is global")
        self.assertFalse(c.python_changed(self.root, "tests/test_flaky.py", {"README.md"}), "docs are not")
        self.assertIsNone(c.python_changed(self.root, "tests/test_dyn.py", {"shop/util.py"}))
        # deleted module imported by the test counts as changed
        self.assertTrue(c.python_changed(self.root, "tests/test_cart.py", {"shop/cart.py"}))
        write(self.root, {"tests/test_gone.py": "from shop.gone import f\n"})
        self.assertTrue(c.python_changed(self.root, "tests/test_gone.py", {"shop/gone.py"}))

    def test_js(self):
        write(self.root, {
            "package.json": "{}", "node_modules/vitest/package.json": "{}",
            "vitest.config.js": "export default { test: { setupFiles: ['./test/setup.js'] } }\n",
            "test/setup.js": "", "src/cart.js": "import { r } from './round';\n", "src/round.ts": "",
            "test/cart.test.js": "import { test } from 'vitest';\nimport { total } from '../src/cart.js';\n",
            "test/flaky.test.js": "import { test } from 'vitest';\n",
            "test/alias.test.js": "import x from '@/lib';\n",
        })
        self.assertTrue(c.js_changed(self.root, self.root, "test/cart.test.js", {"src/round.ts"}))
        self.assertFalse(c.js_changed(self.root, self.root, "test/flaky.test.js", {"src/cart.js"}))
        self.assertTrue(c.js_changed(self.root, self.root, "test/flaky.test.js", {"test/setup.js"}),
                        "setup files from the config affect every test")
        self.assertTrue(c.js_changed(self.root, self.root, "test/flaky.test.js", {"package-lock.json"}))
        self.assertIsNone(c.js_changed(self.root, self.root, "test/alias.test.js", {"src/cart.js"}))

    def test_go(self):
        write(self.root, {
            "go.mod": "module ex.com/m\n", "a/a.go": "package a\n\nimport \"ex.com/m/b\"\n",
            "a/a_test.go": "package a\n", "b/b.go": "package b\n", "c/c_test.go": "package c\n\nimport \"testing\"\n",
        })
        self.assertTrue(c.go_changed(self.root, "ex.com/m", self.root, "ex.com/m/a", {"b/b.go"}))
        self.assertFalse(c.go_changed(self.root, "ex.com/m", self.root, "ex.com/m/c", {"b/b.go"}))
        self.assertTrue(c.go_changed(self.root, "ex.com/m", self.root, "ex.com/m/c", {"go.mod"}))
        self.assertIsNone(c.go_changed(self.root, "ex.com/m", self.root, "other.com/x", {"b/b.go"}))


if __name__ == "__main__":
    unittest.main()
