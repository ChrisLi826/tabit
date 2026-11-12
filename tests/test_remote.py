#!/usr/bin/env python3
"""Remote page server: routes and the SSE stream, on a real unix socket."""
import json
import os
import re
import shutil
import socket
import urllib.parse
import sys
import tempfile
import threading
import time
import types
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

    def _remote_page(self, name="remote.html"):
        return b"<html>page</html>" if name == "remote.html" else b"<html>chat</html>"

    def _remote_snapshot(self):
        return dict(self.snap)

    def _remote_call(self, fn, timeout=3):
        return fn()

    _remote_wanted_room = staticmethod(tabit.Tabit._remote_wanted_room)

    def _remote_screen(self, rid, color=False, back=0, want=None):
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
        head, body = http_get("/", self.sock)                # the address opens the chat
        self.assertIn("200", head.splitlines()[0])
        self.assertEqual(body, b"<html>chat</html>")
        self.assertEqual(http_get("/terminal", self.sock)[1], b"<html>page</html>")
        self.assertEqual(http_get("/chat", self.sock)[1], b"<html>chat</html>")   # old links still work
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

    def test_dirs_lists_folders_on_this_machine(self):
        d = tempfile.mkdtemp(dir=self.dir.name)
        os.mkdir(os.path.join(d, "Sub"))
        os.mkdir(os.path.join(d, ".secret"))
        with open(os.path.join(d, "note.txt"), "w") as f:
            f.write("x")
        head, body = http_get("/api/dirs?path=" + urllib.parse.quote(d), self.sock)
        self.assertIn("200", head.splitlines()[0])
        got = json.loads(body)
        self.assertEqual(got["path"], os.path.realpath(d))
        self.assertEqual([x["name"] for x in got["dirs"]], ["Sub"])
        head, body = http_get("/api/dirs?path=" + urllib.parse.quote(d) + "&hidden=1", self.sock)
        self.assertEqual([x["name"] for x in json.loads(body)["dirs"]], [".secret", "Sub"])
        self.assertEqual(got["parent"], os.path.dirname(os.path.realpath(d)))
        head, _ = http_get("/api/dirs?path=" + urllib.parse.quote(
            os.path.join(d, "note.txt")), self.sock)
        self.assertIn("400", head.splitlines()[0])
        self.app._remote_send_on = False
        head, _ = http_get("/api/dirs?path=" + urllib.parse.quote(d), self.sock)
        self.assertIn("403", head.splitlines()[0])

    def test_new_folder_makes_one_plain_folder(self):
        ok = {"Tailscale-User-Login": "me@example.com", "X-Tabit": "1",
              "Content-Type": "application/json", "Origin": "https://me.ts.net"}
        d = tempfile.mkdtemp(dir=self.dir.name)

        def make(name, parent=d, headers=ok):
            return http_post("/api/dirs/new", self.sock,
                             json.dumps({"parent": parent, "name": name}), headers)[0]
        self.assertEqual(make("fresh"), 200)
        self.assertTrue(os.path.isdir(os.path.join(d, "fresh")))
        self.assertEqual(make("fresh"), 409)                    # already there
        for bad in ("", " ", ".", "..", "a/b", "../x", "x\0y"):
            self.assertEqual(make(bad), 400, repr(bad))
        self.assertEqual(os.listdir(d), ["fresh"])              # nothing else got made
        self.assertEqual(make("x", parent=os.path.join(d, "nope")), 400)
        self.assertEqual(make("y", headers={k: v for k, v in ok.items() if k != "X-Tabit"}), 403)
        self.app._remote_send_on = False
        self.assertEqual(make("z"), 403)

    def test_new_options_lists_tools_and_hides_secrets(self):
        path = os.path.join(self.dir.name, "last.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "sn": "ABC", "password": "sekret-token-xyz", "email": "a@b.c",
                "ticket": "tick-token", "gateway_pass": "gw-token",
                "use_tmux": True, "env": "lab",
            }, f)
        old_file, old_has = tabit.CONNECT_LAST_FILE, tabit.HAS_SSH_TOOL
        old_envs = tabit.Tabit._ssh_tool_envs
        tabit.CONNECT_LAST_FILE = path
        tabit.HAS_SSH_TOOL = True
        tabit.Tabit._ssh_tool_envs = staticmethod(lambda: ["prod", "lab"])
        try:
            head, body = http_get("/api/new-options", self.sock)
            self.assertIn("200", head.splitlines()[0])
            got = json.loads(body)
            self.assertEqual(got["baud"], "115200")
            self.assertEqual(got["tools"][0], "screen")
            self.assertTrue(got["connect"])
            self.assertNotIn(b"sekret-token-xyz", body)
            self.assertNotIn(b"tick-token", body)
            self.assertNotIn(b"gw-token", body)
            self.assertEqual(got["connect_form"]["sn"], "ABC")
            self.assertEqual(got["connect_form"]["email"], "a@b.c")
            self.assertTrue(got["connect_form"]["saved_password"])
            self.assertEqual(got["connect_form"]["envs"], ["prod", "lab"])
            self.app._remote_send_on = False
            head, _ = http_get("/api/new-options", self.sock)
            self.assertIn("403", head.splitlines()[0])
        finally:
            tabit.CONNECT_LAST_FILE = old_file
            tabit.HAS_SSH_TOOL = old_has
            tabit.Tabit._ssh_tool_envs = old_envs
            self.app._remote_send_on = True

    def test_icons(self):
        # the real lookup and rendering, from this repo's SVGs
        self.app._remote_icon = lambda path: tabit.Tabit._remote_icon(
            type("A", (), {"_agent_detect_roots": staticmethod(lambda: [ROOT])})(), path)
        head, body = http_get("/icon.svg", self.sock)
        self.assertIn("image/svg+xml", head)
        self.assertIn(b"<svg", body)
        self.assertIn(b"chat bubble", body)     # the phone's icon, not the desk's
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


