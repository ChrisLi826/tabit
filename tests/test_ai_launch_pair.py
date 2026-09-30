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

from unittest import mock  # noqa: E402

import tabit  # noqa: E402
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



SID = "01a0f0c6-686f-7bf3-8770-68a390472632"


class TestFoundSession(unittest.TestCase):
    """codex and agy pick their own id; tabit reads it off the agent."""

    def test_codex_file(self):
        links = ["/dev/pts/3", "socket:[1]",
                 "/home/u/.codex/sessions/2026/09/30/"
                 "rollout-2026-09-30T13-25-29-%s.jsonl" % SID]
        self.assertEqual(Tabit._ai_session_id_from_links("codex", links),
                         SID)

    def test_agy_file_not_its_side_files(self):
        base = "/home/u/.gemini/antigravity-cli/conversations/%s" % SID
        self.assertIsNone(Tabit._ai_session_id_from_links(
            "agy", [base + ".db-wal", base + ".db-shm"]))
        self.assertEqual(Tabit._ai_session_id_from_links(
            "/usr/local/bin/agy", [base + ".db-wal", base + ".db"]), SID)

    def test_another_cli_finds_nothing(self):
        links = ["/home/u/.codex/sessions/x-%s.jsonl" % SID]
        self.assertIsNone(Tabit._ai_session_id_from_links("claude", links))
        self.assertIsNone(Tabit._ai_session_id_from_links("agy", links))

    def test_agy_resumes_with_conversation(self):
        self.assertEqual(Tabit._ai_tries_with_id("agy", [], SID),
                         ["--conversation " + SID])

    def test_a_cli_given_as_a_path_resumes_its_own_way(self):
        self.assertEqual(Tabit._ai_tries_with_id("/usr/bin/agy", [], SID),
                         ["--conversation " + SID])
        self.assertEqual(
            Tabit._ai_tries_with_id("/usr/local/bin/codex", [], SID),
            ["resume " + SID])

    def test_argv_with_session_codex_tmux(self):
        _l, stored, _i = Tabit._ai_launch_pair(
            "codex", "/tmp/a b", ["resume --last"], tmux=True,
            continue_now=False)
        new = Tabit._ai_argv_with_session(stored, SID)
        inner, sess = Tabit._ai_tmux_unwrap(new)
        self.assertEqual(sess, Tabit._ai_tmux_unwrap(stored)[1])
        self.assertEqual(Tabit._ai_tries_of(inner[2]), ["resume " + SID])
        self.assertEqual(Tabit._ai_cli_path_of(new), ("codex", "/tmp/a b"))

    def test_argv_with_session_keeps_bypass(self):
        _l, stored, _i = Tabit._ai_launch_pair(
            "claude", "/tmp", ["--continue"], bypass=True)
        new = Tabit._ai_argv_with_session(stored, SID)
        self.assertTrue(Tabit._ai_has_bypass(new))
        self.assertIn("--resume " + SID, new[2])

    def test_new_codex_session_starts_fresh_until_its_id_is_known(self):
        # "resume --last" before the first message would pick up the
        # newest session in the folder: another tab's.
        _l, stored, _i = Tabit._ai_launch_pair(
            "codex", "/tmp", ["resume --last"], continue_now=False,
            resume_later=True)
        self.assertEqual(Tabit._ai_argv_plain(stored), stored)
        # continuing now keeps the tries, as before
        _l, stored, _i = Tabit._ai_launch_pair(
            "codex", "/tmp", ["resume --last"], continue_now=True)
        self.assertIn("resume --last", stored[2])

    def test_hunt_scan_finds_the_agent_under_a_plain_tab(self):
        row = object()
        link = "/x/.codex/sessions/r-%s.jsonl" % SID
        with mock.patch.object(Tabit, "_proc_children",
                               staticmethod(lambda: {50: [51]})), \
                mock.patch.object(Tabit, "_proc_open_files",
                                  staticmethod(lambda root, kids:
                                               [link] if root == 50 else [])):
            got = Tabit._ai_hunt_scan([(row, "codex", None, 50)])
        self.assertEqual(got, {row: SID})

    def test_hunt_scan_uses_the_tmux_pane(self):
        row = object()
        link = "/x/antigravity-cli/conversations/%s.db" % SID
        out = mock.Mock(stdout="ai-agy-x 70\nother 80\n")
        with mock.patch.object(tabit.subprocess, "run", return_value=out), \
                mock.patch.object(Tabit, "_proc_children",
                                  staticmethod(lambda: {})), \
                mock.patch.object(Tabit, "_proc_open_files",
                                  staticmethod(lambda root, kids:
                                               [link] if root == 70 else [])):
            got = Tabit._ai_hunt_scan([(row, "agy", "ai-agy-x", None)])
        self.assertEqual(got, {row: SID})

    def test_open_files_walks_the_tree(self):
        kids = {10: [11], 11: [12]}
        with mock.patch.object(tabit.os, "listdir",
                               lambda d: ["0"] if d == "/proc/12/fd"
                               else []), \
                mock.patch.object(tabit.os, "readlink",
                                  lambda p: "/x/.codex/sessions/r-%s.jsonl"
                                  % SID):
            links = Tabit._proc_open_files(10, kids)
        self.assertEqual(Tabit._ai_session_id_from_links("codex", links),
                         SID)


