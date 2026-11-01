#!/usr/bin/env python3
"""Tabit Chat over the real remote server: who gets in, and one chat end to end
on a real unix socket, with the fake claude behind it."""
import json
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402
import tabit_chat  # noqa: E402
from tests.test_remote import _ServerCase, http_get, http_post  # noqa: E402

OK = {"Tailscale-User-Login": "me@example.com", "X-Tabit": "1",
      "Content-Type": "application/json", "Origin": "https://me.ts.net"}


class ChatApp:
    _remote_url = "https://me.ts.net/"
    _remote_files_on = False
    _remote_send_on = True

    def __init__(self, mgr):
        self.mgr = mgr

    def _chat_mgr(self):
        return self.mgr

    def _remote_page(self, name="remote.html"):
        with open(os.path.join(ROOT, name), "rb") as f:
            return f.read()


class TestChatRoutes(_ServerCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        os.environ["FAKE_CLAUDE_LOG"] = os.path.join(self.work.name, "argv.log")
        super().setUp()
        self.app = ChatApp(tabit_chat.ChatManager(
            root=os.path.join(self.work.name, "chats"),
            claude=os.path.join(HERE, "fake_claude.py")))
        # _ServerCase built the handler with its own FakeApp: point it at ours
        self.srv.RequestHandlerClass.app = self.app

    def tearDown(self):
        for c in self.app.mgr.chats.values():
            c.stop_process()
        super().tearDown()
        self.work.cleanup()

    def post(self, path, body=None, headers=OK):
        code, raw = http_post(path, self.sock, json.dumps(body or {}), headers)
        return code, json.loads(raw)

    def get(self, path):
        head, body = http_get(path, self.sock)
        return int(head.split()[1]), json.loads(body)

    def sse(self, path, kinds, n):
        """The chat pushes seen so far, once `n` of them have arrived."""
        head, body = http_get(path, self.sock, read_until=(b"event: chat", n))
        out = []
        for block in body.decode().split("\n\n"):
            if "event: chat" in block:
                out.append(json.loads(block.split("data: ", 1)[1]))
        return out

    def wait_for(self, fn, secs=8):
        end = time.time() + secs
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.03)
        return False

    def test_the_page_and_its_manifest(self):
        head, body = http_get("/chat", self.sock)
        self.assertIn("200", head.splitlines()[0])
        self.assertIn(b"tabit chat", body)
        head, body = http_get("/chat.webmanifest", self.sock)
        self.assertEqual(json.loads(body)["start_url"], "chat")

    def test_it_is_off_until_the_phone_may_type(self):
        self.app._remote_send_on = False
        self.assertEqual(self.get("/api/chats")[0], 403)
        self.assertEqual(self.post("/api/chats", {"cwd": "/tmp"})[0], 403)
        self.assertEqual(self.get("/api/chat-groups")[0], 403)
        self.assertEqual(self.app.mgr.chats, {})

    def test_only_your_login_and_only_from_the_page(self):
        head, _ = http_get("/api/chats", self.sock, login="eve@example.com")
        self.assertIn("403", head.splitlines()[0])
        head, _ = http_get("/api/chats", self.sock, login=None)
        self.assertIn("403", head.splitlines()[0])
        for change in ({"X-Tabit": None}, {"Origin": "https://evil.example"},
                       {"Content-Type": "text/plain"}):
            h = {k: v for k, v in dict(OK, **change).items() if v is not None}
            self.assertEqual(self.post("/api/chats", {"cwd": "/tmp"}, h)[0], 403, change)
        self.assertEqual(self.app.mgr.chats, {})

    def test_one_chat_end_to_end(self):
        code, out = self.post("/api/chats", {"cwd": self.work.name, "title": "T"})
        self.assertEqual(code, 200, out)
        cid = out["chat"]["id"]
        self.assertEqual(self.get("/api/chats")[1]["chats"][0]["id"], cid)
        self.assertEqual(self.post(f"/api/chats/{cid}/send", {"text": "write q.txt"})[0], 200)
        chat = self.app.mgr.chats[cid]
        self.assertTrue(self.wait_for(lambda: chat.pending))
        # the page reads the question from the stream
        push = self.sse(f"/api/chats/{cid}/events?tail=50", None, 1)[-1]
        self.assertEqual(push["state"], "waiting")
        ask = push["pending"][0]
        self.assertEqual(ask["tool"], "Write")
        self.assertEqual([e["t"] for e in push["events"]][:2], ["user", "tool"])
        # and answers it
        self.assertEqual(self.post(f"/api/chats/{cid}/answer",
                                   {"rid": ask["rid"], "allow": True})[0], 200)
        self.assertTrue(self.wait_for(lambda: chat.state == "idle" and chat.events[-1]["t"] == "done"))
        # a page that dropped off comes back at its last seq and gets only the rest
        last = push["seq"]
        again = self.sse(f"/api/chats/{cid}/events?since={last}", None, 1)[-1]
        self.assertEqual([e["t"] for e in again["events"]], ["answered", "text", "done"])
        self.assertEqual(again["pending"], [])
        # older events, for "load earlier"
        code, hist = self.get(f"/api/chats/{cid}/history?before={last}")
        self.assertEqual(code, 200)
        self.assertEqual([e["t"] for e in hist["events"]], ["user", "tool"])
        # Last-Event-ID does the same as since=
        self.assertEqual(self.get(f"/api/chats/{cid}")[1]["state"], "idle")

    def test_a_group_over_http(self):
        ids = [self.post("/api/chats", {"cwd": self.work.name, "title": t})[1]["chat"]["id"]
               for t in ("Ann", "Ben")]
        code, out = self.post("/api/chat-groups", {"title": "G", "members": ids})
        self.assertEqual(code, 200, out)
        gid = out["group"]["id"]
        self.assertEqual(self.post(f"/api/chat-groups/{gid}/send", {"text": "say hello"})[0], 200)
        group = self.app.mgr.groups[gid]
        self.assertTrue(self.wait_for(lambda: len([e for e in group.events if e["t"] == "say"]) == 2))
        push = self.sse(f"/api/chat-groups/{gid}/events", None, 1)[-1]
        self.assertEqual(sorted(e["name"] for e in push["events"] if e["t"] == "say"), ["ann", "ben"])
        self.assertEqual(self.get("/api/chat-groups")[1]["groups"][0]["id"], gid)
        self.assertEqual(self.post(f"/api/chat-groups/{gid}/delete")[0], 200)
        self.assertEqual(self.get(f"/api/chat-groups/{gid}")[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.post("/api/chats", {"cwd": "/no/such"})[0], 400)
        self.assertEqual(self.get("/api/chats/zzz")[0], 404)
        self.assertEqual(self.post("/api/chats/zzz/send", {"text": "x"})[0], 404)
        self.assertEqual(self.get("/api/chats/zzz/events")[0], 404)


if __name__ == "__main__":
    unittest.main()
