#!/usr/bin/env python3
"""Remote on 127.0.0.1 for a browser on this computer: it works without
Tailscale, and only a viewer with the key from Settings gets in."""
import json
import os
import socket
import tempfile
import threading
import types
import unittest
import unittest.mock

from test_remote import FakeApp, tabit

KEY = "k" * 43


def req(port, method, path, headers=None, body=b""):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    extra = "".join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
    s.sendall(f"{method} {path} HTTP/1.1\r\n{extra}Content-Length: {len(body)}\r\n\r\n".encode() + body)
    out = b""
    while chunk := s.recv(65536):
        out += chunk
    s.close()
    head, _, rest = out.partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode(), rest


class TestLocalDoor(unittest.TestCase):
    def setUp(self):
        self.app = FakeApp()
        handler = type("L", (tabit._RemoteHandler,),
                       {"app": self.app, "stop": threading.Event(), "owner": None,
                        "local_key": KEY, "_streams": [], "_chat_streams": [],
                        "_streams_lock": threading.Lock()})
        self.srv = tabit._LocalServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cookie = {"Cookie": f"{tabit.LOCAL_COOKIE}={KEY}"}

    def tearDown(self):
        self.srv.RequestHandlerClass.stop.set()
        self.srv.shutdown()
        self.srv.server_close()

    def test_the_key_becomes_a_cookie_and_leaves_the_address(self):
        code, head, _ = req(self.port, "GET", f"/?key={KEY}&x=1")
        self.assertEqual(code, 303)
        self.assertIn("Location: /?x=1\r\n", head)
        self.assertIn(f"Set-Cookie: {tabit.LOCAL_COOKIE}={KEY}; Path=/;", head)
        self.assertIn("HttpOnly; SameSite=Strict", head)
        code, _, body = req(self.port, "GET", "/", self.cookie)
        self.assertEqual((code, body), (200, b"<html>chat</html>"))
        code, _, body = req(self.port, "GET", "/api/status", self.cookie)
        self.assertEqual(json.loads(body)["tabs"][0]["ai_status"], "idle")

    def test_no_key_no_entry(self):
        for name, path, headers in (
                ("nothing", "/", {}),
                ("a wrong key", "/?key=nope", {}),
                ("a wrong cookie", "/", {"Cookie": f"{tabit.LOCAL_COOKIE}=nope"}),
                ("a Tailscale login means nothing here", "/", {"Tailscale-User-Login": "me@example.com"}),
                ("another site's name pointing here", "/", {**self.cookie, "Host": f"evil.example:{self.port}"}),
                ("another port's name", "/", {**self.cookie, "Host": "127.0.0.1:1"})):
            self.assertEqual(req(self.port, "GET", path, headers)[0], 403, name)

    def test_the_app_manifest_and_icons_need_no_key(self):
        # the browser fetches the manifest without cookies
        for path in ("/chat.webmanifest", "/manifest.webmanifest"):
            self.assertEqual(req(self.port, "GET", path)[0], 200, path)
        self.assertEqual(req(self.port, "GET", "/chat.webmanifest", {"Host": "evil.example"})[0], 403)
        self.assertEqual(req(self.port, "GET", "/settings.js")[0], 403)

    def test_post_needs_the_cookie_and_this_origin(self):
        ok = {**self.cookie, "X-Tabit": "1", "Content-Type": "application/json",
              "Origin": f"http://127.0.0.1:{self.port}"}
        body = json.dumps({"text": "hi"}).encode()
        self.assertEqual(req(self.port, "POST", "/api/tabs/a1/send", ok, body)[0], 200)
        self.assertEqual(self.app.acts, [("a1", "send", {"text": "hi"})])
        for name, change in (("another site", {"Origin": "https://evil.example"}),
                             ("the Tailscale page", {"Origin": "https://me.ts.net"}),
                             ("no cookie", {"Cookie": None}),
                             ("the key in the address", {"Cookie": None, "X-Tabit": "1"})):
            path = "/api/tabs/a1/send" + (f"?key={KEY}" if "address" in name else "")
            self.assertEqual(req(self.port, "POST", path, {**ok, **change}, body)[0], 403, name)
        self.assertEqual(len(self.app.acts), 1)
        code, _, _ = req(self.port, "POST", "/api/tabs/a1/send",
                         {**ok, "Host": f"localhost:{self.port}", "Origin": f"http://localhost:{self.port}"}, body)
        self.assertEqual(code, 200)


class TestLocalStartStop(unittest.TestCase):
    def test_key_is_kept_and_private(self):
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(tabit, "CONFIG_DIR", d):
            a = tabit.Tabit._local_key()
            self.assertGreaterEqual(len(a), 32)
            self.assertEqual(tabit.Tabit._local_key(), a)
            self.assertEqual(os.stat(os.path.join(d, "remote-local-key")).st_mode & 0o777, 0o600)

    def test_runs_without_tailscale_and_takes_another_port_when_busy(self):
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        self.addCleanup(busy.close)
        app = types.SimpleNamespace(_local_srv=None, _local_url="", _remote_srv=None,
                                    _local_key=tabit.Tabit._local_key)
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(tabit, "CONFIG_DIR", d), \
                unittest.mock.patch.object(tabit, "REMOTE_LOCAL_PORT", busy.getsockname()[1]):
            tabit.Tabit._local_start(app)
            self.addCleanup(tabit.Tabit._local_stop, app)
            port = app._local_srv.server_address[1]
            self.assertNotEqual(port, busy.getsockname()[1])
            self.assertTrue(app._local_url.startswith(f"http://127.0.0.1:{port}/?key="))
            self.assertEqual(app._local_srv.server_address[0], "127.0.0.1")
            tabit.Tabit._local_start(app)               # once only
            self.assertEqual(app._local_srv.server_address[1], port)
            tabit.Tabit._local_stop(app)
            self.assertIsNone(app._local_srv)
            self.assertEqual(app._local_url, "")
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", port), timeout=1)


if __name__ == "__main__":
    unittest.main()
