#!/usr/bin/env python3
"""JSON Format: a bad JSON puts the cursor where the parser stopped."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
gi.require_version("GtkSource", "4")
from gi.repository import GtkSource  # noqa: E402

import tabit  # noqa: E402


class TestNoteJsonError(unittest.TestCase):

    def _run(self, text, select=None):
        buf = GtkSource.Buffer()
        buf.set_text(text)
        if select:
            buf.select_range(buf.get_iter_at_offset(select[0]),
                             buf.get_iter_at_offset(select[1]))
        row = type("Row", (), {"buffer": buf, "view": None})()
        msgs = []
        app = type("App", (), {
            "_note_get_range": tabit.Tabit._note_get_range,
            "_note_msg": lambda _s, *a: msgs.append(a)})()
        tabit.Tabit._note_json_format(app, row)
        it = buf.get_iter_at_mark(buf.get_insert())
        return it.get_line() + 1, it.get_line_offset() + 1, msgs

    def test_cursor_goes_to_error(self):
        text = '{\n  "a": {\n    "b": 1,\n  }\n}\n'
        line, col, msgs = self._run(text)
        self.assertEqual((line, col), (4, 3))
        self.assertIn("Line 4, col 3", msgs[0][2])

    def test_error_inside_selection(self):
        # only the second line is selected; its offset counts from there
        text = 'keep\n{"a": 1,}\n'
        line, col, _ = self._run(text, select=(5, 15))
        self.assertEqual((line, col), (2, 9))


if __name__ == "__main__":
    unittest.main()
