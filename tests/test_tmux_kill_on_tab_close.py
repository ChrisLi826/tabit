#!/usr/bin/env python3
"""Kill tmux on single-tab close/exited — never on full app quit."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

from tabit import Tabit  # noqa: E402


class _FakeRow:
    def __init__(self, argv=None):
        self.argv = argv or []


class TestRowTmuxSession(unittest.TestCase):
    def test_plain_tmux_argv(self):
        row = _FakeRow(["tmux", "new-session", "-A", "-s", "conn-abc"])
        self.assertEqual(Tabit._row_tmux_session(Tabit, row), "conn-abc")

    def test_ai_wrapped_argv(self):
        argv = Tabit._ai_tmux_argv(
            ["/bin/sh", "-c", "cd /tmp; exec claude"], "ai-claude-tmp-abc123")
        row = _FakeRow(argv)
        self.assertEqual(
            Tabit._row_tmux_session(Tabit, row), "ai-claude-tmp-abc123")

    def test_non_tmux_shell(self):
        row = _FakeRow(["/bin/bash"])
        self.assertIsNone(Tabit._row_tmux_session(Tabit, row))


class TestKillRowTmuxSession(unittest.TestCase):
    def setUp(self):
        # Minimal stand-in: only the kill helper + shutdown flag.
        self.win = Tabit.__new__(Tabit)
        self.win._shutting_down = False

    def test_kills_named_session(self):
        row = _FakeRow(["tmux", "attach-session", "-t", "dev"])
        with mock.patch("tabit.subprocess.run") as run:
            Tabit._kill_row_tmux_session(self.win, row)
            run.assert_called_once()
            args = run.call_args[0][0]
            self.assertEqual(args, ["tmux", "kill-session", "-t", "dev"])
        self.assertTrue(row._tmux_session_killed)

    def test_idempotent_second_call(self):
        row = _FakeRow(["tmux", "attach-session", "-t", "dev"])
        with mock.patch("tabit.subprocess.run") as run:
            Tabit._kill_row_tmux_session(self.win, row)
            Tabit._kill_row_tmux_session(self.win, row)
            self.assertEqual(run.call_count, 1)

    def test_skips_during_app_shutdown(self):
        """Full app quit must leave sessions Attach-able after restart."""
        self.win._shutting_down = True
        row = _FakeRow(["tmux", "attach-session", "-t", "ai-keep-me"])
        with mock.patch("tabit.subprocess.run") as run:
            Tabit._kill_row_tmux_session(self.win, row)
            run.assert_not_called()
        self.assertFalse(getattr(row, "_tmux_session_killed", False))

    def test_skips_non_tmux_row(self):
        row = _FakeRow(["bash", "-l"])
        with mock.patch("tabit.subprocess.run") as run:
            Tabit._kill_row_tmux_session(self.win, row)
            run.assert_not_called()


class TestShutdownFlagContract(unittest.TestCase):
    """Document the close vs quit distinction (helpers only — no GTK window)."""

    def test_delete_event_sets_shutting_down_before_teardown(self):
        # Source-level contract: _on_delete_event assigns the flag so
        # subsequent child-exited from VTE destroy will not kill-session.
        with open(os.path.join(ROOT, "tabit.py")) as f:
            src = f.read()
        # Flag set in delete-event path
        self.assertIn("self._shutting_down = True", src)
        # kill only via the shared helper (close + exited)
        self.assertIn("self._kill_row_tmux_session(row)", src)
        # delete-event must not invoke the kill helper (comments may mention it)
        start = src.index("def _on_delete_event")
        end = src.index("\n    def ", start + 1)
        block = src[start:end]
        self.assertNotIn("_kill_row_tmux_session(", block)
        self.assertNotIn('["tmux", "kill-session"', block)
        self.assertIn("_shutting_down = True", block)


if __name__ == "__main__":
    unittest.main()
