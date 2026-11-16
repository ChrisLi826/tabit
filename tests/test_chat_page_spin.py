#!/usr/bin/env python3
"""The list is redrawn every 5 s. A background chat's spinning arrow must start
at the clock's place in its turn, or every redraw makes it jump back."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestSpinKeepsTurning(unittest.TestCase):
    def test_turn_length_matches_the_css(self):
        src = _page()
        css = re.search(r"\.bub \.spin \{[^}]*animation: bubspin ([\d.]+)s", src).group(1)
        js = re.search(r"Date\.now\(\) % (\d+)\}ms", src).group(1)
        self.assertEqual(int(float(css) * 1000), int(js))

    def test_two_redraws_continue_the_turn(self):
        src = _page()
        code = re.search(r"^function bub\(r\) \{.*?\n\}\n", src, re.M | re.S).group(0)
        script = "const esc = (s) => s;\nlet now = 0; Date.now = () => now;\n" + code + r"""
const delay = () => +bub({ state: "background" }).match(/animation-delay:(-?\d+)ms/)[1];
now = 1000000; const a = delay();
now += 5000;   const b = delay();
// 5 s later the arrow is 5 s further on: the start moved back by 5000 ms mod one turn
if (a !== -0 && a !== 0) { console.error("a", a); process.exit(1); }
if (b !== -(5000 % 1600)) { console.error("b", b); process.exit(1); }
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)


if __name__ == "__main__":
    unittest.main()