class TestPlainTabColours(unittest.TestCase):
    """A plain tab's screen comes with the colours tabit draws."""

    def _term(self, text, colours):
        C = lambda r, g, b: type("C", (), {"red": r << 8, "green": g << 8, "blue": b << 8})()
        attrs = [type("A", (), {"fore": C(*f), "back": C(*b)})() for f, b in colours]
        t = unittest.mock.Mock()
        t.get_cursor_position.return_value = (2, 1)
        t.get_column_count.return_value = 80
        t.get_row_count.return_value = 24
        t.get_text_range.return_value = (text, attrs)
        return t

    def test_only_a_changed_colour_gets_a_code(self):
        fg, bg, red = (200, 200, 200), (20, 20, 30), (240, 80, 80)
        text = "ok red\n$ \n"
        cols = [(fg, bg)] * 3 + [(red, bg)] * 3 + [(fg, bg)] * 4
        lines, ansi = tabit.Tabit._term_tail_ansi(self._term(text, cols))
        self.assertEqual(lines, ["ok red", "$ "])
        self.assertEqual(ansi, ["ok \x1b[0;38;2;240;80;80mred\x1b[0m", "$ "])

    def test_wide_text_keeps_its_colours(self):
        # VTE gives one colour per UTF-8 byte: 已 is three
        fg, bg, red = (200, 200, 200), (20, 20, 30), (240, 80, 80)
        text = "a已b\n"
        cols = [(fg, bg)] + [(red, bg)] * 3 + [(fg, bg)] * 2
        lines, ansi = tabit.Tabit._term_tail_ansi(self._term(text, cols))
        self.assertEqual(lines, ["a已b"])
        self.assertEqual(ansi, ["a\x1b[0;38;2;240;80;80m已\x1b[0mb"])

    def test_no_colours_reads_as_none(self):
        t = self._term("abc", [])
        self.assertIsNone(tabit.Tabit._term_tail_ansi(t))


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
        row.term.feed_child.assert_called_once_with(b"\x1bOB")

    def test_page_keys_are_key_presses_too(self):
        # less and git diff page with them
        for name, keyval, raw in (("pgup", tabit.Gdk.KEY_Page_Up, b"\x1b[5~"),
                                  ("pgdn", tabit.Gdk.KEY_Page_Down, b"\x1b[6~"),
                                  ("home", tabit.Gdk.KEY_Home, b"\x1bOH"),
                                  ("end", tabit.Gdk.KEY_End, b"\x1bOF")):
            code, app, row = self._press(name, True)
            self.assertEqual(code, 200)
            app._press_key.assert_called_once_with(row.term, keyval)
            _, _, row = self._press(name, False)
            row.term.feed_child.assert_called_once_with(raw)

    def test_a_tab_not_shown_gets_the_keys_a_pager_wants(self):
        # no window: tabit took its page out. less only takes ESC O for
        # arrows, Home and End; a shell takes it too
        for name, raw in (("up", b"\x1bOA"), ("down", b"\x1bOB"), ("right", b"\x1bOC"),
                          ("left", b"\x1bOD"), ("home", b"\x1bOH"), ("end", b"\x1bOF")):
            _, _, row = self._press(name, False)
            row.term.feed_child.assert_called_once_with(raw)

    def test_other_keys_stay_bytes(self):
        _, app, row = self._press("enter", True)
        app._press_key.assert_not_called()
        row.term.feed_child.assert_called_once_with(b"\r")

    def test_esc_and_ctrl_c_are_noted_for_the_status(self):
        # they run no claude hook, so the status needs to hear of them
        for name in ("esc", "ctrl-c"):
            _, app, row = self._press(name, True)
            app._agent_note_interrupt.assert_called_once_with(row)
        _, app, _ = self._press("enter", True)
        app._agent_note_interrupt.assert_not_called()

    def test_a_scrolled_tmux_tab_comes_back_down_first(self):
        # a tmux tab scrolled up eats keys as copy-mode commands
        for name in ("enter", "up"):
            _, app, row = self._press(name, True)
            app._tmux_unscroll.assert_called_once_with(row)
        app = unittest.mock.Mock(_remote_send_on=True, _TG_CONTROL=tabit.Tabit._TG_CONTROL)
        app._remote_tab_trouble.return_value = None
        row = app._tg_row_by_id.return_value
        tabit.Tabit._remote_act(app, "t", "type", {"text": "hi"})
        app._tmux_unscroll.assert_called_once_with(row)
        app = unittest.mock.Mock(_tg_safe_text=lambda t: t)
        with unittest.mock.patch.object(tabit.GLib, "timeout_add"):
            tabit.Tabit._tg_paste(app, row, "task")
        app._tmux_unscroll.assert_called_once_with(row)


