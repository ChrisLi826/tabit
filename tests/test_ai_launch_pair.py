#!/usr/bin/env python3
"""+AI: "continue now" and "resume after restart" are separate choices."""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

from tabit import ICON_AI, ICON_AI_TMUX, Tabit  # noqa: E402

TRIES = ["--continue"]


def has_tries(argv):
    inner, _ = Tabit._ai_tmux_unwrap(argv)
    return Tabit._ai_argv_plain(inner) != inner


class TestAiLaunchPair(unittest.TestCase):
    def test_four_combos_plain(self):
        for now in (True, False):
            for later in (True, False):
                launch, stored, icon = Tabit._ai_launch_pair(
                    "claude", "/tmp", TRIES,
                    continue_now=now, resume_later=later)
                self.assertEqual(icon, ICON_AI)
                self.assertEqual(has_tries(launch), now, (now, later))
                self.assertEqual(has_tries(stored), later, (now, later))

    def test_four_combos_tmux_share_one_name(self):
        for now in (True, False):
            for later in (True, False):
                launch, stored, icon = Tabit._ai_launch_pair(
                    "claude", "/tmp", TRIES, tmux=True,
                    continue_now=now, resume_later=later)
                self.assertEqual(icon, ICON_AI_TMUX)
                self.assertEqual(has_tries(launch), now)
                self.assertEqual(has_tries(stored), later)
                # a restore must reattach to the agent that launch made
                self.assertEqual(Tabit._ai_tmux_unwrap(launch)[1],
                                 Tabit._ai_tmux_unwrap(stored)[1])

    def test_new_now_gets_its_own_tmux_session(self):
        stable = Tabit._ai_tmux_session("claude", "/tmp")
        _, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, tmux=True, continue_now=False)
        self.assertNotEqual(Tabit._ai_tmux_unwrap(stored)[1], stable)
        _, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, tmux=True, continue_now=True)
        self.assertEqual(Tabit._ai_tmux_unwrap(stored)[1], stable)

    def test_session_id_kept_for_restart(self):
        tries = Tabit._ai_tries_with_id("claude", TRIES, "abc-123")
        _, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", tries, continue_now=True, resume_later=True)
        self.assertIn("abc-123", stored[2])


if __name__ == "__main__":
    unittest.main()
