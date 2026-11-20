#!/usr/bin/env python3
"""Memory low: before systemd-oomd kills something, tabit puts idle AI tabs
to sleep, the one used longest ago first. A sleeping tab stays, and opening
it goes on with the same conversation."""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402

T = tabit.Tabit
SID = "11111111-2222-3333-4444-555555555555"


def argv(sid=SID):
    inner = T._ai_argv("claude", "/w", [f"--resume {sid}"] if sid else [])
    return T._ai_tmux_argv(inner, "ai-claude-w-1")


def row(name, status="idle", busy=0.0, sid=SID, **kw):
    r = types.SimpleNamespace(kind="term", term=object(), icon_name=tabit.ICON_AI_TMUX,
                              dead=False, agent_status=status, argv=argv(sid),
                              title_text=name, _last_busy=busy, _agent_hook_tx="")
    r.__dict__.update(kw)
    return r


class App:
    """The parts of Tabit the memory watch uses."""
    _ai_last_used = T._ai_last_used
    _ai_can_sleep = T._ai_can_sleep
    _ai_sleep_tick = T._ai_sleep_tick
    _ai_session_of = T._ai_session_of
    _ai_cli_path_of = T._ai_cli_path_of
    _ai_is_claude = staticmethod(T._ai_is_claude)
    _claude_session_saved = staticmethod(lambda p, s: T._claude_session_saved(p, s))

    def __init__(self, rows, on=True):
        self.rows = rows
        self.on = on
        self.slept = []
        self._mem_high = 0
        self._mem_wait = 0.0
        self._left_row = self._right_row = None

    def _session_rows(self):
        return self.rows

    def _load_settings(self):
        return {"ai_sleep_on_pressure": self.on}

    def _ai_sleep(self, r, _p):
        self.slept.append(r.title_text)
        r._asleep = True


