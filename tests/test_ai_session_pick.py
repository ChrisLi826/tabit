#!/usr/bin/env python3
"""+AI Session ID: one exact session, its own tmux, and a list to pick from."""
import json
import os
import sys
import tempfile
import time
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402

T = tabit.Tabit
A = "aaaaaaaa-1111-2222-3333-444444444444"
B = "bbbbbbbb-1111-2222-3333-444444444444"
C = "cccccccc-1111-2222-3333-444444444444"


def pair(**kw):
    kw.setdefault("tmux", True)
    return T._ai_launch_pair("claude", "/home/max/proj", ["--continue"],
                             continue_now=True, resume_later=True, **kw)


class TestExactSession(unittest.TestCase):

    def test_each_id_has_its_own_tmux(self):
        # the bug: every id in one folder attached to the same session
        na = T._ai_tmux_unwrap(pair(resume_id=A)[0])[1]
        nb = T._ai_tmux_unwrap(pair(resume_id=B)[0])[1]
        self.assertNotEqual(na, nb)
        self.assertEqual(na, T._ai_tmux_unwrap(pair(resume_id=A)[1])[1])
        self.assertTrue(na.endswith("-aaaaaaaa"))

    def test_no_id_keeps_the_old_name(self):
        # existing tmux tabs must still match on restore
        self.assertEqual(T._ai_tmux_unwrap(pair()[0])[1],
                         T._ai_tmux_session("claude", "/home/max/proj"))

    def test_no_continue_behind_an_id(self):
        launch = T._ai_tmux_unwrap(pair(resume_id=A)[0])[0][2]
        self.assertIn(f"--resume {A}", launch)
        self.assertNotIn("--continue", launch)
        self.assertIn("could not open session", launch)  # says so, and stays

    def test_background_session_is_attached(self):
        # claude refuses --resume for a session running in the background;
        # `claude attach <short id>` opens it
        launch = T._ai_tmux_unwrap(pair(resume_id=A)[0])[0][2]
        self.assertLess(launch.index(f"--resume {A}"), launch.index("attach aaaaaaaa"))
        codex = T._ai_launch_pair("codex", "/p", [], continue_now=True, resume_id=A)[0]
        self.assertNotIn("attach", codex[2])

    def test_restart_resumes_that_session_only(self):
        stored = T._ai_tmux_unwrap(pair(resume_id=A)[1])[0]
        self.assertEqual(T._ai_tries_of(stored[2]), [f"--resume {A}", "attach aaaaaaaa"])
        self.assertNotIn("--continue", stored[2])
        self.assertEqual(T._ai_cli_path_of(stored), ("claude", "/home/max/proj"))

    def test_plain_tab_too(self):
        launch, stored, _ = pair(resume_id=A, tmux=False)
        self.assertNotIn("--continue", launch[2] + stored[2])

    def test_id_only_for_continue_now(self):
        launch, _s, _i = T._ai_launch_pair(
            "claude", "/p", [], continue_now=False, resume_id=A, tmux=True)
        self.assertNotIn(A, launch[2])

    def test_longer_name_still_parses(self):
        name = T._ai_tmux_unwrap(pair(resume_id=A)[0])[1]
        self.assertEqual(T._tmux_ai_cli(name), "claude")
        self.assertEqual(T._tmux_session_category(name), "ai")


