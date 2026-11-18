#!/usr/bin/env python3
"""Text size in Settings: this browser's choice scales the pages' words (--fs),
not icons in fixed boxes or the terminal screen that fits its columns."""
import json
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestTextSize(unittest.TestCase):
    def test_words_scale_and_the_terminal_screen_does_not(self):
        chat = read("chat.html").partition("</style>")[0]
        self.assertIn("body { color: var(--fg); font: calc(16px * var(--fs, 1))/1.5", chat)
        self.assertIn("font-size: calc(16px * var(--fs, 1)); }", chat)       # chat bubbles
        self.assertGreater(chat.count("var(--fs, 1)"), 50)
        remote = read("remote.html").partition("</style>")[0]
        self.assertIn("font: 11px/1.35 ui-monospace", remote)              # #screen keeps its cells
        self.assertGreater(remote.count("var(--fs, 1)"), 20)

    def test_the_choice_is_kept_and_applied(self):
        script = r"""
const store = new Map();
globalThis.localStorage = { getItem: (k) => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
const props = {};
const els = {};
globalThis.document = { documentElement: { style: { setProperty: (k, v) => { props[k] = v; } } },
  getElementById: (id) => els[id] || null };
globalThis.window = globalThis;
store.set("textsize", "1.3");
eval(SRC);
if (props["--fs"] !== "1.3") { console.error("load", props); process.exit(1); }
// the sheet: tap XXL, then M
const btns = [];
els.sfs = { querySelectorAll: () => btns };
els.sclose = {};
let html = "";
window.openTabitSettings((h) => { html = h; const re = /data-fs="([\d.]+)"/g; let m;
  while ((m = re.exec(h))) btns.push({ dataset: { fs: m[1] }, classList: { toggle() {} } }); }, () => {});
if (!/data-fs="1.3" class="sel"/.test(html)) { console.error(html); process.exit(1); }
btns.find((b) => b.dataset.fs === "1.5").onclick();
if (props["--fs"] !== "1.5" || store.get("textsize") !== "1.5") process.exit(2);
btns.find((b) => b.dataset.fs === "1").onclick();
if (props["--fs"] !== "1" || store.has("textsize")) process.exit(3);
console.log("ok");
"""
        src = read("settings.js")
        # drawSettings needs fetch; give it one that never answers
        pre = "globalThis.fetch = () => new Promise(() => {});\nconst SRC = " + json.dumps(src) + ";\n"
        out = subprocess.run(["node", "-e", pre + script], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr or out.stdout)


if __name__ == "__main__":
    unittest.main()