class TestInjectEnter(unittest.TestCase):
    """The Enter comes a moment after the paste, one text at a time."""

    def setUp(self):
        self.app = unittest.mock.Mock(_tg_safe_text=lambda t: t, _TG_ENTER_MS=300)
        for name in ("_tg_inject", "_tg_paste", "_tg_enter"):
            setattr(self.app, name, getattr(tabit.Tabit, name).__get__(self.app))
        self.row = unittest.mock.Mock(_tg_typing=None)
        self.timers = []
        patch = unittest.mock.patch.object(
            tabit.GLib, "timeout_add",
            side_effect=lambda ms, fn, *a: self.timers.append((ms, fn, a)))
        patch.start()
        self.addCleanup(patch.stop)

    def keys(self):
        return [c.args[0] for c in self.row.term.method_calls]

    def tick(self):
        ms, fn, a = self.timers.pop(0)
        self.assertEqual(ms, 300)
        self.assertFalse(fn(*a))

    def test_the_enter_waits_for_the_timer(self):
        # Claude Code loses an Enter that comes with a paste ending in a path
        self.app._tg_inject(self.row, "look\n/tmp/a.png")
        self.assertEqual(self.keys(), ["look\n/tmp/a.png"])
        self.tick()
        self.assertEqual(self.keys(), ["look\n/tmp/a.png", b"\r"])
        self.assertIsNone(self.row._tg_typing)
        self.assertEqual(self.timers, [])

    def test_an_at_word_last_gets_a_space_in_a_grok_tab(self):
        # Grok opens its file picker on "@." and the Enter would pick a file
        for cli, text, typed in (("grok", "do not start a line with @.", "do not start a line with @. "),
                                 ("grok", "ask @beta", "ask @beta "),
                                 ("grok", "ask @beta\n", "ask @beta "),
                                 ("grok", "mail me@x.com", "mail me@x.com"),
                                 ("grok", "P@ssw0rd", "P@ssw0rd"),
                                 ("grok", "@beta: hi", "@beta: hi"),
                                 ("claude", "ask @beta", "ask @beta"),
                                 (None, "P@ss @w0rd", "P@ss @w0rd")):   # a shell's password prompt
            self.app._chat_cli_of = lambda row, cli=cli: cli
            self.row.reset_mock()
            self.row._tg_typing = None
            self.app._tg_inject(self.row, text)
            self.assertEqual(self.keys(), [typed], (cli, text))
            self.tick()

    def test_a_second_text_waits_for_the_first_enter(self):
        self.app._tg_inject(self.row, "one")
        self.app._tg_inject(self.row, "two")
        self.app._tg_inject(self.row, "three")
        self.assertEqual(self.keys(), ["one"])
        self.tick()
        self.tick()
        self.tick()
        self.assertEqual(self.keys(), ["one", b"\r", "two", b"\r", "three", b"\r"])
        self.assertIsNone(self.row._tg_typing)

    def test_a_failed_paste_does_not_stop_the_next(self):
        self.row.term.paste_text.side_effect = [RuntimeError("closing"), None]
        with self.assertRaises(RuntimeError):
            self.app._tg_inject(self.row, "one")
        self.app._tg_inject(self.row, "two")
        self.tick()
        self.assertEqual(self.keys(), ["one", "two", b"\r"])


