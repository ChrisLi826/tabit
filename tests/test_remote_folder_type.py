#!/usr/bin/env python3
"""The new-tab folder box narrows the list from what was typed."""
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "remote.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


def _rules():
    src = _page()
    start = src.index("// folder-type:start")
    end = src.index("// folder-type:end")
    return src[start:end]


class TestTypedFolder(unittest.TestCase):
    def test_last_part_narrows_the_list(self):
        script = r"""
%s
function assert(cond, msg) { if (!cond) { console.error(msg); process.exit(1); } }
function eq(got, dir, prefix, msg) {
  assert(got.dir === dir && got.prefix === prefix, msg + " got " + got.dir + " / " + got.prefix);
}
eq(typedFolder(""), "~", "", "empty");
eq(typedFolder("~"), "~", "", "home");
eq(typedFolder("~/"), "~", "", "home slash");
eq(typedFolder("~/show"), "~", "show", "prefix");
eq(typedFolder("~/showmethemoney/"), "~/showmethemoney", "", "into a folder");
eq(typedFolder("~/showmethemoney/co"), "~/showmethemoney", "co", "inside, narrowed");
eq(typedFolder("/"), "/", "", "root");
eq(typedFolder("/tmp"), "/", "tmp", "root child");
eq(typedFolder("/tmp/"), "/tmp", "", "into tmp");
eq(typedFolder("/tmp/foo"), "/tmp", "foo", "inside tmp");
console.log("ok");
""" % _rules()
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertIn("ok", out.stdout)

    def test_the_page_follows_typing(self):
        src = _page()
        box = src.split('id="ncwd"', 1)[1].split("function tabMenu", 1)[0]
        load = box.split("async function loadDirs", 1)[1]
        self.assertIn('addEventListener("input", applyTypedFolder)', box)
        self.assertIn('id="nhid"', _page())
        self.assertIn("hidden=1", load)
        self.assertIn('d.name.startsWith(".")', box)
        self.assertIn("if (!keep && $(\"ncwd\"))", load)
        self.assertLess(load.index("if (!keep"), load.index("paintDirs(prefix)"))
