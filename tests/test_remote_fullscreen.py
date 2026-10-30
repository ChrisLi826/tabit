#!/usr/bin/env python3
"""A program on tmux's alternate screen has no history to scroll up to."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class TestFullscreenPane(unittest.TestCase):

    def setUp(self):
        # a tmux server of its own, never the one these tests may run in
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TMUX_TMPDIR"] = d.name
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], capture_output=True)

    def pane(self, cmd):
        subprocess.run(["tmux", "new-session", "-d", "-s", "t", "-x", "80", "-y", "24", cmd],
                       check=True)
        time.sleep(0.5)
        return tabit.Tabit._tmux_pane_ansi("t", 200)

    def test_alternate_screen_says_no_history(self):
        out = self.pane("seq 100; printf '\\033[?1049h'; echo full; sleep 30")
        self.assertTrue(out["nohist"])
        self.assertEqual(out["back"], 0)

    def test_inline_keeps_its_history(self):
        out = self.pane("seq 100; sleep 30")
        self.assertFalse(out["nohist"])
        self.assertEqual(out["back"], 200)
        self.assertIn("\n1\n", "\n" + out["ansi"])


if __name__ == "__main__":
    unittest.main()
