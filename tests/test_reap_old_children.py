#!/usr/bin/env python3
"""A restart execs tabit in place, so what the old tabit spawned stays its
child and exits into a zombie nobody waits for. The new tabit waits for the
ones an earlier run started, and leaves its own to whoever spawned them."""
import os
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


def _zombie():
    """A child that has exited and that nothing waits for, like one an
    earlier tabit spawned."""
    pid = os.posix_spawn("/bin/true", ["true"], dict(os.environ))
    for _ in range(200):
        state = tabit._proc_state_start(pid)
        if state and state[0] == "Z":
            return pid
        time.sleep(0.01)
    raise AssertionError("the child did not exit")


def _gone(pid):
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


class TestReapOldChildren(unittest.TestCase):
    def test_a_zombie_from_an_earlier_run_is_waited_for(self):
        pid = _zombie()
        self.assertIn(pid, tabit._child_pids())
        self.assertEqual(tabit._reap_old_children(since=float("inf"), pids=[pid]), 1)
        self.assertTrue(_gone(pid))
        self.assertNotIn(pid, tabit._child_pids())

    def test_a_zombie_this_run_started_is_left_alone(self):
        pid = _zombie()
        self.addCleanup(lambda: _gone(pid))
        self.assertEqual(tabit._reap_old_children(since=0.0, pids=[pid]), 0)
        self.assertEqual(tabit._proc_state_start(pid)[0], "Z")     # still there for its owner

    def test_a_live_child_is_left_alone(self):
        pid = os.posix_spawn("/bin/sleep", ["sleep", "5"], dict(os.environ))
        self.addCleanup(lambda: (os.kill(pid, 9), os.waitpid(pid, 0)))
        tabit._reap_old_children(since=float("inf"), pids=[pid])
        self.assertEqual(os.waitpid(pid, os.WNOHANG), (0, 0))      # running, not waited for

    def test_the_timer_lives_on_when_a_round_fails(self):
        with mock.patch.object(tabit, "_reap_old_children", side_effect=OSError("boom")), \
                mock.patch("sys.stderr"):
            self.assertIs(tabit._reap_tick(True), True)        # the 60 s timer goes on
            self.assertIs(tabit._reap_tick(False), False)      # the first round runs once


if __name__ == "__main__":
    unittest.main()