class TestTakesNewId(unittest.TestCase):
    """--session-id only for a program whose --help shows it."""

    def _ask(self, cli, help_text, exists=True):
        Tabit._ai_new_id_cache.clear()
        out = mock.Mock(stdout=help_text, stderr="", returncode=0)
        with mock.patch.object(tabit.shutil, "which",
                               lambda c: "/bin/" + c if exists else None), \
                mock.patch.object(tabit.subprocess, "run",
                                  return_value=out):
            return Tabit._ai_takes_new_id(cli)

    def test_the_xai_grok(self):
        self.assertTrue(self._ask(
            "grok", "  -s, --session-id <SESSION_ID>\n"))

    def test_another_grok(self):
        self.assertFalse(self._ask("grok", "  -k, --api-key <key>\n"))

    def test_a_cli_not_in_the_list(self):
        self.assertFalse(self._ask("codex", "--session-id"))

    def test_a_slow_help_is_asked_again(self):
        Tabit._ai_new_id_cache.clear()
        with mock.patch.object(tabit.shutil, "which", lambda c: "/bin/x"), \
                mock.patch.object(tabit.subprocess, "run",
                                  side_effect=tabit.subprocess.TimeoutExpired(
                                      "x", 5)):
            self.assertFalse(Tabit._ai_takes_new_id("claude"))
        self.assertEqual(Tabit._ai_new_id_cache, {})

    def test_a_failed_help_is_asked_again(self):
        Tabit._ai_new_id_cache.clear()
        out = mock.Mock(stdout="", stderr="boom", returncode=1)
        with mock.patch.object(tabit.shutil, "which", lambda c: "/bin/x"), \
                mock.patch.object(tabit.subprocess, "run", return_value=out):
            self.assertFalse(Tabit._ai_takes_new_id("claude"))
        self.assertEqual(Tabit._ai_new_id_cache, {})

    def test_not_installed(self):
        self.assertFalse(self._ask("claude", "--session-id", exists=False))



class _Row:
    icon_name = ICON_AI_TMUX
    dead = False
    term = object()
    pid = None

    def __init__(self, argv, hunt=False):
        self.argv = argv
        self.ai_hunt = hunt


class _Win:
    """Just enough window for the hunt's main-loop half."""

    _ai_hunt_jobs = Tabit._ai_hunt_jobs
    _ai_hunt_wanted = Tabit._ai_hunt_wanted
    _ai_hunt_apply = Tabit._ai_hunt_apply
    _ai_cli_path_of = Tabit._ai_cli_path_of
    _ai_tmux_unwrap = Tabit._ai_tmux_unwrap
    _ai_argv_plain = Tabit._ai_argv_plain
    _ai_argv_with_session = Tabit._ai_argv_with_session

    def __init__(self, rows):
        self.rows = rows
        self.saved = 0

    def _session_rows(self):
        return self.rows

    def _save_sessions_soon(self):
        self.saved += 1


class TestHuntMainLoop(unittest.TestCase):
    def _codex(self, later, hunt):
        _l, stored, _i = Tabit._ai_launch_pair(
            "codex", "/tmp", ["resume --last"], tmux=True,
            continue_now=False, resume_later=later)
        return _Row(stored, hunt=hunt)

    def test_a_new_tab_waiting_for_its_id_is_looked_at(self):
        r = self._codex(True, True)
        self.assertEqual([j[0] for j in _Win([r])._ai_hunt_jobs()], [r])

    def test_a_tab_set_to_start_fresh_is_left_alone(self):
        r = self._codex(False, False)
        self.assertEqual(_Win([r])._ai_hunt_jobs(), [])

    def test_a_found_id_is_written(self):
        r = self._codex(True, True)
        w = _Win([r])
        w._ai_hunt_apply({r: SID}, {r: r.argv})
        self.assertIn("resume " + SID, Tabit._ai_tmux_unwrap(r.argv)[0][2])
        self.assertEqual(w.saved, 1)

    def test_resume_turned_off_during_the_scan_stays_off(self):
        r = self._codex(True, True)
        w = _Win([r])
        seen = {r: r.argv}
        # the rename popover unticks Resume while the scan runs
        r.argv = self._codex(False, False).argv
        r.ai_hunt = False
        w._ai_hunt_apply({r: SID}, seen)
        self.assertNotIn(SID, r.argv[2])
        self.assertEqual(w.saved, 0)


if __name__ == "__main__":
    unittest.main()
