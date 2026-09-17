#!/usr/bin/env python3
"""M2 helpers: tmux session → category / argv parse / chrome normalize."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

from tabit import (  # noqa: E402
    ICON_AI_TMUX, ICON_CONNECT, ICON_TMUX, Tabit,
)


class TestM2TmuxSidebarHelpers(unittest.TestCase):
    def test_category(self):
        self.assertEqual(Tabit._tmux_session_category("ai-claude-x"), "ai")
        self.assertEqual(Tabit._tmux_session_category("conn-ab12"), "connect")
        self.assertEqual(Tabit._tmux_session_category("dev"), "other")

    def test_session_from_argv(self):
        self.assertEqual(
            Tabit._tmux_session_from_argv(
                ["tmux", "new-session", "-A", "-s", "ai-claude-p"]),
            "ai-claude-p")
        self.assertEqual(
            Tabit._tmux_session_from_argv(
                ["tmux", "attach-session", "-t", "conn-sn1"]),
            "conn-sn1")
        self.assertEqual(
            Tabit._tmux_session_from_argv(
                ["/bin/sh", "-c",
                 "tmux has-session -t ai-x 2>/dev/null"
                 " || tmux new-session -d -s ai-x claude ;"
                 " exec tmux attach-session -t ai-x"]),
            "ai-x")

    def test_normalize_ai_connect(self):
        lab, argv, icon, sub = Tabit._normalize_tmux_hosted_tab(
            Tabit, "ai-claude-proj-deadbeef",
            ["tmux", "new-session", "-A", "-s", "ai-claude-proj-deadbeef"],
            ICON_TMUX, "tmux")
        self.assertEqual(icon, ICON_AI_TMUX)
        self.assertEqual(lab, "claude")

        lab, argv, icon, sub = Tabit._normalize_tmux_hosted_tab(
            Tabit, "conn-device1",
            ["tmux", "new-session", "-A", "-s", "conn-device1"],
            ICON_TMUX, "tmux")
        self.assertEqual(icon, ICON_CONNECT)
        self.assertIn("connect", lab.lower())

        lab, argv, icon, sub = Tabit._normalize_tmux_hosted_tab(
            Tabit, "scratch",
            ["tmux", "new-session", "-A", "-s", "scratch"],
            ICON_TMUX, "tmux")
        self.assertEqual(icon, ICON_TMUX)
        self.assertEqual(lab, "scratch")


if __name__ == "__main__":
    unittest.main()
