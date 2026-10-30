#!/usr/bin/env python3
"""Remote keeps each tab's unsent message. The rules live in remote.html."""
import json
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
    start = src.index("// draft-keep:start")
    end = src.index("// draft-keep:end")
    return src[start:end]


class TestRemoteDraftRules(unittest.TestCase):
    def test_each_tab_keeps_its_own_unsent_text(self):
        script = r"""
let current = null;
%s
function assert(cond, msg) { if (!cond) { console.error(msg); process.exit(1); } }
rememberDraft("a", "hello A");
rememberDraft("b", "hello B");
assert(draftOf("a") === "hello A", "A lost its text");
assert(draftOf("b") === "hello B", "B lost its text");
rememberDraft("a", "");
assert(draftOf("a") === "", "cleared text still there");
assert(draftOf("b") === "hello B", "clearing A touched B");
rememberDraft(null, "nope");
assert(draftOf("b") === "hello B", "a missing tab id wrote a draft");
rememberDraft("a", "fail me");
assert(restoreFailed("a", "fail me", "typed since") === "fail me\ntyped since", "failed send dropped the rest");
assert(restoreFailed("a", "only", "") === "only", "failed send added a blank line");
rememberDraft("a", "hello A");
assert(textToKeep("a", "") === "hello A", "a wiped box dropped its text");
assert(textToKeep("a", "newer") === "newer", "new text lost to the stored draft");
rememberDraft("a", "");
assert(textToKeep("a", "") === "", "cleared text came back from a wipe");
rememberDraft("gone", "bye");
rememberDraft("stay", "keep");
current = "stay";
forgetMissing({tabs: [{id: "stay"}]});
assert(draftOf("gone") === "", "a closed tab kept its text");
assert(draftOf("stay") === "keep", "the open tab lost its text");
current = "away";
rememberDraft("away", "still typing");
forgetMissing({tabs: []});
assert(draftOf("away") === "still typing", "a missing snap wiped the open tab");
console.log("ok");
""" % _rules()
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertIn("ok", out.stdout)

    def test_the_page_uses_those_rules(self):
        src = _page()
        open_tab = src.split("function openTab", 1)[1].split("function closeTab", 1)[0]
        close_tab = src.split("function closeTab", 1)[1].split("function act", 1)[0]
        send = src.split('$("sendbtn").onclick', 1)[1].split("const TOUCH", 1)[0]
        # the save happens before the open tab changes, or the text would follow
        self.assertLess(open_tab.index("textToKeep"), open_tab.index("current = id"))
        self.assertLess(open_tab.index("rememberDraft"), open_tab.index("current = id"))
        # the box is filled after the tab is current, once it can be shown
        self.assertGreater(open_tab.index("showBox"), open_tab.index("current = id"))
        self.assertLess(close_tab.index("textToKeep"), close_tab.index("current = null"))
        self.assertLess(close_tab.index("rememberDraft"), close_tab.index("current = null"))
        self.assertIn('addEventListener("input", onBoxInput)', src)
        self.assertIn('addEventListener("compositionend", onBoxInput)', src)
        self.assertIn('rememberDraft(t.id, "")', src)
        self.assertIn("restoreFailed", send)
        self.assertIn("forgetMissing(snap)", src)