class TestRemotePager(unittest.TestCase):
    """A pager in front shows only its own screen: the scrollback above it is
    out of order once it scrolls back."""

    def _tab_running(self, argv):
        import pty, signal
        pid, fd = pty.fork()
        if pid == 0:                                   # a session of its own, in front on its tty
            os.environ["TERM"] = "dumb"
            os.execvp(argv[0], argv)
        self.addCleanup(os.close, fd)
        self.addCleanup(lambda: (os.kill(pid, signal.SIGKILL), os.waitpid(pid, 0)))
        term = unittest.mock.Mock()
        term.get_pty.return_value.get_fd.return_value = fd
        term.get_row_count.return_value = 24
        time.sleep(0.5)
        return term

    def test_git_log_runs_less_in_front(self):
        # the job's leader is git (here sh); less is its child
        term = self._tab_running(["sh", "-c", "less /etc/hostname; sleep 5"])
        self.assertEqual(tabit.Tabit._term_pager_rows(term), 24)

    def test_anything_else_is_no_pager(self):
        term = self._tab_running(["sleep", "5"])
        self.assertEqual(tabit.Tabit._term_pager_rows(term), 0)
        term.get_pty.return_value = None               # a tab whose program has ended
        self.assertEqual(tabit.Tabit._term_pager_rows(term), 0)


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

    def test_a_group_named_chat_is_like_any_other(self):
        self.app._group_names["red"] = "chat"
        self.app._ungroup = lambda c: [setattr(r, "group_color", None)
                                       for r in self.rows if r.group_color == c]
        self.assertEqual(self.op("red", {"ungroup": True}), 200)
        self.assertEqual(self.colours(), [None, None, None])

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


class TestRemoteSetupLinks(unittest.TestCase):
    """A setup that waits for the user shows the page to open."""

    def test_a_link_keeps_its_query(self):
        m = tabit.Tabit._link_markup("https://login.tailscale.com/a/x?a=1&b=2", "open")
        self.assertEqual(m, '<a href="https://login.tailscale.com/a/x?a=1&amp;b=2">open</a>')

    def test_stop_ends_a_waiting_login(self):
        app = unittest.mock.Mock(_remote_srv=None)
        proc = app._remote_login_proc
        proc.poll.return_value = None
        tabit.Tabit._remote_stop(app, unpublish=False)
        proc.terminate.assert_called_once_with()

    def test_a_new_on_forgets_the_old_step(self):
        app = unittest.mock.Mock(_remote_pending=("log in to Tailscale", "https://x"))
        tabit.Tabit._remote_fresh(app)
        self.assertIsNone(app._remote_pending)



