#!/usr/bin/env python3
"""Remote page server: routes and the SSE stream, on a real unix socket."""
import json
import os
import re
import socket
import sys
import tempfile
import threading
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class FakeApp:
    """Stands in for the window: no GTK thread hop needed in a test."""
    _remote_url = "https://me.ts.net/"
    _remote_files_on = True
    _remote_send_on = True

    def __init__(self):
        self.acts = []
        self.snap = {"time": 1, "version": "v0", "groups": [],
                     "tabs": [{"id": "a", "type": "ai", "ai_status": "idle"}]}

    def _remote_page(self):
        return b"<html>page</html>"

    def _remote_snapshot(self):
        return dict(self.snap)

    def _remote_call(self, fn, timeout=3):
        return fn()

    def _remote_screen(self, rid, color=False):
        return 200, {"id": rid, "lines": ["hello"], "color": color}, ("sess" if color else None)

    @staticmethod
    def _tmux_pane_ansi(name, back=0):
        return {"ansi": "\x1b[31mred", "cols": 80, "rows": 24, "back": back} if name == "sess" else None

    def _remote_act(self, rid, action, body):
        self.acts.append((rid, action, body))
        return 200, {"ok": True}

    def _remote_ack_id(self, rid):
        self.acts.append((rid, "ack", None))
        return 200, {"ok": True}

    def _remote_manage(self, op, target, body):
        self.acts.append((op, target, body))
        return 200, {"ok": True}


def http_post(path, sock_path, body, headers):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(sock_path)
    data = body.encode()
    extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    s.sendall(f"POST {path} HTTP/1.1\r\nHost: me.ts.net\r\nContent-Length: {len(data)}\r\n"
              f"{extra}\r\n".encode() + data)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    s.close()
    head, _, rest = out.partition(b"\r\n\r\n")
    return int(head.split()[1]), rest


def http_get(path, sock_path, read_until=None, login="me@example.com"):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(sock_path)
    who = f"Tailscale-User-Login: {login}\r\n" if login else ""
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n{who}\r\n".encode())
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
        if read_until and data.count(read_until[0]) >= read_until[1]:
            break
    s.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return head.decode(), body


