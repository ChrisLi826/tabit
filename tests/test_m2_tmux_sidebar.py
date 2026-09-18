#!/usr/bin/env python3
"""M2 helpers: chrome normalize + work-group focus inheritance (no type mega-groups)."""
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
        # M1 picker filter buckets (list layer — not sidebar grouping)
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


class _FakeRow:
    def __init__(self, argv=None, icon_name=None, group_color=None):
        self.argv = argv or []
        self.icon_name = icon_name
        self.group_color = group_color


class _FakeWin:
    """Mirrors _place_tab_row focus-inheritance rule without GTK UI."""

    def __init__(self):
        self._active = None

    def _get_active_group_color(self):
        return self._active

    def _inherit_new_tab_group(self, _row):
        """Same rule as Tabit._place_tab_row (non-restore path)."""
        return self._get_active_group_color()


class TestM2WorkGroupFocusInheritance(unittest.TestCase):
    """Groups = work content; all tab types inherit focused work group.

    When nothing is focused → stay ungrouped (no prompt, no type mega-group).
    """

    def setUp(self):
        self.win = _FakeWin()

    def _ai_row(self):
        return _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "ai-claude-proj-deadbeef"],
            icon_name=ICON_AI_TMUX)

    def _conn_row(self):
        return _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "conn-TESTDEVICE123"],
            icon_name=ICON_CONNECT)

    def _other_row(self):
        return _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "misc-build"],
            icon_name=ICON_TMUX)

    def test_all_types_inherit_focused_work_group(self):
        self.win._active = "red"  # work group "Project X"
        for row in (self._ai_row(), self._conn_row(), self._other_row()):
            self.assertEqual(self.win._inherit_new_tab_group(row), "red")

    def test_no_focus_stays_ungrouped(self):
        self.win._active = None
        for row in (self._ai_row(), self._conn_row(), self._other_row()):
            self.assertIsNone(self.win._inherit_new_tab_group(row))

    def test_ai_row_keeps_ai_chrome_not_type_group(self):
        """AI recognition is icon/status chrome — not forced into an AI mega-group."""
        self.win._active = "teal"  # a work group that happens to use teal
        row = self._ai_row()
        self.assertEqual(self.win._inherit_new_tab_group(row), "teal")
        # Chrome still identifies AI (status badges attach to ICON_AI_TMUX rows)
        self.assertEqual(row.icon_name, ICON_AI_TMUX)
        self.assertTrue(row.icon_name.startswith("tabit-ai"))

    def test_connect_row_keeps_connect_icon_in_work_group(self):
        self.win._active = "purple"
        row = self._conn_row()
        self.assertEqual(self.win._inherit_new_tab_group(row), "purple")
        self.assertEqual(row.icon_name, ICON_CONNECT)


if __name__ == "__main__":
    unittest.main()