class TestChatTabOps(unittest.TestCase):
    """What Tabit Chat asks of the tabs: the groups and a new tab."""

    def setUp(self):
        Row = type("Row", (), {})
        self.rows = [Row()]
        self.rows[0].group_color = "red"
        self.calls = []
        test = self

        class App:
            _group_names = {"red": "Work"}
            _session_rows = lambda _s: test.rows
            _remote_call = lambda _s, fn, timeout=3: fn()

            def _remote_manage(self, op, target, body):
                test.calls.append((op, target, body))
                return 200, {"id": "n1"}

        self.app = App()

    def ops(self, op, target=None, body=None):
        return tabit.Tabit._chat_tab_ops(self.app, op, target, body)

    def test_groups_lists_the_used_ones(self):
        code, out = self.ops("groups")
        self.assertEqual(out["groups"], [{"color": "red", "name": "Work"}])

    def test_a_new_tab_with_no_group_stays_out_of_groups(self):
        self.assertEqual(self.ops("new", None, {"kind": "ai"})[0], 200)
        self.assertEqual(self.calls, [("new", None, {"kind": "ai"})])

    def test_a_new_tab_in_a_picked_group_is_left_alone(self):
        self.ops("new", None, {"kind": "ai", "group": "red"})
        self.assertEqual(self.calls, [("new", None, {"kind": "ai", "group": "red"})])

    def test_there_is_no_filing_any_more(self):
        self.assertEqual(self.ops("file", "t1")[0], 404)
        self.assertEqual(self.calls, [])

    def test_close_rename_and_account_go_to_the_phone_switch(self):
        self.ops("close", "t1")
        self.ops("rename", "t1", {"name": "x"})
        self.ops("account", "t1", {"account": "Personal"})
        self.assertEqual(self.calls, [("close", "t1", {}), ("rename", "t1", {"name": "x"}),
                                      ("account", "t1", {"account": "Personal"})])


class TestChatModelSwitch(unittest.TestCase):
    """A chat switches a tab's model the way it switches its login: tabit
    restarts the tab's claude with --model. Claude's settings are not touched."""

    def setUp(self):
        self.row = type("Row", (), {"argv": tabit.Tabit._ai_argv("claude", "/tmp", [])})()
        test = self
        self.asked = []

        class App:
            _remote_call = lambda _s, fn, timeout=3: fn()
            _tg_row_by_id = lambda _s, tid: test.row if tid == "t1" else None
            _ai_cli_path_of = staticmethod(tabit.Tabit._ai_cli_path_of)
            _claude_account_of = staticmethod(tabit.Tabit._claude_account_of)
            _ai_account_of = staticmethod(tabit.Tabit._ai_account_of)

            def _ai_switch_account(self, row, name, model=None, effort=None):
                test.asked.append((row, name, model, effort))
                return test.why
        App._chat_model = tabit.Tabit._chat_model
        self.app = App()
        self.why = None

    def ops(self, body):
        return tabit.Tabit._chat_tab_ops(self.app, "model", "t1", body)

    def test_the_tab_restarts_with_the_model_and_keeps_its_login(self):
        self.assertEqual(self.ops({"model": "haiku"}), (200, {"ok": True}))
        self.assertEqual(self.asked, [(self.row, "", "haiku", None)])

    def test_a_login_can_change_in_the_same_restart(self):
        self.ops({"model": "", "account": "Personal"})
        self.assertEqual(self.asked, [(self.row, "Personal", "", None)])

    def test_the_effort_alone_keeps_the_model(self):
        self.ops({"effort": "high"})
        self.assertEqual(self.asked, [(self.row, "", None, "high")])

    def test_a_tab_that_will_not_switch_says_why(self):
        self.why = "Claude is working: switch when it stops"
        self.assertEqual(self.ops({"model": "opus"}), (409, {"error": self.why}))
        self.assertEqual(tabit.Tabit._chat_tab_ops(self.app, "model", "zz", {"model": "opus"})[0], 404)


if __name__ == "__main__":
    unittest.main()


