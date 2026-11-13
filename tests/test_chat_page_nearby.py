#!/usr/bin/env python3
"""The chat page's Nearby sheet: off until picked, what came shown as plain
text, every offer asked about."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestNearbyPage(unittest.TestCase):
    def test_the_button_and_the_sheet(self):
        src = _page()
        self.assertIn('<button id="nearby" type="button"', src)
        self.assertIn('$("nearby").onclick = $("sb-nearby").onclick = nearbySheet;', src)
        self.assertIn('api("api/chats/nearby/on", { for: b.dataset.nbon })', src)
        self.assertIn('api("api/chats/nearby/send", { to, text, files: paths })', src)

    def test_an_offer_can_go_on_to_a_chat_you_pick(self):
        src = _page()
        offer = re.search(r"async function nearbyOffer\(o\) \{.*?\n\}\n", src, re.S).group(0)
        self.assertIn('<option value="">Just keep it (Received in 📡)</option>', offer)
        self.assertIn('api("api/chats/nearby/answer", { id: o.id, accept, chat: into })', offer)
        self.assertIn('const into = accept ? $("nb-into").value : "";', offer)

    def test_a_closed_sheet_is_not_written_to(self):
        # the sheet can close while avahi or the other side has not answered
        src = _page()
        sheet = re.search(r"async function nearbySheet\(\) \{.*?\n\}\n", src, re.S).group(0)
        self.assertIn('const to = $("nb-to");', sheet)
        self.assertIn('msg = $("nb-msg"), btn = $("nb-send"), box = $("nb-text"), pick = $("nb-files");', sheet)
        after = sheet.split("await api(\"api/chats/nearby/peers\")")[1].split("};")[0]
        self.assertNotIn('$("', after)

    def test_what_came_is_plain_text(self):
        # a neighbour's words never become links to this computer's files
        src = _page()
        offer = re.search(r"async function nearbyOffer\(o\) \{.*?\n\}\n", src, re.S).group(0)
        self.assertNotIn("inline(", offer)
        self.assertIn('<div class="nb-text">${esc(o.text)}</div>', offer)
        sheet = re.search(r"async function nearbySheet\(\) \{.*?\n\}\n", src, re.S).group(0)
        self.assertNotIn("inline(", sheet)
        self.assertNotIn("md(", sheet)

    def test_sizes_read_well(self):
        src = _page()
        code = re.search(r"^const sizeOf = .*$", src, re.M).group(0)
        script = code + r"""
const eq = (a, b) => { if (a !== b) { console.error(a, "!=", b); process.exit(1); } };
eq(sizeOf(500), "500 B"); eq(sizeOf(2048), "2 KB"); eq(sizeOf(3 * 1048576), "3.0 MB");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
