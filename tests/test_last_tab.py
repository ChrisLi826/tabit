"""Ctrl+Tab jump-back: what counts as having stayed on a tab."""
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
from gi.repository import Gdk  # noqa: E402
from tabit import Tabit  # noqa: E402


CTRL = Gdk.ModifierType.CONTROL_MASK
ALT = Gdk.ModifierType.MOD1_MASK


class _Row:
    def __init__(self, name, attached=True):
        self.name = name
        self._attached = attached

    def get_parent(self):
        return object() if self._attached else None

    def close(self):
        self._attached = False

    def __repr__(self):
        return f"<{self.name}>"


class _Win:
    """Drives the dwell state machine without GTK timers or a real list."""

    _cancel_tab_dwell = Tabit._cancel_tab_dwell
    _start_tab_dwell = Tabit._start_tab_dwell
    _commit_tab_dwell = Tabit._commit_tab_dwell
    _jump_to_last_tab = Tabit._jump_to_last_tab
    _tab_dwell_done = Tabit._tab_dwell_done
    _live_row = staticmethod(Tabit._live_row)

    def __init__(self):
        self._dwelt_row = None
        self._prev_dwelt_row = None
        self._dwell_row = None
        self._dwell_mods = 0
        self._dwell_src = None
        self._dwell_elapsed = False
        self.held = 0          # modifiers the test says are physically down
        self.selected = None
        self._held_mods = lambda: self.held
        self.listbox = self

    # listbox stand-in
    def select_row(self, row):
        self.selected = row
        self.visit(row)

    def visit(self, row):
        """A tab switch, as _on_row_selected reports it."""
        self._start_tab_dwell(row)

    def wait(self):
        """The dwell timer firing."""
        if self._dwell_src is None:
            return
        self._dwell_src = None
        self._dwell_elapsed = True
        self._commit_tab_dwell()

    def _scroll_to_row(self, _row):
        return False

    def release_mods(self):
        self.held = 0
        self._commit_tab_dwell()


class _NoTimers:
    """Timer handles become plain ints; tests fire the dwell by hand.

    Scoped per test: GLib is shared, so patching it at import time would
    leak into every other test module.
    """

    def setUp(self):
        import tabit
        self._glib = (tabit.GLib.timeout_add, tabit.GLib.source_remove)
        tabit.GLib.timeout_add = lambda *_a, **_k: 1
        tabit.GLib.source_remove = lambda *_a, **_k: None
        self.addCleanup(self._restore)
        super().setUp()

    def _restore(self):
        import tabit
        tabit.GLib.timeout_add, tabit.GLib.source_remove = self._glib


