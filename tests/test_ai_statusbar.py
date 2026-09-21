"""Sidebar AI status bar: counts every AI tab, and its clicks find them."""
import os
import sys
import unittest

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkSource", "4")
gi.require_version("Vte", "2.91")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabit import ICON_AI_TMUX, Tabit  # noqa: E402


class _FakeRow:
    def __init__(self, status, icon=ICON_AI_TMUX, group=None):
        self.agent_status = status
        self.icon_name = icon
        self.group_color = group
        self.title_text = f"row-{status}"


class _Win:
    """Only the parts _refresh_ai_summaries touches."""

    _AI_ATTENTION_ORDER = Tabit._AI_ATTENTION_ORDER
    _ai_empty_buckets = Tabit._ai_empty_buckets
    _ai_viewport_band = staticmethod(lambda _r: "visible")

    def __init__(self, rows, collapsed=()):
        self._rows = rows
        self._collapsed_groups = set(collapsed)
        self.filled = {}
        self.ai_peek_top = object()
        self.ai_peek_bottom = object()
        self.ai_status_bar = None

    def _session_rows(self):
        return self._rows

    def _fill_ai_peek_bar(self, _bar, buckets, direction):
        self.filled[direction] = buckets

    def _fill_group_header_ai_summaries(self, _by_group):
        pass

    def _group_header_row(self, _color):
        return None

    def counts(self):
        Tabit._refresh_ai_summaries(self)
        return {k: len(v) for k, v in self._ai_peek_targets["bar"].items()}


class TestStatusBarCounts(unittest.TestCase):
    def test_counts_every_ai_tab(self):
        win = _Win([_FakeRow("working"), _FakeRow("working"),
                    _FakeRow("idle"), _FakeRow("blocked")])
        self.assertEqual(win.counts(),
                         {"blocked": 1, "ready": 0, "working": 2, "idle": 1})

    def test_counts_tabs_inside_collapsed_groups(self):
        """A collapsed group hides rows from the list, not from the count."""
        win = _Win([_FakeRow("blocked", group="red")], collapsed=["red"])
        self.assertEqual(win.counts()["blocked"], 1)

    def test_ignores_non_ai_tabs(self):
        win = _Win([_FakeRow("working", icon="utilities-terminal-symbolic"),
                    _FakeRow("working")])
        self.assertEqual(win.counts()["working"], 1)

    def test_ignores_unknown_status(self):
        win = _Win([_FakeRow("nonsense"), _FakeRow("idle")])
        self.assertEqual(sum(win.counts().values()), 1)

    def test_empty_when_no_ai_tabs(self):
        self.assertEqual(sum(_Win([]).counts().values()), 0)


class TestStatusBarBucketName(unittest.TestCase):
    """The click handler reads _ai_peek_targets by edge name."""

    def test_bar_edge_reads_its_own_bucket(self):
        win = _Win([_FakeRow("blocked")])
        win.counts()
        self.assertIn("bar", win._ai_peek_targets)
        self.assertNotEqual(win._ai_peek_targets["bar"],
                            win._ai_peek_targets["below"])

    def test_peek_edges_still_present(self):
        win = _Win([_FakeRow("idle")])
        win.counts()
        self.assertIn("above", win._ai_peek_targets)
        self.assertIn("below", win._ai_peek_targets)


if __name__ == "__main__":
    unittest.main()
