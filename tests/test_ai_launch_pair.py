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

    def test_new_claude_session_resumes_its_own_id(self):
        launch, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, continue_now=False, resume_later=True,
            new_sid="11111111-2222-3333-4444-555555555555")
        self.assertIn("--session-id 11111111-2222-3333-4444-555555555555",
                      launch[2])
        self.assertNotIn("--continue", launch[2])
        # one command, no fallback that would start a second agent
        self.assertNotIn("||", launch[2].split("exit 1;", 1)[1])
        # its own id, then a plain start; never another tab's session
        self.assertIn("--resume 11111111-2222-3333-4444-555555555555 ||",
                      stored[2])
        self.assertNotIn("--continue", stored[2])

    def test_new_id_only_where_it_applies(self):
        sid = "11111111-2222-3333-4444-555555555555"
        # fresh after restart: no id kept
        _, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, continue_now=False, resume_later=False,
            new_sid=sid)
        self.assertNotIn(sid, stored[2])
        # continuing now: the new id is not used
        launch, _, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, continue_now=True, new_sid=sid)
        self.assertNotIn(sid, launch[2])
        # a CLI with no "new with id" flag keeps the generic behaviour
        launch, stored, _ = Tabit._ai_launch_pair(
            "codex", "/tmp", ["resume --last"], continue_now=False,
            new_sid=sid)
        self.assertNotIn(sid, launch[2] + stored[2])

    def test_new_id_survives_tmux(self):
        sid = "11111111-2222-3333-4444-555555555555"
        launch, stored, _ = Tabit._ai_launch_pair(
            "claude", "/tmp", TRIES, tmux=True, continue_now=False,
            new_sid=sid)
        self.assertIn(sid, Tabit._ai_tmux_unwrap(launch)[0][2])
        self.assertIn(sid, Tabit._ai_tmux_unwrap(stored)[0][2])


if __name__ == "__main__":
    unittest.main()
