#!/usr/bin/env python3
"""The chat page names a Grok tab's chat Grok, and its settings sheet does not
offer Claude's logins and models for it."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestGrokPage(unittest.TestCase):
    def test_who_answers(self):
        src = _page()
        lines = re.search(r"const AI_NAMES = .*?\nconst aiName = .*?\n", src, re.S).group(0)
        script = lines + r"""
function eq(a, b) { if (a !== b) { console.error(a, "!=", b); process.exit(1); } }
eq(aiName({ tab: { cli: "grok" } }), "Grok");
eq(aiName({ tab: { cli: "codex" } }), "Codex");
eq(aiName({ tab: { cli: "claude" } }), "Claude");
eq(aiName({}), "Claude");
eq(aiName(null), "Claude");
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)

    def test_the_sheet_sends_only_the_name_for_grok(self):
        src = _page()
        self.assertIn('<div ${grok ? "hidden" : ""}><label>Account</label>', src)
        self.assertIn("const asked = () => grok ? { title: $(\"e-title\").value, ...look.get() }", src)

    def test_the_new_chat_form_offers_grok_and_past_conversations(self):
        src = _page()
        self.assertIn('${AI_NAMES[c] || "Claude"}</option>', src)
        # Grok gets no Claude logins to pick from
        self.assertIn('cli === "claude" ? cfg?.accounts : []', src)
        self.assertIn('<select id="n-sess">', src)
        self.assertIn('api("api/chats/sessions", { cli: cliOf(), cwd: $("n-cwd").value', src)
        self.assertIn('const go = $("n-sess").value ? { resume: $("n-sess").value } : {};', src)


if __name__ == "__main__":
    unittest.main()