class TestPressure(unittest.TestCase):

    def test_reads_full_avg10(self):
        text = ("some avg10=41.50 avg60=3.00 avg300=1.00 total=9\n"
                "full avg10=33.25 avg60=2.00 avg300=0.50 total=8\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=text)):
            self.assertEqual(tabit._mem_pressure(), 33.25)

    def test_none_when_unreadable(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertIsNone(tabit._mem_pressure())


class TestWhoCanSleep(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(T, "_claude_session_saved", return_value=True)
        self.saved = p.start()
        self.addCleanup(p.stop)

    def can(self, r, app=None):
        return (app or App([r]))._ai_can_sleep(r)

    def test_an_idle_or_done_tab_that_resumes_its_session(self):
        self.assertTrue(self.can(row("a", "idle")))
        self.assertTrue(self.can(row("a", "ready")))

    def test_never_a_busy_one(self):
        for st in ("working", "blocked", "background", "unknown", None):
            self.assertFalse(self.can(row("a", st)), st)

    def test_never_the_one_on_screen(self):
        r = row("a")
        app = App([r])
        app._right_row = r
        self.assertFalse(self.can(r, app))

    def test_never_one_that_would_not_come_back(self):
        self.assertFalse(self.can(row("a", sid=None)))          # starts fresh
        r = row("a")
        r._agent_hook_tx = "/x/99999999-2222-3333-4444-555555555555.jsonl"
        self.assertFalse(self.can(r))       # in another session than it resumes
        self.saved.return_value = False
        self.assertFalse(self.can(row("a")))    # claude never saved it

    def test_not_twice_nor_a_dead_or_plain_tab(self):
        self.assertFalse(self.can(row("a", _asleep=True)))
        self.assertFalse(self.can(row("a", dead=True)))
        self.assertFalse(self.can(row("a", icon_name="utilities-terminal")))
        self.assertFalse(self.can(row("a", _respawn=("x",))))


class TestTick(unittest.TestCase):

    def setUp(self):
        p = mock.patch.object(T, "_claude_session_saved", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.p = mock.patch.object(tabit, "_mem_pressure", return_value=60.0)
        self.pressure = self.p.start()
        self.addCleanup(self.p.stop)
        self.now = mock.patch.object(tabit.time, "monotonic", return_value=1000.0)
        self.clock = self.now.start()
        self.addCleanup(self.now.stop)

    def test_longest_idle_first_after_two_looks(self):
        app = App([row("new", busy=300), row("old", busy=100), row("mid", busy=200)])
        self.assertTrue(app._ai_sleep_tick())
        self.assertEqual(app.slept, [])                 # one look is not enough
        app._ai_sleep_tick()
        self.assertEqual(app.slept, ["old"])

    def test_one_then_a_wait(self):
        app = App([row("a", busy=1), row("b", busy=2)])
        for _ in range(4):
            app._ai_sleep_tick()
        self.assertEqual(app.slept, ["a"])
        self.clock.return_value = 1000.0 + tabit.AI_SLEEP_WAIT_SEC
        app._ai_sleep_tick()
        self.assertEqual(app.slept, ["a", "b"])

    def test_low_pressure_starts_the_count_again(self):
        app = App([row("a")])
        app._ai_sleep_tick()
        self.pressure.return_value = 5.0
        app._ai_sleep_tick()
        self.pressure.return_value = 60.0
        app._ai_sleep_tick()
        self.assertEqual(app.slept, [])

    def test_off_in_settings(self):
        app = App([row("a")], on=False)
        for _ in range(3):
            app._ai_sleep_tick()
        self.assertEqual(app.slept, [])

    def test_the_conversation_file_counts_as_use(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        tx = os.path.join(d, SID + ".jsonl")
        open(tx, "w").close()
        os.utime(tx, (500, 500))
        app = App([row("talked", busy=100, _agent_hook_tx=tx), row("quiet", busy=200)])
        app._ai_sleep_tick()
        app._ai_sleep_tick()
        self.assertEqual(app.slept, ["quiet"])


class TestSleepAndWake(unittest.TestCase):

    def gtk_row(self):
        r = row("a")
        r.term = mock.Mock()
        r.subtitle = mock.Mock()
        r.dot = mock.Mock()
        r.sub_text = "~/w"
        r._live_tmux = "ai-claude-w-1"
        r.get_style_context = mock.Mock()
        return r

    def test_sleep_ends_the_tmux_session_and_the_tab_stays_asleep(self):
        app = mock.Mock()
        r = self.gtk_row()
        with mock.patch.object(tabit, "_tmux_kill_session") as kill:
            T._ai_sleep(app, r, 40.0)
        kill.assert_called_once_with("ai-claude-w-1")
        self.assertTrue(r._asleep)
        # its attach ends: child-exited marks it asleep, not exited
        app._ai_mark_asleep = lambda rr: T._ai_mark_asleep(app, rr)
        T._on_child_exited(app, None, 0, r)
        self.assertTrue(r.dead and r._asleep)
        r.subtitle.set_text.assert_called_with("💤 asleep to free memory · open to wake")
        app._set_agent_status.assert_called_with(r, "exited", notify=False)

    def test_wake_runs_its_own_command_again(self):
        app = mock.Mock()
        app._ai_cd_path.return_value = "/nonexistent-dir"
        r = self.gtk_row()
        r._asleep = r.dead = True
        T._ai_wake(app, r)
        self.assertFalse(r._asleep or r.dead)
        app._spawn_term.assert_called_once_with(r, r.argv, tabit.GLib.get_home_dir())
        r.subtitle.set_text.assert_called_with("~/w")

    def test_the_phone_is_told_it_sleeps(self):
        r = row("a", _asleep=True, dead=True)
        self.assertIn("asleep", T._ai_row_trouble(r))
        self.assertIn("asleep", T._remote_tab_trouble(mock.Mock(), r))


class TestSaved(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        old = tabit.SESSIONS_FILE
        tabit.SESSIONS_FILE = os.path.join(d.name, "sessions.json")
        self.addCleanup(setattr, tabit, "SESSIONS_FILE", old)

    def test_a_sleeping_tab_is_saved_asleep_an_exited_one_not(self):
        rows = [types.SimpleNamespace(title_text=n, sub_text="", argv=["x"], icon_name="x",
                                      dead=True, _asleep=a) for n, a in (("s", True), ("e", False))]
        app = types.SimpleNamespace(_session_rows=lambda: rows, _term_cwd=lambda _r: None)
        T._save_sessions(app)
        with open(tabit.SESSIONS_FILE) as f:
            got = json.load(f)
        self.assertEqual([(e["label"], e.get("asleep")) for e in got], [("s", True)])


if __name__ == "__main__":
    unittest.main()