class TestSessionList(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        for name, value in (("CLAUDE_PROJECTS_DIR", os.path.join(d.name, "projects")),
                            ("AGENT_HOOK_DIR", os.path.join(d.name, "agent"))):
            old = getattr(tabit, name)
            setattr(tabit, name, value)
            self.addCleanup(setattr, tabit, name, old)
        os.makedirs(tabit.AGENT_HOOK_DIR)
        self.folder = os.path.join(d.name, "my_proj")
        os.makedirs(self.folder)
        self.dir = os.path.join(tabit.CLAUDE_PROJECTS_DIR,
                                self.folder.replace("/", "-").replace("_", "-"))
        os.makedirs(self.dir)

    def session(self, sid, lines, age=0):
        path = os.path.join(self.dir, sid + ".jsonl")
        with open(path, "w") as f:
            for d in lines:
                f.write(json.dumps(d, separators=(",", ":")) + "\n")   # as claude writes
        t = time.time() - age
        os.utime(path, (t, t))
        return path

    @staticmethod
    def user(text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def test_titles(self):
        p = self.session(A, [self.user("<command>x</command>"), self.user("Fix the login bug"),
                             {"type": "ai-title", "aiTitle": "Login fix"},
                             {"type": "custom-title", "customTitle": "My login work"}])
        self.assertEqual(T._claude_session_title(p), "My login work")
        p = self.session(B, [self.user("Fix the login bug"),
                             {"type": "ai-title", "aiTitle": "Login fix"}])
        self.assertEqual(T._claude_session_title(p), "Login fix")
        p = self.session(C, [self.user("[Request interrupted by user]"),
                             self.user("Write the README")])
        self.assertEqual(T._claude_session_title(p), "Write the README")

    @staticmethod
    def said(text):
        return {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hm"}, {"type": "text", "text": text}]}}

    @staticmethod
    def tool_result():
        return {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}

    def test_last_prompt_and_reply(self):
        p = self.session(A, [
            self.user("First question"), self.said("First answer"),
            self.user("Second question"),
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            self.tool_result(), self.said("Second answer"),
            dict(self.user("Stop hook feedback: write in Chinese"), isMeta=True),
            dict(self.user("This session is being continued from a previous conversation"),
                 isCompactSummary=True),
            self.user("[Request interrupted by user]"),
            {"type": "last-prompt", "lastPrompt": "x"}])
        self.assertEqual(T._claude_session_last(p), ("Second question", "Second answer"))

    def test_last_prompt_far_back(self):
        # tool output after the prompt fills more than one read step
        filler = [{"type": "attachment", "blob": "x" * 5000}] * 40
        p = self.session(A, [self.user("The real question"), self.said("Reply")] + filler)
        self.assertEqual(T._claude_session_last(p, step=4096),
                         ("The real question", "Reply"))

    def test_read_stops_at_the_limit(self):
        filler = [{"type": "attachment", "blob": "x" * 5000}] * 40
        p = self.session(A, [self.user("Too far back")] + filler)
        self.assertEqual(T._claude_session_last(p, limit=8192, step=4096), (None, None))

    def test_headless_run_is_left_out(self):
        p = self.session(A, [dict(self.user("Curate the notes"), entrypoint="sdk-cli")])
        self.assertIsNone(T._claude_session_title(p))

    def test_no_title_no_entry(self):
        p = self.session(A, [{"type": "mode", "mode": "normal"}])
        self.assertIsNone(T._claude_session_title(p))

    def app(self, rows, tmux=(), active=()):
        app = types.SimpleNamespace(
            _session_rows=lambda: rows, _agent_hook_key=T._agent_hook_key,
            _ai_tmux_sessions=lambda: list(tmux),
            _claude_active_sessions=lambda: list(active),
            _claude_session_title=T._claude_session_title,
            _claude_session_last=T._claude_session_last)
        app._ai_session_ids_in_use = lambda: T._ai_session_ids_in_use(app)
        return app

    def test_free_sessions_newest_first_without_held_ones(self):
        self.session(A, [self.user("held by a tab's command")], age=10)
        self.session(B, [self.user("held through the hooks")], age=20)
        self.session(C, [self.user("free and older")], age=3000)
        free = "dddddddd-1111-2222-3333-444444444444"
        self.session(free, [self.user("free and newest")], age=1)
        with open(os.path.join(tabit.AGENT_HOOK_DIR, "ai-claude-x-1"), "w") as f:
            f.write(f"idle\n{self.dir}/{B}.jsonl\n0\n\n\n")
        tab = types.SimpleNamespace(icon_name=tabit.ICON_AI,
                                    argv=["/bin/sh", "-c", f"claude --resume {A}"])
        app = self.app([tab], tmux=[("ai-claude-x-1", "claude", "/p", False)])
        got = T._claude_free_sessions(app, self.folder)
        self.assertEqual([s[0] for s in got], [free, C])
        self.assertEqual(got[0][1], "free and newest")
        self.assertEqual(got[0][3], "free and newest")     # its last prompt

    def test_resume_with_no_id_takes_the_newest_free_one(self):
        self.session(A, [self.user("held")], age=1)
        self.session(B, [self.user("newest free")], age=50)
        self.session(C, [self.user("older free")], age=500)
        tab = types.SimpleNamespace(icon_name=tabit.ICON_AI,
                                    argv=["/bin/sh", "-c", f"claude --resume {A}"])
        app = self.app([tab])
        app._claude_free_sessions = lambda *a, **k: T._claude_free_sessions(app, *a, **k)
        app._ai_is_claude = T._ai_is_claude
        self.assertEqual(T._ai_newest_free_session(app, "claude", self.folder)[0], B)
        self.assertIsNone(T._ai_newest_free_session(app, "codex", self.folder))
        self.assertIsNone(T._ai_newest_free_session(app, "claude", "/no/such/folder"))

    def test_running_sessions_are_left_out(self):
        # open in another terminal, or running in the background
        self.session(A, [self.user("in the background")], age=1)
        self.session(B, [self.user("free")], age=50)
        app = self.app([], active=[{"sessionId": A, "kind": "background", "id": "aaaaaaaa"}])
        self.assertEqual([s[0] for s in T._claude_free_sessions(app, self.folder)], [B])

    def test_folder_with_no_sessions(self):
        self.assertEqual(T._claude_free_sessions(self.app([]), "/no/such/folder"), [])

    def test_ago(self):
        self.assertEqual(T._ago(time.time() - 7200), "2 h ago")
        self.assertEqual(T._ago(time.time()), "just now")


if __name__ == "__main__":
    unittest.main()


class TestFollowSession(unittest.TestCase):
    """A tab comes back to the session its claude is in now, not an old one."""

    def setUp(self):
        self.saved = []
        self.app = types.SimpleNamespace(
            _ai_argv_with_session=T._ai_argv_with_session,
            _save_sessions_soon=lambda: self.saved.append(1))

    def row(self, argv, tx_sid, hunt=False):
        return types.SimpleNamespace(
            argv=argv, ai_hunt=hunt,
            _agent_hook_tx=f"/home/max/.claude/projects/-p/{tx_sid}.jsonl" if tx_sid else "")

    def follow(self, row):
        T._ai_follow_session(self.app, row)
        return row.argv

    def test_after_clear_the_new_id_is_kept(self):
        _l, stored, _i = T._ai_launch_pair("claude", "/p", [], continue_now=False,
                                           resume_later=True, new_sid=A, tmux=True)
        row = self.row(stored, B)
        argv = self.follow(row)
        self.assertIn(f"--resume {B}", T._ai_tmux_unwrap(argv)[0][2])
        self.assertNotIn(A, " ".join(argv))
        self.assertEqual(T._ai_tmux_unwrap(argv)[1], T._ai_tmux_unwrap(stored)[1])  # same tmux
        self.assertEqual(self.saved, [1])

    def test_same_id_changes_nothing(self):
        _l, stored, _i = pair(resume_id=A)
        self.assertEqual(self.follow(self.row(stored, A)), stored)
        self.assertEqual(self.saved, [])

    def test_a_start_fresh_tab_stays_fresh(self):
        plain = T._ai_argv("claude", "/p", [])
        self.assertEqual(self.follow(self.row(plain, B)), plain)

    def test_a_tab_that_should_resume_starts_following(self):
        plain = T._ai_argv("claude", "/p", [])
        self.assertIn(f"--resume {B}", self.follow(self.row(plain, B, hunt=True))[2])

    def test_bypass_is_kept(self):
        stored = T._ai_argv("claude", "/p", [f"--resume {A}"], bypass=True)
        self.assertIn("--dangerously-skip-permissions", self.follow(self.row(stored, B))[2])

    def test_no_hook_state_changes_nothing(self):
        stored = T._ai_argv("claude", "/p", [f"--resume {A}"])
        self.assertEqual(self.follow(self.row(stored, None)), stored)


class TestOpenElsewhere(unittest.TestCase):
    """+AI warns before opening a session another terminal has open."""

    def check(self, active):
        app = types.SimpleNamespace(_claude_active_sessions=lambda: active)
        return T._ai_session_open_elsewhere(app, A)

    def test_open_in_a_terminal(self):
        self.assertEqual(self.check([{"sessionId": A, "kind": "interactive", "pid": 7}])["pid"], 7)

    def test_background_is_attached_not_warned(self):
        self.assertIsNone(self.check([{"sessionId": A, "kind": "background"}]))

    def test_not_open(self):
        self.assertIsNone(self.check([{"sessionId": B, "kind": "interactive"}]))