class _ServerCase(unittest.TestCase):
    """A real _RemoteServer on a temp unix socket, with FakeApp behind it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sock = os.path.join(self.dir.name, "r.sock")
        self.app = FakeApp()
        self.stop = threading.Event()
        handler = type("H", (tabit._RemoteHandler,),
                       {"app": self.app, "stop": self.stop, "owner": "me@example.com"})
        self.srv = tabit._RemoteServer(self.sock, handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.dir.cleanup()


class TestRemoteServer(_ServerCase):

    def test_page_status_and_404(self):
        head, body = http_get("/", self.sock)
        self.assertIn("200", head.splitlines()[0])
        self.assertEqual(body, b"<html>page</html>")
        head, body = http_get("/api/status", self.sock)
        self.assertIn("application/json", head)
        self.assertEqual(json.loads(body)["tabs"][0]["ai_status"], "idle")
        head, _ = http_get("/nope", self.sock)
        self.assertIn("404", head.splitlines()[0])

    def test_post_only_from_the_page(self):
        ok = {"Tailscale-User-Login": "me@example.com", "X-Tabit": "1",
              "Content-Type": "application/json", "Origin": "https://me.ts.net"}
        body = json.dumps({"text": "hi"})
        self.assertEqual(http_post("/api/tabs/a1/send", self.sock, body, ok)[0], 200)
        self.assertEqual(self.app.acts, [("a1", "send", {"text": "hi"})])
        for name, change in (
                ("no X-Tabit (a plain cross-site form)", {"X-Tabit": None}),
                ("another site", {"Origin": "https://evil.example"}),
                ("an opaque origin", {"Origin": "null"}),
                ("form encoding", {"Content-Type": "application/x-www-form-urlencoded"}),
                ("a colleague", {"Tailscale-User-Login": "x@example.com"})):
            h = {k: v for k, v in {**ok, **change}.items() if v is not None}
            self.assertEqual(http_post("/api/tabs/a1/send", self.sock, body, h)[0], 403, name)
        self.assertEqual(http_post("/api/tabs/a1/send", self.sock, "[1]", ok)[0], 400)
        self.assertEqual(http_post("/api/tabs/a1/rm", self.sock, body, ok)[0], 404)
        self.assertEqual(http_post("/api/tabs/a1/type", self.sock, json.dumps({"text": "ls"}), ok)[0], 200)
        self.app.acts.pop()
        self.assertEqual(len(self.app.acts), 1)            # none of those got through
        head, body = http_get("/api/tabs/a1/screen", self.sock)
        self.assertEqual((json.loads(body)["lines"], json.loads(body)["color"]), (["hello"], False))
        head, body = http_get("/api/tabs/a1/screen?color=1", self.sock)
        j = json.loads(body)
        self.assertEqual((j["ansi"], j["cols"], "lines" in j), ("\x1b[31mred", 80, False))
        # reading "ready" is a state change: a POST behind the same checks
        self.assertEqual(http_post("/api/tabs/a1/ack", self.sock, "{}", ok)[0], 200)
        self.assertEqual(self.app.acts[-1], ("a1", "ack", None))
        self.assertEqual(http_post("/api/tabs/a1/ack", self.sock, "{}", {**ok, "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(len(self.app.acts), 2)
        for q, want in (("&back=400", 400), ("&back=99999", 3000), ("&back=-5", 0), ("&back=x", 0)):
            head, body = http_get("/api/tabs/a1/screen?color=1" + q, self.sock)
            self.assertEqual(json.loads(body)["back"], want, q)

    def test_manage_routes(self):
        ok = {"Tailscale-User-Login": "me@example.com", "X-Tabit": "1",
              "Content-Type": "application/json", "Origin": "https://me.ts.net"}
        for path, body, want in (
                ("/api/new", {"kind": "shell"}, ("new", None, {"kind": "shell"})),
                ("/api/tabs/a1/close", {}, ("close", "a1", {})),
                ("/api/tabs/a1/group", {"color": "red"}, ("group", "a1", {"color": "red"})),
                ("/api/tabs/a1/move", {"delta": 1}, ("move", "a1", {"delta": 1})),
                ("/api/tabs/a1/rename", {"name": "x"}, ("rename", "a1", {"name": "x"})),
                ("/api/groups/red", {"collapsed": True}, ("groupop", "red", {"collapsed": True}))):
            self.assertEqual(http_post(path, self.sock, json.dumps(body), ok)[0], 200, path)
            self.assertEqual(self.app.acts.pop(), want, path)
            # and none of them without the page's own header or origin
            for change in ({"X-Tabit": None}, {"Origin": "https://evil.example"},
                           {"Tailscale-User-Login": "x@example.com"}):
                h = {k: v for k, v in {**ok, **change}.items() if v is not None}
                self.assertEqual(http_post(path, self.sock, json.dumps(body), h)[0], 403, (path, change))
        self.assertEqual(self.app.acts, [])

    def test_icons(self):
        # the real lookup and rendering, from this repo's SVGs
        self.app._remote_icon = lambda path: tabit.Tabit._remote_icon(
            type("A", (), {"_agent_detect_roots": staticmethod(lambda: [ROOT])})(), path)
        head, body = http_get("/icon.svg", self.sock)
        self.assertIn("image/svg+xml", head)
        self.assertIn(b"<svg", body)
        for path, size in (("/favicon.png", 32), ("/apple-touch-icon.png", 180),
                           ("/icon-192.png", 192), ("/icon-512.png", 512),
                           ("/icon-maskable.png", 512)):
            head, body = http_get(path, self.sock)
            self.assertIn("image/png", head, path)
            self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n", path)
            self.assertEqual(int.from_bytes(body[16:20], "big"), size, path)   # IHDR width
        head, _ = http_get("/icon.svg", self.sock, login="x@example.com")
        self.assertIn("403", head.splitlines()[0])

    def test_manifest_installs_the_page(self):
        head, body = http_get("/manifest.webmanifest", self.sock)
        self.assertIn("application/manifest+json", head)
        m = json.loads(body)
        self.assertEqual(m["display"], "standalone")
        self.assertEqual({i["src"] for i in m["icons"]},
                         {"icon-192.png", "icon-512.png", "icon-maskable.png"})
        head, _ = http_get("/manifest.webmanifest", self.sock, login="x@example.com")
        self.assertIn("403", head.splitlines()[0])

    def test_never_in_a_frame(self):
        head, _ = http_get("/", self.sock)
        self.assertIn("X-Frame-Options: DENY", head)
        self.assertIn("frame-ancestors 'none'", head)

    def test_only_the_owner_gets_in(self):
        for login in (None, "colleague@example.com"):
            head, body = http_get("/api/status", self.sock, login=login)
            self.assertIn("403", head.splitlines()[0])
            self.assertNotIn(b"tabs", body)

    def test_error_is_never_a_status(self):
        self.app._remote_snapshot = lambda: {"error": "RuntimeError: boom"}
        head, body = http_get("/api/status", self.sock)
        self.assertIn("500", head.splitlines()[0])
        old, tabit.REMOTE_PUSH_EVERY = tabit.REMOTE_PUSH_EVERY, 0.1
        try:
            head, body = http_get("/api/events", self.sock, read_until=(b"event: alive", 2))
        finally:
            tabit.REMOTE_PUSH_EVERY = old
        self.assertNotIn(b"event: status", body)

    def test_old_streams_give_way(self):
        old = tabit.REMOTE_PUSH_EVERY, tabit.REMOTE_MAX_STREAMS
        tabit.REMOTE_PUSH_EVERY, tabit.REMOTE_MAX_STREAMS = 0.2, 2
        self.addCleanup(lambda: (setattr(tabit, "REMOTE_PUSH_EVERY", old[0]),
                                 setattr(tabit, "REMOTE_MAX_STREAMS", old[1])))
        socks = []
        for _ in range(3):                  # a third stream: the first must end
            c = socket.socket(socket.AF_UNIX)
            c.settimeout(3)
            c.connect(self.sock)
            c.sendall(b"GET /api/events HTTP/1.1\r\nHost: x\r\n"
                      b"Tailscale-User-Login: me@example.com\r\n\r\n")
            c.recv(4096)
            socks.append(c)
            __import__("time").sleep(0.3)

        seen = b""

        def closed(c, within=1.5):
            nonlocal seen
            end = __import__("time").monotonic() + within
            c.settimeout(0.3)
            while __import__("time").monotonic() < end:
                try:
                    data = c.recv(65536)
                    if not data:
                        return True             # the server hung up
                    seen += data
                except socket.timeout:
                    pass
            return False
        self.assertTrue(closed(socks[0]))
        self.assertIn(b"retry: 30000", seen)    # and it waits before coming back
        self.assertFalse(closed(socks[2]))
        for c in socks:
            c.close()

    def test_events_push_first_then_on_change(self):
        old, tabit.REMOTE_PUSH_EVERY = tabit.REMOTE_PUSH_EVERY, 0.2
        self.addCleanup(setattr, tabit, "REMOTE_PUSH_EVERY", old)
        got = []

        def read():
            got.append(http_get("/api/events", self.sock, read_until=(b"event: status", 2)))
        t = threading.Thread(target=read)
        t.start()
        threading.Timer(0.5, lambda: self.app.snap.update(
            tabs=[{"id": "a", "type": "ai", "ai_status": "blocked"}], time=2)).start()
        t.join(5)
        head, body = got[0]
        self.assertIn("text/event-stream", head)
        lines = body.decode().splitlines()
        events = [json.loads(lines[i + 1][6:]) for i, line in enumerate(lines)
                  if line == "event: status"]
        self.assertEqual([e["tabs"][0]["ai_status"] for e in events], ["idle", "blocked"])
        self.assertIn("event: alive", body.decode())  # unchanged checks say only "alive"




class TestRemoteFiles(_ServerCase):
    """GET /api/file: what the phone may open, and how it is sent."""

    def setUp(self):
        super().setUp()
        d = self.dir.name                       # under /tmp: an allowed root
        self.md = os.path.join(d, "notes.md")
        with open(self.md, "w") as f:
            f.write("# Title\n\n- one\n- two\n\n<script>alert(1)</script>\n")
        os.makedirs(os.path.join(d, ".ssh"))
        for name in (".ssh/config.txt", "id_rsa.txt", ".env", "server.key", "blob.bin"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        with open(os.path.join(d, "big.txt"), "w") as f:
            f.write("x" * (tabit.REMOTE_FILE_MAX + 1))
        os.symlink("/etc/passwd", os.path.join(d, "innocent.txt"))
        for name in (".config/gh/hosts.yml", ".config/rclone/rclone.conf",
                     "application_default_credentials.json", "github_token.txt",
                     "tokenizer.py"):
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        os.mkfifo(os.path.join(d, "pipe.md"))

    def get(self, path, render=False):
        q = "/api/file?path=" + __import__("urllib.parse").parse.quote(path)
        head, body = http_get(q + ("&render=1" if render else ""), self.sock)
        return int(head.split()[1]), head, body

    def test_markdown_is_laid_out_and_sandboxed(self):
        code, head, body = self.get(self.md, render=True)
        self.assertEqual(code, 200)
        self.assertIn(b"<h1>Title</h1>", body)
        self.assertIn(b"<li>one</li>", body)
        self.assertIn("Content-Security-Policy: sandbox", head)   # the <script> cannot run
        code, head, body = self.get(self.md)
        self.assertIn("text/plain", head)
        self.assertTrue(body.startswith(b"# Title"))

    def test_refused(self):
        d = self.dir.name
        for path, code in (
                ("/etc/hostname", 403),                       # outside home and /tmp
                (os.path.join(d, "innocent.txt"), 403),       # symlink out to /etc/passwd
                (os.path.join(d, ".ssh/config.txt"), 403),
                (os.path.join(d, "id_rsa.txt"), 403),
                (os.path.join(d, ".env"), 403),
                (os.path.join(d, "server.key"), 403),
                (os.path.join(d, "blob.bin"), 403),           # not a document type
                (os.path.join(d, ".config/gh/hosts.yml"), 403),    # tokens
                (os.path.join(d, ".config/rclone/rclone.conf"), 403),
                (os.path.join(d, "application_default_credentials.json"), 403),
                (os.path.join(d, "github_token.txt"), 403),
                (os.path.join(d, "pipe.md"), 403),            # a FIFO would never end
                (os.path.join(d, "big.txt"), 413),
                (os.path.join(d, "nope.md"), 404),
                ("notes.md", 400)):                           # not a full path
            got, head, body = self.get(path)
            self.assertEqual(got, code, path)
            self.assertNotIn(b"root:", body)
            self.assertIn("Content-Security-Policy: sandbox", head)

    def test_svg_shows_but_its_script_cannot_run(self):
        svg = os.path.join(self.dir.name, "logo.svg")
        with open(svg, "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
                    '<rect width="9" height="9"/></svg>')
        code, head, body = self.get(svg, render=True)
        self.assertEqual(code, 200)
        self.assertIn("Content-Type: image/svg+xml", head)
        csp = re.search(r"Content-Security-Policy: (.*)", head).group(1)
        self.assertTrue(csp.startswith("sandbox;"))            # no allow-scripts
        self.assertIn("default-src 'none'", csp)                # and nothing loads
        self.assertNotIn("script-src", csp)
        code, head, body = self.get(svg)                        # txt: the source
        self.assertIn("text/plain", head)
        self.assertTrue(body.startswith(b"<svg"))

    def test_a_name_with_token_inside_opens(self):
        self.assertEqual(self.get(os.path.join(self.dir.name, "tokenizer.py"))[0], 200)

    def test_front_matter_is_a_block(self):
        fm = os.path.join(self.dir.name, "h.md")
        with open(fm, "w") as f:
            f.write("---\ntitle: 'x'\nstatus: wip\n---\n\n# Goal\n")
        code, head, body = self.get(fm, render=True)
        self.assertIn(b"<pre>title: &#x27;x&#x27;\nstatus: wip</pre>", body)
        self.assertIn(b"<h1>Goal</h1>", body)

    def test_switch_off(self):
        self.app._remote_files_on = False
        self.assertEqual(self.get(self.md)[0], 403)

    def test_not_for_a_stranger(self):
        q = "/api/file?path=" + self.md
        head, _ = http_get(q, self.sock, login="x@example.com")
        self.assertIn("403", head.splitlines()[0])


class TestRemoteUpload(_ServerCase):
    """POST /api/upload: only into the uploads folder, never over a file."""

    def setUp(self):
        super().setUp()
        self.up = os.path.join(self.dir.name, "uploads")
        old, tabit.REMOTE_UPLOAD_DIR = tabit.REMOTE_UPLOAD_DIR, self.up
        self.addCleanup(setattr, tabit, "REMOTE_UPLOAD_DIR", old)
        self.ok = {"Tailscale-User-Login": "me@example.com", "X-Tabit": "1",
                   "Content-Type": "application/octet-stream", "Origin": "https://me.ts.net"}

    def put(self, name, data, headers=None):
        q = "/api/upload?name=" + __import__("urllib.parse").parse.quote(name)
        code, body = http_post(q, self.sock, "", {**self.ok, **(headers or {})}) if data is None \
            else self._raw(q, data, {**self.ok, **(headers or {})})
        try:
            return code, json.loads(body or b"{}")
        except ValueError:
            return code, {"text": body.decode()}          # the plain "not your tabit"

    def _raw(self, path, data, headers):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(5)
        s.connect(self.sock)
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items() if v is not None)
        length = headers.get("Content-Length", len(data))
        s.sendall(f"POST {path} HTTP/1.1\r\nHost: me.ts.net\r\n"
                  f"Content-Length: {length}\r\n{extra}\r\n".encode() + data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
        s.close()
        head, _, rest = out.partition(b"\r\n\r\n")
        return int(head.split()[1]), rest

    def test_saved_in_the_uploads_folder(self):
        code, body = self.put("shot.png", b"\x89PNG data")
        self.assertEqual(code, 200)
        self.assertTrue(body["path"].startswith(self.up + os.sep))
        with open(body["path"], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG data")
        code, again = self.put("shot.png", b"second")          # same name: no overwrite
        self.assertTrue(again["path"].endswith("shot-1.png"))
        with open(body["path"], "rb") as f:
            self.assertEqual(f.read(), b"\x89PNG data")

    def test_names_cannot_climb_out(self):
        for name, want in (("../../etc/passwd", "passwd"), (".bashrc", "bashrc"),
                           ("a/b\\c.txt", "c.txt"), ("截圖 1.png", "截圖_1.png"),
                           ("", "upload"), ("x;rm -rf ~.sh", "x_rm_-rf__.sh"),
                           ("_.hidden", "hidden")):
            code, body = self.put(name, b"x")
            self.assertEqual(code, 200, name)
            self.assertEqual(os.path.dirname(os.path.dirname(body["path"])), self.up, name)
            self.assertEqual(os.path.basename(body["path"]), want, name)

    def test_refused(self):
        self.assertEqual(self.put("e.txt", b"")[0], 400)                         # empty
        self.assertEqual(self.put("b.bin", b"x", {"Content-Length": str(tabit.REMOTE_UPLOAD_MAX + 1)})[0], 413)
        self.assertEqual(self.put("j.txt", b"x", {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.put("n.txt", b"x", {"X-Tabit": None})[0], 403)
        self.assertEqual(self.put("o.txt", b"x", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.put("s.txt", b"x", {"Tailscale-User-Login": "x@example.com"})[0], 403)
        self.app._remote_send_on = False
        self.assertEqual(self.put("off.txt", b"x")[0], 403)
        self.assertFalse(os.path.exists(self.up) and any(
            f for _r, _d, fs in os.walk(self.up) for f in fs))                # nothing written


FAKE_TAILSCALE = r"""#!%(py)s
import json, os, sys
S = os.environ["FAKE_TS"]
st = json.load(open(S + "/state.json"))
args = sys.argv[1:]
open(S + "/calls.log", "a").write(" ".join(["tailscale"] + args) + "\n")
def save():
    json.dump(st, open(S + "/state.json", "w"))
