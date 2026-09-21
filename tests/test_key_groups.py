"""Shortcut grouping: every action is filed, and a group never splits."""
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
from tabit import (  # noqa: E402
    DEFAULT_KEYS, KEY_ACTIONS, _key_action_groups, _split_key_groups,
)


class TestKeyActions(unittest.TestCase):
    def test_every_action_has_a_group(self):
        for action, label, default, group in KEY_ACTIONS:
            self.assertTrue(group and group.strip(),
                            f"{action} has no group")
            self.assertTrue(label and default, action)

    def test_action_ids_unique(self):
        ids = [a for a, _l, _d, _g in KEY_ACTIONS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_default_keys_covers_every_action(self):
        self.assertEqual(set(DEFAULT_KEYS), {a for a, _l, _d, _g in KEY_ACTIONS})


class TestGrouping(unittest.TestCase):
    def test_groups_keep_every_action(self):
        grouped = [a for _name, items in _key_action_groups()
                   for a, _l, _d in items]
        self.assertEqual(grouped, [a for a, _l, _d, _g in KEY_ACTIONS])

    def test_each_group_appears_once(self):
        """Entries of one group must stay adjacent in KEY_ACTIONS."""
        names = [name for name, _items in _key_action_groups()]
        self.assertEqual(len(names), len(set(names)),
                         f"a group is declared in two places: {names}")

    def test_no_empty_group(self):
        for name, items in _key_action_groups():
            self.assertTrue(items, name)


class TestColumnSplit(unittest.TestCase):
    def setUp(self):
        self.groups = _key_action_groups()
        self.left, self.right = _split_key_groups(self.groups)

    def test_both_columns_used(self):
        self.assertTrue(self.left)
        self.assertTrue(self.right)

    def test_split_keeps_groups_whole(self):
        self.assertEqual(self.left + self.right, self.groups)

    def test_columns_are_roughly_even(self):
        rows = lambda col: sum(1 + len(i) for _n, i in col)  # noqa: E731
        self.assertLessEqual(abs(rows(self.left) - rows(self.right)), 6)

    def test_split_is_the_evenest_available(self):
        rows = [1 + len(i) for _n, i in self.groups]
        total = sum(rows)
        best = min(abs(sum(rows[:i]) - (total - sum(rows[:i])))
                   for i in range(1, len(self.groups)))
        got = abs(sum(1 + len(i) for _n, i in self.left)
                  - sum(1 + len(i) for _n, i in self.right))
        self.assertEqual(got, best)


if __name__ == "__main__":
    unittest.main()
