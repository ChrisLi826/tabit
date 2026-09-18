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


class _FakeRow:
    def __init__(self, argv=None, icon_name=None, group_color=None):
        self.argv = argv or []
        self.icon_name = icon_name
        self.group_color = group_color


class _FakeWin:
    """Minimal stand-in to exercise M2 placement resolution without GTK UI."""

    _CATEGORY_GROUP_PREF = Tabit._CATEGORY_GROUP_PREF
    _tmux_session_category = staticmethod(Tabit._tmux_session_category)
    _tmux_session_from_argv = staticmethod(Tabit._tmux_session_from_argv)
    _ai_tmux_unwrap = classmethod(Tabit._ai_tmux_unwrap.__func__)

    def __init__(self):
        self._group_names = {}
        self._active = None
        self._rows = []

    def _session_rows(self):
        return list(self._rows)

    def _get_active_group_color(self):
        return self._active

    def _new_group_color(self):
        return "orange"

    def _save_group_names(self):
        pass

    _is_category_group_color = Tabit._is_category_group_color
    _category_group_color = Tabit._category_group_color
    _row_auto_group_category = Tabit._row_auto_group_category
    _resolve_new_tab_group = Tabit._resolve_new_tab_group


class TestM2NewTabGroupResolve(unittest.TestCase):
    def setUp(self):
        self.win = _FakeWin()
        # Prefill named system groups as restore/auto-group would.
        self.win._group_names["purple"] = "AI"
        self.win._group_names["teal"] = "Connect"

    def test_ai_always_ai_even_if_connect_focused(self):
        self.win._active = "teal"
        row = _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "ai-claude-proj-deadbeef"],
            icon_name=ICON_AI_TMUX)
        self.assertEqual(self.win._resolve_new_tab_group(row), "purple")

    def test_connect_not_stolen_by_focused_ai(self):
        self.win._active = "purple"  # focused AI group (QA failure)
        row = _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "conn-TESTDEVICE123"],
            icon_name=ICON_CONNECT)
        self.assertEqual(self.win._resolve_new_tab_group(row), "teal")

    def test_other_not_sucked_into_focused_ai(self):
        self.win._active = "purple"
        row = _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "misc-build"],
            icon_name=ICON_TMUX)
        self.assertIsNone(self.win._resolve_new_tab_group(row))

    def test_other_inherits_manual_focused_group(self):
        self.win._group_names["red"] = "Work"
        self.win._active = "red"
        row = _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "misc-build"],
            icon_name=ICON_TMUX)
        self.assertEqual(self.win._resolve_new_tab_group(row), "red")

    def test_ai_joins_ai_when_nothing_focused(self):
        self.win._active = None
        row = _FakeRow(
            argv=["tmux", "new-session", "-A", "-s", "ai-claude-x"],
            icon_name=ICON_AI_TMUX)
        self.assertEqual(self.win._resolve_new_tab_group(row), "purple")



if __name__ == "__main__":
    unittest.main()
