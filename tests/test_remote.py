#!/usr/bin/env python3
"""Remote page server: routes and the SSE stream, on a real unix socket."""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest

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

    def _remote_screen(self, rid, color=False, ack=False):
        self.acked = ack
        return 200, {"id": rid, "lines": ["hello"], "color": color}, ("sess" if color else None)

    @staticmethod
    def _tmux_pane_ansi(name, back=0):
        return {"ansi": "\x1b[31mred", "cols": 80, "rows": 24, "back": back} if name == "sess" else None

    def _remote_act(self, rid, action, body):
        self.acts.append((rid, action, body))
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
        http_get("/api/tabs/a1/screen?color=1&ack=1", self.sock)
        self.assertTrue(self.app.acked)
        http_get("/api/tabs/a1/screen?color=1", self.sock)
        self.assertFalse(self.app.acked)
        for q, want in (("&back=400", 400), ("&back=99999", 3000), ("&back=-5", 0), ("&back=x", 0)):
            head, body = http_get("/api/tabs/a1/screen?color=1" + q, self.sock)
            self.assertEqual(json.loads(body)["back"], want, q)

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

        def closed(c, within=1.5):
            end = __import__("time").monotonic() + within
            c.settimeout(0.3)
            while __import__("time").monotonic() < end:
                try:
                    if not c.recv(65536):
                        return True             # the server hung up
                except socket.timeout:
                    pass
            return False
        self.assertTrue(closed(socks[0]))
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
                (os.path.join(d, "big.txt"), 413),
                (os.path.join(d, "nope.md"), 404),
                ("notes.md", 400)):                           # not a full path
            got, head, body = self.get(path)
            self.assertEqual(got, code, path)
            self.assertNotIn(b"root:", body)
            self.assertIn("Content-Security-Policy: sandbox", head)

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


if __name__ == "__main__":
    unittest.main()
