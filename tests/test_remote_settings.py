#!/usr/bin/env python3
"""The phone's Settings sheet: AI defaults, Claude accounts, update, restart."""
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402

T = tabit.Tabit


def app(**kw):
    a = mock.Mock(_remote_send_on=True, _remote_update=None, _remote_login=None,
                  _REMOTE_SETTINGS=T._REMOTE_SETTINGS)
    a._remote_call = lambda fn, timeout=3: fn()
    for name in ("_remote_admin_restart", "_remote_admin_update",
                 "_remote_admin_login_code", "_remote_unsaved_notes"):
        setattr(a, name, getattr(T, name).__get__(a))
    a._session_rows.return_value = []
    a._claude_accounts.return_value = []
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class TestGate(unittest.TestCase):

    def test_nothing_without_typing_from_the_phone(self):
        a = app(_remote_send_on=False)
        for op in ("state", "set", "account-add", "update", "restart"):
            self.assertEqual(T._remote_admin(a, op, {})[0], 403, op)
        a._save_settings.assert_not_called()

    def test_unknown_op(self):
        self.assertEqual(T._remote_admin(app(), "rm-rf", {})[0], 404)


class TestSettings(unittest.TestCase):

    def test_only_the_ai_defaults_and_only_true_or_false(self):
        a = app()
        self.assertEqual(T._remote_admin(a, "set", {"ai_use_tmux": False})[0], 200)
        a._save_settings.assert_called_once_with({"ai_use_tmux": False})
        for bad in ({"remote_send": False}, {"ai_use_tmux": "no"}, {}):
            a = app()
            self.assertEqual(T._remote_admin(a, "set", bad)[0], 400, bad)
            a._save_settings.assert_not_called()


class TestRestart(unittest.TestCase):

    def test_an_unsaved_note_stops_it(self):
        note = types.SimpleNamespace(kind="note", buffer=mock.Mock(),
                                     file_path="/tmp/todo.md")
        note.buffer.get_modified.return_value = True
        a = app()
        a._session_rows.return_value = [note]
        with mock.patch.object(tabit.GLib, "timeout_add") as later:
            code, body = T._remote_admin(a, "restart", {})
        self.assertEqual(code, 409)
        self.assertIn("todo.md", body["error"])
        later.assert_not_called()

    def test_it_answers_first_then_restarts(self):
        with mock.patch.object(tabit.GLib, "timeout_add") as later:
            code, _ = T._remote_admin(app(), "restart", {})
        self.assertEqual(code, 200)
        self.assertEqual(later.call_args[0][0], 500)


class TestUpdate(unittest.TestCase):

    def test_one_at_a_time(self):
        a = app(_remote_update={"state": "running"})
        self.assertEqual(T._remote_admin(a, "update", {})[0], 409)

    def test_a_password_stops_it(self):
        a = app()
        a._remote_latest.return_value = ("v9.9.9", "v9.9.9 · X", True)
        a._sudo_ok.return_value = False
        code, body = T._remote_admin(a, "update", {})
        self.assertEqual(code, 409)
        self.assertIn("password", body["error"])
        a._update_install.assert_not_called()

    def test_nothing_newer_nothing_to_do(self):
        a = app()
        a._remote_latest.return_value = ("v2.0.6.5", "v2.0.6.5 · X", False)
        self.assertEqual(T._remote_admin(a, "update", {})[0], 409)


class TestAccounts(unittest.TestCase):

    def test_a_bad_name_is_refused(self):
        a = app()
        a._claude_account_add.side_effect = ValueError("use 1-24 letters")
        self.assertEqual(T._remote_admin(a, "account-add", {"name": "a b"})[0], 400)
        a._remote_login_start.assert_not_called()

    def test_adding_starts_its_login(self):
        a = app()
        a._claude_account_add.return_value = "/x/work/bin/claude"
        self.assertEqual(T._remote_admin(a, "account-add", {"name": "work"})[0], 200)
        a._remote_login_start.assert_called_once_with("work", "/x/work/bin/claude")

    def test_log_in_an_existing_one(self):
        a = app()
        a._claude_accounts.return_value = [("work", "/x/work/bin/claude")]
        self.assertEqual(T._remote_admin(a, "login-start", {"name": "work"})[0], 200)
        a._remote_login_start.assert_called_once_with("work", "/x/work/bin/claude")
        a = app()
        self.assertEqual(T._remote_admin(a, "login-start", {"name": ""})[0], 200)
        a._remote_login_start.assert_called_once_with("", "claude")
        self.assertEqual(T._remote_admin(app(), "login-start", {"name": "gone"})[0], 400)


class TestLoginJob(unittest.TestCase):
    """A stand-in `claude auth login` under a real pty."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.cli = os.path.join(d.name, "claude")
        with open(self.cli, "w") as f:
            f.write("#!/bin/sh\n"
                    "printf 'visit: https://x.example/oauth?a=1&b=%s\\n' "
                    "$(printf 'z%.0s' $(seq 1 200))\n"
                    "printf 'Paste code here if prompted > '\n"
                    "while read c; do\n"
                    "  [ \"$c\" = good ] && { echo 'Login successful.'; exit 0; }\n"
                    "  echo 'Invalid code. Please make sure the full code was copied.'\n"
                    "done\n")
        os.chmod(self.cli, 0o755)

    def wait(self, a, want, secs=5):
        end = time.time() + secs
        while time.time() < end:
            if a._remote_login.get("state") == want:
                return a._remote_login
            time.sleep(0.05)
        self.fail(f"state stayed {a._remote_login.get('state')}: {a._remote_login}")

    def test_link_then_a_bad_code_then_a_good_one(self):
        a = types.SimpleNamespace(_remote_login=None)
        T._remote_login_start(a, "work", self.cli)
        job = self.wait(a, "waiting")
        self.assertTrue(job["url"].startswith("https://x.example/oauth?a=1&b=zzz"))
        self.assertEqual(len(job["url"]), len("https://x.example/oauth?a=1&b=") + 200)
        self.assertEqual(T._remote_admin_login_code(a, "bad")[0], 200)
        end = time.time() + 5
        while "Invalid" not in a._remote_login.get("msg", "") and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(a._remote_login["state"], "waiting")
        self.assertIn("Invalid code", a._remote_login["msg"])
        T._remote_admin_login_code(a, "good")
        self.wait(a, "done")

    def test_a_code_needs_a_login_waiting(self):
        a = types.SimpleNamespace(_remote_login=None)
        self.assertEqual(T._remote_admin_login_code(a, "x")[0], 409)


if __name__ == "__main__":
    unittest.main()