class TestTailscaleAccounts(unittest.TestCase):
    """Settings > Remote: the account Remote lets in, and the others known."""

    LIST = ("ID    Tailnet            Account\n"
            "65be  zhli826@gmail.com  zhli826@gmail.com*\n"
            "a1b2  work.example.com   chris@work.example.com\n")

    def test_current_and_others(self):
        self.assertEqual(tabit.Tabit._tailscale_accounts(self.LIST),
                         ("zhli826@gmail.com", ["chris@work.example.com"]))

    def test_logged_out(self):
        text = "ID    Tailnet            Account\n65be  zhli826@gmail.com  zhli826@gmail.com\n"
        self.assertEqual(tabit.Tabit._tailscale_accounts(text),
                         (None, ["zhli826@gmail.com"]))

    def test_nothing(self):
        self.assertEqual(tabit.Tabit._tailscale_accounts(""), (None, []))

    def test_switch_falls_back_to_sudo(self):
        app = unittest.mock.Mock()
        app._sudo.return_value = "ok"
        denied = unittest.mock.Mock(returncode=1, stdout="", stderr="Access denied: prefs write access denied")
        with unittest.mock.patch.object(tabit.subprocess, "run", return_value=denied):
            out = tabit.Tabit._tailscale_account_cmd(app, ["tailscale", "switch", "x"])
        self.assertEqual(out, "ok")
        app._sudo.assert_called_once_with(["tailscale", "switch", "x"], 30)

    def test_restart_only_when_remote_is_wanted(self):
        app = unittest.mock.Mock(_remote_want=False)
        tabit.Tabit._remote_restart(app)
        app._remote_start.assert_not_called()
        app = unittest.mock.Mock(_remote_want=True)
        tabit.Tabit._remote_restart(app)
        app._remote_stop.assert_called_once_with(unpublish=False)
        app._remote_start.assert_called_once()


class _RoomTerm:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.sizes = []

    def get_column_count(self):
        return self.cols

    def get_row_count(self):
        return self.rows

    def set_size(self, cols, rows):
        self.sizes.append((cols, rows))
        self.cols, self.rows = cols, rows


class _RoomPage:
    def __init__(self, parent=None):
        self.parent = parent

    def get_parent(self):
        return self.parent


class _RoomRow:
    def __init__(self, cols, rows, parent=None):
        self.term = _RoomTerm(cols, rows)
        self.page = _RoomPage(parent)


class _RoomApp:
    """The room methods only. No window."""

    def __init__(self, rows):
        self.rows = rows
        self._left_frame = object()
        self._right_frame = object()

    def _session_rows(self):
        return self.rows


for _name in ("_remote_drop_phone_room", "_remote_restore_one",
              "_remote_restore_phone_rooms", "_remote_arm_restore",
              "_remote_grow_agent"):
    setattr(_RoomApp, _name, getattr(tabit.Tabit, _name))
for _name in ("_remote_agent_room", "_remote_room_to_save"):
    setattr(_RoomApp, _name, staticmethod(getattr(tabit.Tabit, _name)))