if args == ["status", "--json"]:
    if not st.get("daemon"):
        sys.exit("failed to connect to local tailscaled; it doesn't appear to be running")
    if st.get("status_garbage"):
        sys.exit("permission denied")
    print(json.dumps({"BackendState": st["state"], "Self": {"UserID": 7},
                      "User": {"7": {"LoginName": "me@example.com"}}}))
elif args == ["debug", "prefs"]:
    if st.get("prefs_broken"):
        sys.exit("debug prefs: access denied")
    print(json.dumps({"OperatorUser": st.get("operator", "")}))
elif args[:1] == ["set"]:
    st["operator"] = args[1].split("=", 1)[1]; save()
elif args == ["up"]:
    if st.get("up_refuses"):
        print("Error: changing settings via 'tailscale up' requires mentioning all non-default flags")
        sys.exit(1)
    print("To authenticate, visit:\n\n\thttps://login.tailscale.com/a/FAKE\n", flush=True)
    st["state"] = "Running"; save()
"""

FAKE_SYSTEMCTL = r"""#!%(py)s
import json, os, sys
S = os.environ["FAKE_TS"]
st = json.load(open(S + "/state.json"))
if sys.argv[1:2] == ["is-active"]:
    print(st.get("active", "active" if st.get("daemon") else "inactive"))
