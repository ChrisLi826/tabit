#!/usr/bin/env python3
"""The chat page's script parses. One name declared twice (a second `const
clock`) stopped the whole page: no list, no buttons."""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _upload_helpers():
    with open(os.path.join(ROOT, "chat.html"), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"const PHOTO_MAX_SIDE = .*?\nasync function upload\(file\)", src, re.S)
    assert m, "upload helpers not found in chat.html"
    return m.group(0).rsplit("\nasync function upload(file)", 1)[0]


class TestPageScript(unittest.TestCase):
    def test_every_script_parses(self):
        for name in ("chat.html", "remote.html"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                src = f.read()
            scripts = [s for s in re.findall(r"<script[^>]*>(.*?)</script>", src, re.S) if s.strip()]
            self.assertTrue(scripts, name)
            for js in scripts:
                out = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"],
                                     input=js, capture_output=True, text=True, check=False)
                self.assertEqual(out.returncode, 0, "%s: %s" % (name, out.stderr[-600:]))

    def test_shrink_photo_leaves_small_non_image_and_gif_untouched(self):
        body = _upload_helpers() + """
(async () => {
  const txt = { type: "text/plain", size: 2000000 };
  const gif = { type: "image/gif", size: 2000000 };
  const png = { type: "image/png", size: 2000000 };
  const small = { type: "image/jpeg", size: 600 * 1024 };
  const r1 = await shrinkPhoto(txt, "notes.txt");
  const r2 = await shrinkPhoto(gif, "anim.gif");
  const r3 = await shrinkPhoto(png, "shot.png");
  const r4 = await shrinkPhoto(small, "pic.jpg");
  if (r1.blob !== txt || r1.name !== "notes.txt") throw new Error("txt changed");
  if (r2.blob !== gif || r2.name !== "anim.gif") throw new Error("gif changed");
  if (r3.blob !== png || r3.name !== "shot.png") throw new Error("png changed");
  if (r4.blob !== small || r4.name !== "pic.jpg") throw new Error("small changed");
})();
"""
        out = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_post_upload_retries_transient_errors_and_stops_on_fatal(self):
        body = _upload_helpers() + """
const _realSetTimeout = setTimeout;
global.setTimeout = (fn, _ms) => _realSetTimeout(fn, 1);
(async () => {
  let calls = 0;
  const retries = [];
  global.fetch = async () => {
    calls++;
    if (calls === 1) throw new TypeError("Load failed");
    if (calls === 2) return { ok: false, status: 502, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ path: "/tmp/a.jpg", size: 4 }) };
  };
  const blob = { size: 4, arrayBuffer: async () => new Uint8Array([1, 2, 3, 4]).buffer };
  const res = await postUpload("a.jpg", blob, (n) => retries.push(n));
  if (res.path !== "/tmp/a.jpg" || calls !== 3 || retries.join(",") !== "1,2") {
    throw new Error("unexpected retry result: " + JSON.stringify({ res, calls, retries }));
  }

  let cutCalls = 0;
  global.fetch = async () => {
    cutCalls++;
    if (cutCalls === 1) return { ok: false, status: 400, json: async () => ({ error: "upload cut short" }) };
    return { ok: true, status: 200, json: async () => ({ path: "/tmp/b.jpg", size: 4 }) };
  };
  const res2 = await postUpload("b.jpg", blob);
  if (res2.path !== "/tmp/b.jpg" || cutCalls !== 2) throw new Error("cut short did not retry");

  for (const code of [413, 403]) {
    let fatalCalls = 0;
    global.fetch = async () => {
      fatalCalls++;
      return { ok: false, status: code, json: async () => ({ error: "refused" }) };
    };
    let threw = false;
    try { await postUpload("c.jpg", blob); } catch (_e) { threw = true; }
    if (!threw || fatalCalls !== 1) throw new Error("retried fatal " + code);
  }
})();
"""
        out = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
