#!/usr/bin/env python3
"""Picking an AI fills the new-tab name, and keeps words typed after it."""
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
    start = src.index("// ai-name:start")
    end = src.index("// ai-name:end")
    return src[start:end]


class TestAiName(unittest.TestCase):
    def test_tag_and_suffix(self):
        script = r"""
%s
function assert(cond, msg) { if (!cond) { console.error(msg); process.exit(1); } }
assert(aiTag("grok") === "[Grok]", "grok tag");
assert(aiTag("claude") === "[Claude]", "claude tag");
assert(aiTag("") === "", "empty cli");
assert(nextAiName("", "", "[Grok]") === "[Grok]", "empty name");
assert(nextAiName("[Grok] Fox", "[Grok]", "[Claude]") === "[Claude] Fox", "suffix");
assert(nextAiName("[Grok]abc", "[Grok]", "[Claude]") === "[Claude]abc", "no space");
assert(nextAiName("my name", "[Grok]", "[Claude]") === "my name", "custom name");
assert(nextAiName("[Grok] Fox", "", "[Claude]") === "[Grok] Fox", "no owned tag");
assert(nameWithoutTag("[Claude]", "[Claude]") === "", "only the tag");
assert(nameWithoutTag("[Claude] Fox", "[Claude]") === "Fox", "suffix stays");
assert(nameWithoutTag("[Claude]Fox", "[Claude]") === "Fox", "no space");
assert(nameWithoutTag("hello", "[Claude]") === null, "custom name");
assert(nameWithoutTag("[Claude] Fox", "") === null, "no tag to clear");
assert(nameBackOn("Fox", "Fox", "[Claude]") === "[Claude] Fox", "tag comes back");
assert(nameBackOn("", "", "[Claude]") === "[Claude]", "empty comes back");
assert(nameBackOn("Fox", null, "[Claude]") === null, "not our name");
assert(nameBackOn("Foxy", "Fox", "[Claude]") === null, "edited name");
console.log("ok");
""" % _rules()
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertIn("ok", out.stdout)

    def test_the_page_applies_it(self):
        src = _page()
        body = src.split("function newTab", 1)[1].split("function typedFolder", 1)[0]
        self.assertIn("nameTag = \"\"", body)
        self.assertIn("nameBare = null", body)
        self.assertIn('applyAiName($("ncli").value)', body)
        self.assertIn("else clearAiName()", body)
        self.assertIn('addEventListener("change", () => applyAiName', body)