class TestDwell(_NoTimers, unittest.TestCase):
    def setUp(self):
        self.w = _Win()
        self.a, self.b, self.c, self.d = (_Row("A"), _Row("B"),
                                          _Row("C"), _Row("D"))

    def test_staying_makes_a_tab_the_target(self):
        self.w.visit(self.a); self.w.wait()
        self.w.visit(self.b); self.w.wait()
        self.assertIs(self.w._dwelt_row, self.b)
        self.assertIs(self.w._prev_dwelt_row, self.a)

    def test_tabs_passed_through_are_skipped(self):
        """A→B→C→D in a hurry: only A and D were stayed on."""
        self.w.visit(self.a); self.w.wait()
        self.w.visit(self.b)          # no wait — moved on
        self.w.visit(self.c)          # no wait
        self.w.visit(self.d); self.w.wait()
        self.assertIs(self.w._prev_dwelt_row, self.a)

    def test_held_ctrl_defers_the_promotion(self):
        """Ctrl+PageDown held down is one gesture, however long it pauses."""
        self.w.visit(self.a); self.w.wait()
        self.w.held = CTRL
        self.w.visit(self.b)
        self.w.wait()                 # timer elapsed, but Ctrl is still down
        self.assertIs(self.w._dwelt_row, self.a)
        self.w.visit(self.c)          # same gesture carries on
        self.w.wait()
        self.assertIs(self.w._dwelt_row, self.a)
        self.w.release_mods()         # now it lands
        self.assertIs(self.w._dwelt_row, self.c)
        self.assertIs(self.w._prev_dwelt_row, self.a)

    def test_any_modifier_works_not_just_ctrl(self):
        """Navigation shortcuts are user-editable; Alt+Up must behave too."""
        self.w.visit(self.a); self.w.wait()
        self.w.held = ALT
        self.w.visit(self.b); self.w.wait()
        self.assertIs(self.w._dwelt_row, self.a)   # still mid-gesture
        self.w.release_mods()
        self.assertIs(self.w._dwelt_row, self.b)

    def test_a_different_modifier_does_not_hold_the_gesture(self):
        """Resting on Shift must not freeze a Ctrl gesture's promotion."""
        self.w.visit(self.a); self.w.wait()
        self.w.held = CTRL
        self.w.visit(self.b)
        self.w.held = ALT          # Ctrl let go, something else pressed
        self.w.wait()
        self.assertIs(self.w._dwelt_row, self.b)

    def test_losing_focus_ends_the_gesture(self):
        """Ctrl let go in another window: its release never reaches us."""
        self.w.visit(self.a); self.w.wait()
        self.w.held = CTRL
        self.w.visit(self.b)
        self.w.wait()                       # deferred, Ctrl still down
        self.assertIs(self.w._dwelt_row, self.a)
        self.w._commit_tab_dwell(ignore_mods=True)   # focus-out
        self.assertIs(self.w._dwelt_row, self.b)

    def test_closing_the_settled_tab_keeps_the_older_target(self):
        self.w.visit(self.a); self.w.wait()
        self.w.visit(self.b); self.w.wait()  # dwelt=B prev=A
        self.b.close()
        self.w.visit(self.c); self.w.wait()
        self.assertIs(self.w._prev_dwelt_row, self.a)  # not the closed B

    def test_reselecting_the_same_tab_changes_nothing(self):
        self.w.visit(self.a); self.w.wait()
        self.w.visit(self.b); self.w.wait()
        self.w.visit(self.b); self.w.wait()
        self.assertIs(self.w._prev_dwelt_row, self.a)


class TestJump(_NoTimers, unittest.TestCase):
    def setUp(self):
        self.w = _Win()
        self.a, self.b = _Row("A"), _Row("B")
        self.w.visit(self.a); self.w.wait()
        self.w.visit(self.b); self.w.wait()

    def test_jump_goes_back(self):
        self.assertTrue(self.w._jump_to_last_tab())
        self.assertIs(self.w.selected, self.a)

    def test_jump_twice_returns(self):
        self.w._jump_to_last_tab()
        self.w._jump_to_last_tab()
        self.assertIs(self.w.selected, self.b)

    def test_jump_toggles_indefinitely(self):
        seen = []
        for _ in range(4):
            self.w._jump_to_last_tab()
            seen.append(self.w.selected)
        self.assertEqual(seen, [self.a, self.b, self.a, self.b])

    def test_jump_before_the_new_tab_settles(self):
        """On C but not settled yet: Ctrl+Tab belongs back on B, not A."""
        c = _Row("C")
        self.w.visit(c)                      # no wait — still a candidate
        self.assertTrue(self.w._jump_to_last_tab())
        self.assertIs(self.w.selected, self.b)
        self.w._jump_to_last_tab()           # and straight back to C
        self.assertIs(self.w.selected, c)

    def test_no_target_yet(self):
        self.assertFalse(_Win()._jump_to_last_tab())

    def test_closed_target_is_dropped(self):
        self.a.close()
        self.assertFalse(self.w._jump_to_last_tab())
        self.assertIsNone(self.w._prev_dwelt_row)


if __name__ == "__main__":
    unittest.main()
