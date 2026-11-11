#!/usr/bin/env python3
"""A message another AI tab typed ("[from @name]: ...") shows as that AI's
bubble in the chat page, not as the user's."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestFromTab(unittest.TestCase):
    def test_it_reads_the_name_and_the_text(self):
        fn = re.search(r"function fromTab\(text\) \{.*?\n\}", _page(), re.S).group(0)
        script = fn + r"""
function eq(a, b, msg) { if (JSON.stringify(a) !== JSON.stringify(b)) { console.error(msg, JSON.stringify(a)); process.exit(1); } }
eq(fromTab("[from @ecw220v3]: hi\nthere"), ["ecw220v3", "hi\nthere"], "a tab");
eq(fromTab("[from @claude-台中火車站]: 好"), ["claude-台中火車站", "好"], "a name in Chinese");
eq(fromTab("hello [from @x]: no"), null, "not at the start");
eq(fromTab("[user]: hi"), null, "a group line");
eq(fromTab(""), null, "empty");
eq(fromTab(undefined), null, "no text");
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)

    def test_the_page_draws_it_as_the_ais(self):
        src = _page()
        user = src.split('case "user":', 1)[1].split('case "text":', 1)[0]
        self.assertIn("fromTab(ev.text)", user)
        self.assertIn('div("b ai"', user)
        self.assertIn("x.at === name", user)                       # its own picture
        self.assertIn('c ? emo(face(c)) : "🤖"', user)              # a robot when not known
        self.assertIn('(fromTab(ev.text) || ["You"])[0]', src)     # a reply quotes its name


if __name__ == "__main__":
    unittest.main()
