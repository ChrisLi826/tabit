#!/usr/bin/env python3
"""A path in a reply opens the file, also when its name is not ASCII
(a Chinese file name, for one)."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "chat.html")


def _page():
    with open(PAGE, encoding="utf-8") as f:
        return f.read()


class TestPathLinks(unittest.TestCase):
    def test_paths_become_links(self):
        src = _page()
        esc = re.search(r"^const esc = .*$", src, re.M).group(0)
        code = re.search(r"^const pathLink = .*?\n\}\n", src, re.M | re.S).group(0)
        script = esc + "\n" + code + r"""
function linked(raw, path) {
  const out = inline(raw);
  const want = 'href="api/file?path=' + encodeURIComponent(path) + '&amp;render=1"';
  if (!out.includes(want)) { console.error("no link for", path, "in", out); process.exit(1); }
}
function plain(raw) {
  const out = inline(raw);
  if (out.includes("api/file")) { console.error("a link in", out); process.exit(1); }
}
linked("`/home/chris/a/ECC100_交接包_重點整理.html`", "/home/chris/a/ECC100_交接包_重點整理.html");
linked("see /home/chris/報告.md for it", "/home/chris/報告.md");
linked("寫好了：/home/chris/報告.md。", "/home/chris/報告.md");
linked("`/home/chris/tabit/chat.html`", "/home/chris/tabit/chat.html");
linked("at /tmp/x.log.", "/tmp/x.log");
plain("`npm run build`");
plain("`/usr/bin/a b`");
console.log("ok");
"""
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)


if __name__ == "__main__":
    unittest.main()
