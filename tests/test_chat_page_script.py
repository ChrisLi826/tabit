#!/usr/bin/env python3
"""The chat page's script parses. One name declared twice (a second `const
clock`) stopped the whole page: no list, no buttons."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


class TestPageScript(unittest.TestCase):
    def test_every_script_parses(self):
        with open(PAGE, encoding="utf-8") as f:
            src = f.read()
        scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
        self.assertTrue(scripts)
        for js in scripts:
            out = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
                                 input=js, capture_output=True, text=True, check=False)
            self.assertEqual(out.returncode, 0, out.stderr[-600:])


if __name__ == "__main__":
    unittest.main()
