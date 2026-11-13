#!/usr/bin/env python3
"""A new AI tab from a chat goes on with the Claude, Codex or Grok
conversation picked, and starts fresh else."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class TestNewTabContinue(unittest.TestCase):
    def launch(self, body, used=(), result=False):
        app = tabit.Tabit.__new__(tabit.Tabit)
        pair = mock.Mock(return_value=(["launch"], ["stored"], "icon"))
        with tempfile.TemporaryDirectory() as cwd, \
                mock.patch.object(app, "_load_ai_clis", return_value=[{"cli": "grok", "try": ["--continue"]}, {"cli": "codex", "try": ["resume --last"]},
                                                                     {"cli": "claude", "try": ["--continue"]}], create=True), \
                mock.patch.object(app, "_load_settings", return_value={}, create=True), \
                mock.patch.object(app, "_ai_takes_new_id", return_value=True, create=True), \
                mock.patch.object(app, "_ai_launch_pair", pair, create=True), \
                mock.patch.object(app, "_ai_sub", return_value="", create=True), \
                mock.patch.object(app, "_ai_session_ids_in_use", return_value=set(used), create=True), \
                mock.patch.object(app, "_ai_tab_session_ids", return_value=set(used), create=True), \
                mock.patch.object(app, "_ai_session_open_elsewhere", return_value=None, create=True), \
                mock.patch.object(app, "_add_session", return_value=None, create=True):
            out = app._remote_new(dict(body, kind="ai", cwd=cwd))
        return out if result else pair.call_args.kwargs

    def test_a_picked_codex_or_grok_conversation_goes_on(self):
        sid = "01a14318-e1a5-7552-8ddb-5427756b9cfd"
        for cli in ("grok", "codex"):
            got = self.launch({"cli": cli, "resume": sid})
            self.assertEqual((got["continue_now"], got["resume_id"], got["new_sid"]), (True, sid, None))

    def test_one_open_in_another_tab_is_refused(self):
        sid = "01a14318-e1a5-7552-8ddb-5427756b9cfd"
        code, _ = self.launch({"cli": "grok", "resume": sid}, used={sid}, result=True)
        self.assertEqual(code, 409)

    def test_a_claude_conversation_another_tab_holds_is_refused(self):
        sid = "01a14318-e1a5-7552-8ddb-5427756b9cfd"
        code, _ = self.launch({"cli": "claude", "resume": sid}, used={sid}, result=True)
        self.assertEqual(code, 409)

    def test_without_one_a_fresh_conversation_starts(self):
        got = self.launch({"cli": "claude"})
        self.assertIs(got["continue_now"], False)
        self.assertTrue(got["new_sid"])

    def test_a_resume_must_be_an_id(self):
        code, _ = self.launch({"cli": "grok", "resume": "--last"}, result=True)
        self.assertEqual(code, 400)

class TestSessionList(unittest.TestCase):
    def test_files_are_read_off_the_gtk_thread(self):
        # only the held ids come from the GTK thread; reading session files
        # there froze the window while the new chat form loaded its list
        app = tabit.Tabit.__new__(tabit.Tabit)
        on_gtk = []
        with mock.patch.object(app, "_remote_call", side_effect=lambda fn, timeout=3: on_gtk.append(fn) or {"held"}, create=True), \
                mock.patch.object(app, "_ai_session_ids_in_use", return_value={"held"}, create=True) as held, \
                mock.patch.object(app, "_ai_folder_sessions", return_value=[{"id": "x"}], create=True) as listed:
            code, out = app._chat_tab_ops("sessions", None, {"cli": "grok", "cwd": "/p"})
        self.assertEqual((code, out), (200, {"sessions": [{"id": "x"}]}))
        self.assertEqual(on_gtk, [held])
        listed.assert_called_once_with("grok", "/p", {"held"})


if __name__ == "__main__":
    unittest.main()
