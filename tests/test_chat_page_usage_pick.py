#!/usr/bin/env python3
"""A usage row and the chats that use its login have the same key, so tapping
rows keeps only the chats of the logins picked."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestUsagePick(unittest.TestCase):
    def test_rows_and_chats_match(self):
        src = _page()
        code = re.search(r"^const useKey = .*?\nconst chatUse = .*?\n", src, re.M | re.S).group(0)
        script = code + r"""
function eq(a, b) { if (a !== b) { console.error(a, "!=", b); process.exit(1); } }
// usage rows: {cli, account}
const rows = [{ cli: "claude", account: "" }, { cli: "claude", account: "Personal" }, { cli: "grok", account: "" }, { cli: "codex", account: "" }];
const keys = rows.map((u) => useKey(u.cli, u.account));
// chats: an own chat (no tab), and mirrored Claude, Grok and Codex tabs
eq(chatUse({ account: "" }), keys[0]);
eq(chatUse({ account: "Personal" }), keys[1]);
eq(chatUse({ tab: { cli: "claude" }, account: "Personal" }), keys[1]);
eq(chatUse({ tab: { cli: "claude" }, account: "" }), keys[0]);
eq(chatUse({ tab: { cli: "grok" }, account: "" }), keys[2]);
eq(chatUse({ tab: { cli: "codex" }, account: "" }), keys[3]);
if (chatUse({ account: "Personal" }) === keys[0]) process.exit(1);
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)

    def test_chats_and_groups_with_a_picked_login_stay(self):
        src = _page()
        code = re.search(r"^const useKey = .*?\nconst picked = .*?\n", src, re.M | re.S).group(0)
        script = code + r"""
function eq(a, b) { if (a !== b) { console.error(a, "!=", b); process.exit(1); } }
const chats = [{ id: "a", account: "" }, { id: "b", account: "Personal" }, { id: "g", tab: { cli: "grok" }, account: "" }];
const uses = new Map(chats.map((c) => [c.id, chatUse(c)]));
only.add("claude/Personal");
eq(picked(chats[0], uses), false);
eq(picked(chats[1], uses), true);
eq(picked({ members: [{ id: "a" }, { id: "b" }] }, uses), true);    // one member is enough
eq(picked({ members: [{ id: "a" }, { id: "g" }] }, uses), false);
eq(picked({ members: [{ id: "gone" }] }, uses), false);
only.add("grok/");
eq(picked({ members: [{ id: "a" }, { id: "g" }] }, uses), true);
eq(picked(chats[2], uses), true);
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertIn("(!only.size || picked(r, uses))", src)

    def test_each_login_counts_its_chats(self):
        src = _page()
        code = (re.search(r"^const useKey = .*?\nconst chatUse = .*?\n", src, re.M | re.S).group(0)
                + re.search(r"^const useCount = .*?\n", src, re.M).group(0))
        script = code + r"""
function eq(a, b) { if (a !== b) { console.error(a, "!=", b); process.exit(1); } }
const n = useCount([{ account: "" }, { account: "Personal" }, { tab: { cli: "claude" }, account: "Personal" },
                    { tab: { cli: "grok" }, account: "" }, { account: "", default: true }]);
eq(n.get("claude/"), 1);           // the hidden default chat is not counted
eq(n.get("claude/Personal"), 2);
eq(n.get("grok/"), 1);
eq(n.get("codex/"), undefined);
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)
        self.assertIn("usageHtml(data.usage, useCount(data.chats))", src)
        self.assertIn('$("usage").addEventListener("click"', src)


if __name__ == "__main__":
    unittest.main()