"""


class FakeTerm:
    """A VTE terminal: cursor at (col, row) and what the rows from it read."""
    def __init__(self, text, col=4, row=10):
        self.text, self.pos = text, (col, row)

    def get_cursor_position(self):
        return self.pos

    def get_column_count(self):
        return 80

    def get_row_count(self):
        return 24

    def get_text_range(self, *_a):
        return self.text, None


class TestRemoteArrows(unittest.TestCase):
    """An arrow goes in as a key press, so a pager in its own key mode gets it."""

    def _press(self, name, pressed):
        app = unittest.mock.Mock(_remote_send_on=True, _REMOTE_KEYS=tabit.Tabit._REMOTE_KEYS,
                                 _REMOTE_ARROWS=tabit.Tabit._REMOTE_ARROWS)
        app._remote_tab_trouble.return_value = None
        app._press_key.return_value = pressed
        row = app._tg_row_by_id.return_value
        code, _ = tabit.Tabit._remote_act(app, "t", "key", {"key": name})
        return code, app, row

    def test_an_arrow_is_a_key_press(self):
        code, app, row = self._press("up", True)
        self.assertEqual(code, 200)
        app._press_key.assert_called_once_with(row.term, tabit.Gdk.KEY_Up)
        row.term.feed_child.assert_not_called()

    def test_no_window_falls_back_to_bytes(self):
        _, _, row = self._press("down", False)
        row.term.feed_child.assert_called_once_with(b"\x1b[B")

    def test_other_keys_stay_bytes(self):
        _, app, row = self._press("enter", True)
        app._press_key.assert_not_called()
        row.term.feed_child.assert_called_once_with(b"\r")


class TestRemoteCursor(unittest.TestCase):

    def test_cursor_counts_lines_from_its_row(self):
        lines = ["old", "$ ", "status bar"]
        out = tabit.Tabit._remote_cursor(FakeTerm("$ \n\nstatus bar\n\n"), lines)
        self.assertEqual(out, {"cursor": [4, 0], "rows": 3})
        self.assertEqual(lines, ["old", "$ ", "status bar"])

    def test_blank_last_row_gets_a_line(self):
        lines = ["$ ls", "a b"]
        out = tabit.Tabit._remote_cursor(FakeTerm("\n\n"), lines)
        self.assertEqual((out["rows"], lines), (1, ["$ ls", "a b", ""]))

    def test_a_wrapped_line_counts_its_rows_above(self):
        # an 80-column screen: row 8 is "old", rows 9 and 10 are one long
        # line that wrapped; the cursor sits at column 4 of row 10
        class Wrapped(FakeTerm):
            def get_text_range(self, r0, _c0, r1, _c1, _attrs):
                if (r0, r1) == (9, 10):
                    return "x" * 84, None       # joined: no line break
                if (r0, r1) == (8, 9):
                    return "old\n" + "x" * 80, None
                return "x" * 84 + "\n\n", None
        out = tabit.Tabit._remote_cursor(Wrapped(None, col=4, row=10), ["x" * 84])
        self.assertEqual(out["cursor"], [84, 0])

    def test_failed_read_gives_no_cursor(self):
        lines = ["$ ls"]
        self.assertEqual(tabit.Tabit._remote_cursor(FakeTerm(None), lines), {})
        self.assertEqual(lines, ["$ ls"])


class TestRemoteRename(unittest.TestCase):

    def rename(self, kind, name):
        row = type("Row", (), {"kind": kind})()
        app = type("App", (), {"_tg_row_by_id": lambda _s, _id: row, "_save_sessions": lambda _s: None,
                               "_set_tab_name": lambda _s, _r, n: setattr(row, "name", n)})()
        code, _out = tabit.Tabit._remote_manage_op(app, "rename", "x", {"name": name})
        return code, getattr(row, "name", None)

    def test_rename(self):
        self.assertEqual(self.rename("term", "  build  "), (200, "build"))
        self.assertEqual(self.rename("term", "   "), (400, None))
        self.assertEqual(self.rename("term", "x" * 81), (400, None))

    def test_unsaved_note_mark_is_not_kept(self):
        self.assertEqual(self.rename("note", "todo *"), (200, "todo"))



class TestRemoteFileDeny(unittest.TestCase):
    """Agent logins are keys too: never opened from the page."""

    def test_agent_logins(self):
        home = os.path.realpath(os.path.expanduser("~"))
        why = tabit._RemoteHandler._file_problem
        self.assertTrue(why(os.path.join(home, ".codex", "auth.json")))
        self.assertTrue(why(os.path.join(home, ".grok", "auth.json")))
        self.assertIsNone(why(os.path.join(home, "notes", "oauth-plan.md")))

    def test_tabit_own_state(self):
        # connect_last.json and the argv in sessions.json can hold passwords
        why = tabit._RemoteHandler._file_problem
        conf = os.path.realpath(tabit.CONFIG_DIR)
        self.assertTrue(why(os.path.join(conf, "connect_last.json")))
        self.assertTrue(why(os.path.join(conf, "sessions.json")))


class TestRemoteGroupColour(unittest.TestCase):
    """Recolour a group, and make one of the tabs without a group."""

    def setUp(self):
        Row = type("Row", (), {})
        self.rows = [Row(), Row(), Row()]
        for r, c in zip(self.rows, ("red", "red", None)):
            r.group_color = c
        test = self

        class App:
            _group_names = {"red": "Work"}
            _collapsed_groups = {"red"}
            _session_rows = lambda _s: test.rows
            _relayout = _save_sessions = _save_group_names = _save_collapsed_groups = lambda _s: None

            def _apply_group(self, r, c):
                r.group_color = c

        App._recolor_group = tabit.Tabit._recolor_group
        self.app = App()

    def op(self, color, body):
        return tabit.Tabit._remote_group_op(self.app, color, body)[0]

    def colours(self):
        return [r.group_color for r in self.rows]

    def test_recolour_keeps_name_and_fold(self):
        self.assertEqual(self.op("red", {"color": "blue"}), 200)
        self.assertEqual(self.colours(), ["blue", "blue", None])
        self.assertEqual(self.app._group_names, {"blue": "Work"})
        self.assertEqual(self.app._collapsed_groups, {"blue"})

    def test_recolour_refuses_a_used_or_unknown_colour(self):
        self.rows[2].group_color = "green"
        self.assertEqual(self.op("red", {"color": "green"}), 409)
        self.assertEqual(self.op("red", {"color": "nope"}), 400)
        self.assertEqual(self.op("red", {"color": "blue", "name": "x" * 41}), 400)
        self.assertEqual(self.colours(), ["red", "red", "green"])

    def test_loose_tabs_become_a_group(self):
        self.assertEqual(self.op("red", {"loose": True}), 409)
        self.assertEqual(self.op("teal", {"loose": True, "name": " Misc "}), 200)
        self.assertEqual(self.colours(), ["red", "red", "teal"])
        self.assertEqual(self.app._group_names["teal"], "Misc")
        self.assertEqual(self.op("pink", {"loose": True}), 404)   # none left

    def test_every_check_comes_before_a_change(self):
        self.assertEqual(self.op("nope", {}), 404)
        self.assertEqual(self.op("teal", {"loose": True, "color": "red"}), 409)
        self.assertEqual(self.colours(), ["red", "red", None])

    def test_desk_keeps_the_recoloured_header_selected(self):
        test, headers = self, {"red": object()}
        picked = []

        class Box:
            get_selected_row = lambda _s: test.hdr if not picked and not test.done else None
            select_row = lambda _s, r: picked.append(r)

        class Hdr:
            kind, group_color = "group_header", "red"
            get_parent = get_visible = lambda _s: True

        self.hdr = Hdr()
        app = self.app
        app.listbox = Box()
        app._group_header_row = lambda c: headers.get(c)
        app._remote_send_on = True
        app._remote_manage_op = lambda op, t, b: (headers.pop("red"), headers.update(blue=test.hdr), (200, {}))[2]
        self.done = False
        op = app._remote_manage_op
        app._remote_manage_op = lambda *a: (setattr(test, "done", True), op(*a))[1]   # the relayout drops it
        tabit.Tabit._remote_manage(app, "groupop", "red", {"color": "blue"})
        self.assertEqual(picked, [self.hdr])

class TestTailscaleSetup(unittest.TestCase):
    """_tailscale_prepare against a fake tailscale: what it asks, runs and
    opens. Counts matter here: a retry every minute must never nag."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.bin = os.path.join(d, "bin")
        os.makedirs(self.bin)
        self._script("systemctl", FAKE_SYSTEMCTL)
        env = {"FAKE_TS": d, "PATH": self.bin, "USER": "me"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old.items()])
        self.asked, self.sudo, self.opened, self.declined = [], [], [], []
        self.answer = True
        self.srv = object()
        test = self

        class Stub:
            _remote_srv = self.srv

            def _remote_ask(self, title, body):
                test.asked.append(body)
                return test.answer

            def _sudo(self, argv, timeout):
                test.sudo.append(" ".join(argv[:3]))
                if argv[0] == "sh":                     # the install script
                    test._script("tailscale", FAKE_TAILSCALE)
                elif argv[0] == "systemctl":
                    test.state(daemon=True, active="active")
                else:
                    __import__("subprocess").run(argv, check=True)

            def _remote_show(self, *_a):
                return False

            def _open_uri(self, uri):
                test.opened.append(uri)

            def _remote_decline(self):
                test.declined.append(1)
                return False
        self.app = Stub()

    def _script(self, name, text):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(text % {"py": sys.executable})
        os.chmod(path, 0o755)

    def state(self, **kw):
        path = os.path.join(self.dir.name, "state.json")
        st = {}
        if os.path.exists(path):
            with open(path) as f:
                st = json.load(f)
        st.update(kw)
        with open(path, "w") as f:
            json.dump(st, f)

    def prepare(self):
        out = tabit.Tabit._tailscale_prepare(self.app, self.srv)
        ctx = tabit.GLib.MainContext.default()
        while ctx.pending():                            # the idle_add callbacks
            ctx.iteration(False)
        return out

    def ready(self, **kw):
        self._script("tailscale", FAKE_TAILSCALE)
        self.state(**{"daemon": True, "state": "Running", "operator": "me", **kw})

    def test_fresh_computer(self):
        self.state(state="NeedsLogin")
        self.assertIsNone(self.prepare())
        self.assertEqual(len(self.asked), 1)            # one question for all of it
        self.assertEqual(self.sudo, ["sh -c curl -fsSL https://tailscale.com/install.sh | sh",
                                     "systemctl enable --now", "tailscale set --operator=me"])
        self.assertEqual(self.opened, ["https://login.tailscale.com/a/FAKE"])

    def test_ready_asks_nothing(self):
        self.ready()
        self.assertIsNone(self.prepare())
        self.assertEqual((self.asked, self.sudo, self.opened), ([], [], []))

    def test_no_runs_nothing_and_turns_remote_off(self):
        self.answer = False
        self.state(state="NeedsLogin")
        self.assertIn("declined", self.prepare())
        self.assertEqual((len(self.asked), self.sudo, self.declined), (1, [], [1]))
        self.assertIn("declined", self.prepare())      # a retry does not ask again
        self.assertEqual(len(self.asked), 1)

    def test_starting_or_waiting_for_approval_retries_quietly(self):
        for state in ("Starting", "NeedsMachineAuth"):
            self.ready(state=state)
            self.assertTrue(self.prepare().startswith("retry:"), state)
        self.state(daemon=False, active="activating")    # just after a boot
        self.assertTrue(self.prepare().startswith("retry:"))
        self.assertEqual((self.asked, self.sudo), ([], []))

    def test_daemon_up_but_not_answering_does_not_loop(self):
        self.ready(status_garbage=True)
        out = self.prepare()
        self.assertFalse(out.startswith("retry:"))
        self.assertIn("permission denied", out)
        self.assertEqual((self.asked, self.sudo), ([], []))

    def test_unreadable_operator_is_fixed_once(self):
        self.ready(prefs_broken=True)
        self.assertIsNone(self.prepare())
        self.assertEqual((len(self.asked), self.sudo), (1, ["tailscale set --operator=me"]))
        out = self.prepare()                            # the next retry
        self.assertIn("did not help", out)
        self.assertFalse(out.startswith("retry:"))
        self.assertEqual((len(self.asked), len(self.sudo)), (1, 1))

    def test_login_error_is_shown(self):
        self.ready(state="NeedsLogin", up_refuses=True)
        self.assertIn("non-default flags", self.prepare())
        self.assertEqual(self.opened, [])

    def test_nothing_runs_once_remote_is_off(self):
        self.state(daemon=True, state="NeedsLogin", operator="")
        self._script("tailscale", FAKE_TAILSCALE)
        self.app._remote_ask = lambda title, body: (setattr(self.app, "_remote_srv", None), True)[1]
        self.assertIn("turned off", self.prepare())
        self.assertEqual(self.sudo, [])

    def test_old_failure_leaves_a_new_server_alone(self):
        stopped = []
        stub = type("S", (), {"_remote_srv": "new", "_remote_stop": lambda s, unpublish: stopped.append(1)})()
        self.assertFalse(tabit.Tabit._remote_fail(stub, "old", "boom"))
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()
