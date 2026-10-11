#!/usr/bin/env python3
"""Telegram answers and tasks: nothing hangs, leaks or presses Enter alone."""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class TestReplyFile(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, "t.md")
        self.secret = os.path.join(d.name, "telegram.json")
        with open(self.secret, "w") as f:
            f.write('{"bot_token": "123:abc"}')
        path = self.path
        self.app = type("App", (), {
            "_tg_jobs": lambda _s: {"t": {"row_id": "r", "name": "x", "msg_id": 1}},
            "_tg_reply_path": lambda _s, _t: path,
            "_tg_chunks": tabit.Tabit._tg_chunks,
            "_TG_REPLY_PARTS": tabit.Tabit._TG_REPLY_PARTS,
            "_TG_MSG_LIMIT": tabit.Tabit._TG_MSG_LIMIT,
        })()

    def test_a_fifo_does_not_hang(self):
        os.mkfifo(self.path)
        self.assertFalse(tabit.Tabit._tg_deliver_reply(self.app, "t"))

    def test_a_hard_link_is_not_sent(self):
        os.link(self.secret, self.path)
        self.assertFalse(tabit.Tabit._tg_deliver_reply(self.app, "t"))


class TestControlOnlyTask(unittest.TestCase):

    def test_nothing_to_type(self):
        app = type("App", (), {"_tg_row_trouble": lambda _s, _r: None,
                               "_tg_safe_text": tabit.Tabit._tg_safe_text})()
        self.assertEqual(tabit.Tabit._tg_send_task(app, object(), "\x1b\x1b", is_answer=True),
                         "nothing to type in that message")



class TestSecretsFile(unittest.TestCase):

    def test_written_whole_and_private(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, val in (("CONFIG_DIR", d.name), ("TELEGRAM_FILE", os.path.join(d.name, "telegram.json"))):
            old = getattr(tabit, name)
            setattr(tabit, name, val)
            self.addCleanup(setattr, tabit, name, old)
        tabit.Tabit._tg_save_secrets(" 123:abc ", "42")
        self.assertEqual(os.listdir(d.name), ["telegram.json"])
        self.assertEqual(os.stat(tabit.TELEGRAM_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(tabit.Tabit._tg_load_secrets(), {"bot_token": "123:abc", "chat_id": "42"})

class TestBotOrChatSwitch(unittest.TestCase):
    """After Settings switches the bot or chat, nothing from the old one acts."""

    def setUp(self):
        from unittest import mock
        self.sec = {"bot_token": "111:AAA", "chat_id": "500"}
        self.state = {}
        self.idle = []
        conf = dict(tabit.DEFAULT_SETTINGS, telegram_enabled=True, telegram_reply=True)
        T = tabit.Tabit
        for name, val in (
                ("_tg_load_secrets", staticmethod(lambda: dict(self.sec))),
                ("_tg_load_state", staticmethod(lambda: dict(self.state))),
                ("_tg_save_state", classmethod(lambda _c, **kw: self.state.update(kw))),
                ("_load_settings", staticmethod(lambda: dict(conf))),
                ("_tg_poll_gap", classmethod(lambda _c, _s: 0))):
            p = mock.patch.object(T, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tabit.GLib, "idle_add", lambda f, *a: self.idle.append((f, a)))
        p.start()
        self.addCleanup(p.stop)
        self.app = T.__new__(T)

    def poll(self, answer):
        calls = []
        def call(_c, token, method, params, timeout=None):
            calls.append((token, params["offset"]))
            return answer(token), None
        from unittest import mock
        with mock.patch.object(tabit.Tabit, "_tg_call", classmethod(call)):
            self.app._tg_poll_once(5)
        return calls

    def test_an_old_bot_offset_and_updates_are_dropped(self):
        def answer(token):
            self.sec["bot_token"] = "222:BBB"       # saved while polling
            return [{"update_id": 900000, "message": {
                "chat": {"id": 500, "type": "private"}, "from": {"id": 500}, "text": "hi"}}]
        self.poll(answer)
        self.assertEqual(self.idle, [])
        self.assertNotIn("offset", self.state)
        self.state.update(offset=900001, offset_bot=tabit.Tabit._tg_bot_key("111:AAA"))
        calls = self.poll(lambda _t: [])
        self.assertEqual(calls, [("222:BBB", 0)])

    def test_a_handler_for_the_old_chat_does_nothing(self):
        old = ("500", tabit.Tabit._tg_bot_key("111:AAA"))
        self.sec["chat_id"] = "777"
        self.app._tg_drafts = {"t": {"text": "deploy", "rows": ["r"]}}
        self.app._tg_send_task = lambda *a, **k: self.fail("typed")
        self.app._tg_answer = self.app._tg_send = lambda *a, **k: self.fail("answered")
        tabit.Tabit._tg_on_callback(self.app, "c", "s:t:r", old)
        tabit.Tabit._tg_on_message(self.app, {"text": "go"}, old)

    def test_a_message_id_belongs_to_its_chat(self):
        old = ("500", tabit.Tabit._tg_bot_key("111:AAA"))
        self.app._tg_remember_msg(150, "r", old)
        reply = {"reply_to_message": {"message_id": 150}}
        self.assertEqual(self.app._tg_replied_row_id(reply), "r")
        self.sec["chat_id"] = "777"
        self.assertIsNone(self.app._tg_replied_row_id(reply))

    def test_an_old_reply_gets_buttons_not_typed(self):
        import time
        dest = ("500", tabit.Tabit._tg_bot_key("111:AAA"))
        self.app._tg_remember_msg(150, "r", dest)
        self.app._tg_send_task = lambda *a, **k: self.fail("typed straight in")
        sent = []
        self.app._tg_targets = lambda: [("r", "x")]
        self.app._tg_hold = lambda text, rows: "tok"
        self.app._tg_confirm_text = lambda text, targets: text
        self.app._tg_keyboard = lambda tok, targets: None
        self.app._tg_send = lambda text, markup=None: sent.append(text)
        tabit.Tabit._tg_on_message(self.app, {
            "text": "y", "date": int(time.time()) - 3600,
            "reply_to_message": {"message_id": 150}}, dest)
        self.assertEqual(sent, ["y"])


if __name__ == "__main__":
    unittest.main()
