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

if __name__ == "__main__":
    unittest.main()
