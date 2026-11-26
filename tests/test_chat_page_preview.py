#!/usr/bin/env python3
"""A question whose options have previews (AskUserQuestion "preview") shows the
picked option's preview under its buttons, as the tab shows it beside them."""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _run(body):
    with open(PAGE, encoding="utf-8") as f:
        src = f.read()
    code = "".join(re.search(r"^function %s\(.*?\n\}\n" % n, src, re.M | re.S).group(0)
                   for n in ("optPicked", "pvBox", "pvOf"))
    script = ("const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');\n"
              + code + body)
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


Q = {"question": "Which?", "options": [
    {"label": "One", "description": "the first", "preview": "┌─┐\n│1│\n└─┘"},
    {"label": "Two", "preview": "<2>"}]}


class TestPreview(unittest.TestCase):
    def test_an_open_card_shows_the_first_option_as_the_tab_does(self):
        got = _run("console.log(JSON.stringify(pvOf(%s, true, {})))" % json.dumps(Q))
        self.assertIn('<div class="hint">the first</div>', got)
        self.assertIn("<pre>┌─┐\n│1│\n└─┘</pre>", got)

    def test_an_answered_card_shows_the_picked_one(self):
        got = _run("console.log(JSON.stringify(pvOf(%s, false, {'Which?': 'Two'})))" % json.dumps(Q))
        self.assertEqual(got, '<div class="pv"><pre>&lt;2></pre></div>')

    def test_no_box_without_previews(self):
        plain = {"question": "Which?", "options": [{"label": "One", "description": "d"}]}
        got = _run("console.log(JSON.stringify([pvOf(%s, true, {}), pvOf(%s, false, {})]))"
                   % (json.dumps(plain), json.dumps(Q)))
        self.assertEqual(got, ["", ""])              # no previews; answered with no pick

    def test_picked_handles_single_and_multi_select(self):
        got = _run("console.log(JSON.stringify(["
                   "optPicked(%s, {'Which?': 'Two'}, 'Two'),"
                   "optPicked(%s, {'Which?': 'Two'}, 'One'),"
                   "optPicked(%s, {'Which?': ['One', 'Two']}, 'One'),"
                   "optPicked(%s, {'Which?': ['One']}, 'Two'),"
                   "optPicked(%s, {}, 'One')"
                   "]))" % ((json.dumps(Q),) * 5))
        self.assertEqual(got, [True, False, True, False, False])


if __name__ == "__main__":
    unittest.main()