class TestRemoteAgentRoom(unittest.TestCase):
    """The phone picks a size that fills its screen. A desktop pane stays."""

    def test_wanted_room_is_checked(self):
        self.assertEqual(tabit.Tabit._remote_wanted_room("64", "36"), (64, 36))
        self.assertIsNone(tabit.Tabit._remote_wanted_room(None, None))
        self.assertIsNone(tabit.Tabit._remote_wanted_room("10", "10"))
        self.assertIsNone(tabit.Tabit._remote_wanted_room("nope", "1"))

    def test_the_default_and_the_narrow_mistake_take_the_phone_size(self):
        want = (64, 36)
        self.assertEqual(
            tabit.Tabit._remote_agent_room(80, 24, False, want, False), want)
        self.assertEqual(
            tabit.Tabit._remote_agent_room(42, 40, False, want, False), want)

    def test_a_desktop_size_is_not_squeezed(self):
        want = (64, 36)
        self.assertIsNone(tabit.Tabit._remote_agent_room(80, 24, True, want, False))
        self.assertIsNone(tabit.Tabit._remote_agent_room(151, 44, False, want, False))
        self.assertIsNone(tabit.Tabit._remote_agent_room(80, 40, False, want, False))
        self.assertIsNone(tabit.Tabit._remote_agent_room(64, 36, False, want, True))
        self.assertEqual(
            tabit.Tabit._remote_agent_room(64, 36, False, (70, 30), True), (70, 30))

    def test_the_narrow_mistake_is_not_what_we_restore(self):
        self.assertEqual(tabit.Tabit._remote_room_to_save(42, 40), (80, 24))
        self.assertEqual(tabit.Tabit._remote_room_to_save(151, 44), (151, 44))

    def test_leaving_a_tab_puts_its_size_back(self):
        first = _RoomRow(80, 24)
        claude = _RoomRow(42, 40)
        app = _RoomApp([first, claude])
        app._remote_grow_agent(first, first.term, (60, 30))
        self.assertEqual(first.term.sizes, [(60, 30)])
        self.assertEqual(first._remote_saved_room, (80, 24))
        app._remote_grow_agent(claude, claude.term, (60, 30))
        self.assertEqual((first.term.cols, first.term.rows), (80, 24))
        self.assertIsNone(first._remote_saved_room)
        self.assertEqual(claude.term.sizes, [(60, 30)])
        self.assertEqual(claude._remote_saved_room, (80, 24))

    def test_the_desktop_pane_is_not_given_the_phone_size(self):
        frame = object()
        row = _RoomRow(80, 24, frame)
        row.term.get_allocation = lambda: types.SimpleNamespace(width=10, height=10)
        app = _RoomApp([row])
        app._left_frame = frame
        app._remote_grow_agent(row, row.term, (60, 30))
        self.assertEqual(row.term.sizes, [])
        self.assertIsNone(getattr(row, "_remote_phone_room", None))


class TestPlainTabHistory(unittest.TestCase):
    """Remote scrolls up a plain tab too: its scrollback above the screen."""

    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk, Vte
        cls.term = Vte.Terminal()
        cls.term.set_scrollback_lines(10000)
        cls.term.set_size(100, 30)
        cls.win = Gtk.Window()
        cls.win.add(cls.term)
        cls.win.show_all()
        cls.term.feed("".join(f"\x1b[31mline {i:05d}\x1b[0m text\r\n"
                              for i in range(1000)).encode())
        for _ in range(50):
            while Gtk.events_pending():
                Gtk.main_iteration()

    def screen(self, back, pager=None):
        row = types.SimpleNamespace(kind="term", term=self.term, title_text="t",
                                    agent_status="idle")
        T = tabit.Tabit
        app = types.SimpleNamespace(
            _remote_send_on=True, _tg_row_by_id=lambda r: row,
            _row_tmux_session=lambda r: None, _term_tail_ansi=T._term_tail_ansi,
            _term_tail_text=T._term_tail_text, _remote_cursor=T._remote_cursor,
            _term_history_text=T._term_history_text,
            _term_history_ends=T._term_history_ends,
            _term_pager_rows=pager or T._term_pager_rows)
        code, out, _ = T._remote_screen(app, "t", True, back)
        self.assertEqual(code, 200)
        nums = [int(x) for x in re.findall(r"line (\d+)", re.sub(r"\x1b\[[0-9;]*m", "", out["ansi"]))]
        return nums, out

    def test_more_lines_on_request(self):
        nums, out = self.screen(0)
        self.assertEqual(nums[-1], 999)
        self.assertFalse(out["nohist"])
        more, _ = self.screen(200)
        self.assertEqual(len(more), len(nums) + 200)
        self.assertEqual(more, list(range(more[0], 1000)))      # no gap, no repeat

    def test_top_of_the_scrollback(self):
        nums, out = self.screen(3000)
        self.assertEqual(nums[0], 0)
        self.assertTrue(out["nohist"])

    def test_a_pager_shows_only_its_screen(self):
        # what it scrolled away is out of order once it goes back
        nums, out = self.screen(200, pager=lambda term: 30)
        self.assertLessEqual(len(nums), 30)
        self.assertEqual(nums[-1], 999)
        self.assertTrue(out["nohist"])

    def test_the_screen_keeps_its_colours(self):
        _nums, out = self.screen(200)
        self.assertIn("38;2;", out["ansi"].split("\n")[-5])
