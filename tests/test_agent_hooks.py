#!/usr/bin/env python3
"""Claude AI tabs report their status through Claude Code hooks."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
HOOKS = ["--settings", tabit.CLAUDE_HOOKS_FILE]


class TestHookArgv(unittest.TestCase):
    """Every claude in the command loads tabit's hooks, and a rebuild keeps one copy."""

    def test_claude_gets_the_hooks_file(self):
        argv = T._ai_argv("claude", "/tmp/x", ["--resume abc"])
        self.assertEqual(argv[2].count("--settings"), 2)   # the try and the exec
        self.assertIn(tabit.CLAUDE_HOOKS_FILE, argv[2])

    def test_other_cli_does_not(self):
        argv = T._ai_argv("codex", "/tmp/x", ["resume --last"])
        self.assertNotIn("--settings", argv[2])

    def test_round_trip_keeps_one_copy(self):
        for bypass in (False, True):
            argv = T._ai_argv("claude", "/tmp/x", ["--resume abc"], bypass=bypass)
            tries = T._ai_tries_of(argv[2])
            self.assertEqual(tries, ["--resume abc"])
            self.assertEqual(T._ai_argv("claude", "/tmp/x", tries, bypass=bypass), argv)

    def test_tmux_round_trip(self):
        inner = T._ai_argv("claude", "/tmp/x", [])
        argv = T._ai_tmux_argv(inner, "ai-claude-x-123456")
        self.assertEqual(T._ai_cli_path_of(argv), ("claude", "/tmp/x"))
        again = T._ai_argv_with_session(argv, "abc")
        self.assertEqual(again, T._ai_argv_with_session(again, "abc"))
        self.assertEqual(T._ai_tmux_unwrap(again)[0][2].count("--settings"), 2)


class TestHookCommand(unittest.TestCase):
    """The shell command a hook runs writes the state tabit reads."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.tx = os.path.join(self.dir, "s.jsonl")
        with open(self.tx, "w") as f:
            f.write("x" * 10)
        self.hooks = tabit._claude_hooks_settings(self.dir)["hooks"]

    def run_hook(self, event, payload, env=None):
        cmd = self.hooks[event][0]["hooks"][0]["command"]
        e = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        e.update(env or {"TABIT_AGENT": "k1"})
        # dash where it is there: the /bin/sh of most systems, and stricter
        r = subprocess.run([shutil.which("dash") or "/bin/sh", "-c", cmd], input=json.dumps(
            payload, separators=(",", ":")), text=True, env=e, timeout=10)
        self.assertEqual(r.returncode, 0)

    def state(self, key="k1"):
        with open(os.path.join(self.dir, key)) as f:
            return f.read().split("\n")

    def test_tool_use_is_working(self):
        self.run_hook("PreToolUse", {"transcript_path": self.tx, "tool_name": "Bash"})
        self.assertEqual(self.state()[:3], ["working", self.tx, "10"])

    def test_a_question_is_blocked(self):
        for tool in ("AskUserQuestion", "ExitPlanMode"):
            self.run_hook("PreToolUse", {"transcript_path": self.tx, "tool_name": tool})
            self.assertEqual(self.state()[0], "blocked", tool)

    def test_compaction_is_not_the_end_of_a_turn(self):
        self.assertEqual(self.hooks["SessionStart"][0]["matcher"],
                         "startup|resume|clear")

    def test_events(self):
        for event, want in (("SessionStart", "idle"), ("UserPromptSubmit", "working"),
                            ("PermissionRequest", "blocked"), ("PostToolUse", "working"),
                            ("Stop", "idle"), ("StopFailure", "idle")):
            self.run_hook(event, {"transcript_path": self.tx})
            self.assertEqual(self.state()[0], want, event)

    # Claude Code 2.1.301: Stop lists the background tasks still running,
    # and every event from a sub-agent carries its agent_id
    RUNNING = [{"id": "a1", "type": "subagent", "status": "running",
                "description": "x", "agent_type": "general-purpose"}]

    def test_stop_with_a_subagent_running_is_background(self):
        for event in ("Stop", "StopFailure"):
            self.run_hook(event, {"transcript_path": self.tx,
                                  "background_tasks": self.RUNNING})
            self.assertEqual(self.state()[0], "background", event)

    def test_stop_with_no_subagent_running_is_idle(self):
        shell = [{"id": "b1", "type": "shell", "status": "running"}]
        for tasks in ([], shell):
            self.run_hook("Stop", {"transcript_path": self.tx, "background_tasks": tasks})
            self.assertEqual(self.state()[0], "idle", tasks)

    def test_subagent_tool_use_leaves_the_state(self):
        self.run_hook("Stop", {"transcript_path": self.tx, "background_tasks": self.RUNNING})
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            self.run_hook(event, {"transcript_path": self.tx, "tool_name": "Bash",
                                  "agent_id": "a1"})
            self.assertEqual(self.state()[0], "background", event)

    def test_subagent_asks_then_goes_back(self):
        for before, event, payload in (
                ("background", "Stop", {"background_tasks": self.RUNNING}),
                ("working", "UserPromptSubmit", {})):
            self.run_hook(event, {"transcript_path": self.tx, **payload})
            self.run_hook("PermissionRequest", {"transcript_path": self.tx, "agent_id": "a1"})
            self.assertEqual(self.state()[0], "blocked", before)
            self.run_hook("PostToolUse", {"transcript_path": self.tx, "agent_id": "a1"})
            self.assertEqual(self.state()[0], before)

    def test_another_subagent_does_not_answer(self):
        self.run_hook("Stop", {"transcript_path": self.tx, "background_tasks": self.RUNNING})
        self.run_hook("PermissionRequest", {"transcript_path": self.tx, "agent_id": "a1"})
        self.run_hook("PostToolUse", {"transcript_path": self.tx, "agent_id": "a2"})
        self.assertEqual(self.state()[0], "blocked")
        self.run_hook("PostToolUse", {"transcript_path": self.tx, "agent_id": "a1"})
        self.assertEqual(self.state()[0], "background")

    def test_subagent_asks_with_no_state_yet(self):
        self.run_hook("PermissionRequest", {"transcript_path": self.tx, "agent_id": "a1"})
        self.assertEqual(self.state()[:5:3], ["blocked", "background"])

    def test_subagent_does_not_undo_the_main_agent_asking(self):
        self.run_hook("PermissionRequest", {"transcript_path": self.tx})
        self.run_hook("PostToolUse", {"transcript_path": self.tx, "agent_id": "a1"})
        self.assertEqual(self.state()[0], "blocked")

    def ask(self, agent=None):
        self.run_hook("PermissionRequest", {"transcript_path": self.tx,
                                            **({"agent_id": agent} if agent else {})})

    def tool_end(self, agent=None):
        self.run_hook("PostToolUse", {"transcript_path": self.tx,
                                      **({"agent_id": agent} if agent else {})})

    def test_stop_keeps_a_subagent_asking(self):
        self.run_hook("UserPromptSubmit", {"transcript_path": self.tx})
        self.ask("a1")
        self.run_hook("Stop", {"transcript_path": self.tx, "background_tasks": self.RUNNING})
        self.assertEqual(self.state()[0:5:3] + [self.state()[4]],
                         ["blocked", "background", "a1"])
        self.tool_end("a1")
        self.assertEqual(self.state()[0], "background")

    def test_two_subagents_ask_at_once(self):
        self.run_hook("Stop", {"transcript_path": self.tx, "background_tasks": self.RUNNING})
        self.ask("a1")
        self.ask("a2")
        self.assertEqual(self.state()[4], "a1 a2")
        self.tool_end("a2")
        self.assertEqual((self.state()[0], self.state()[4]), ("blocked", "a1"))
        self.tool_end("a1")
        self.assertEqual(self.state()[0], "background")

    def test_main_agent_works_on_while_a_subagent_asks(self):
        self.run_hook("UserPromptSubmit", {"transcript_path": self.tx})
        self.ask("a1")
        self.tool_end()                 # the main agent's own tool
        self.assertEqual(self.state()[0:5:3], ["blocked", "working"])
        self.tool_end("a1")
        self.assertEqual(self.state()[0], "working")

    def test_main_and_subagent_both_ask(self):
        self.run_hook("UserPromptSubmit", {"transcript_path": self.tx})
        self.ask()                      # the main agent asks first
        self.ask("a1")
        self.tool_end("a1")             # the sub-agent is answered first
        self.assertEqual(self.state()[0], "blocked")   # the main one still asks
        self.tool_end()
        self.assertEqual(self.state()[0], "working")

    def test_subagent_then_main_agent_ask(self):
        self.run_hook("UserPromptSubmit", {"transcript_path": self.tx})
        self.ask("a1")
        self.ask()                      # the main agent asks after it
        self.tool_end("a1")
        self.assertEqual(self.state()[0], "blocked")   # the main one still asks
        self.tool_end()
        self.assertEqual(self.state()[0], "working")

    def test_a_typed_prompt_starts_afresh(self):
        self.ask("a1")                  # denied: no tool end ever comes
        self.run_hook("UserPromptSubmit", {"transcript_path": self.tx})
        self.assertEqual(self.state()[0:5:3] + [self.state()[4]], ["working", "", ""])

    def test_no_key_writes_nothing(self):
        self.run_hook("Stop", {"transcript_path": self.tx}, env={"TABIT_AGENT": ""})
        self.assertEqual(os.listdir(self.dir), ["s.jsonl"])


class TestHookRaw(unittest.TestCase):
    """Reading a state file, and seeing an Esc or a No in the transcript."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        old = tabit.AGENT_HOOK_DIR
        tabit.AGENT_HOOK_DIR = self.dir
        self.addCleanup(setattr, tabit, "AGENT_HOOK_DIR", old)
        self.tx = os.path.join(self.dir, "s.jsonl")
        self.row = types.SimpleNamespace(_agent_key="k1", argv=["claude"])

    def write(self, state, before=b"", after=b""):
        with open(self.tx, "wb") as f:
            f.write(before)
        with open(os.path.join(self.dir, "k1"), "w") as f:
            f.write(f"{state}\n{self.tx}\n{len(before)}\n")
        with open(self.tx, "ab") as f:
            f.write(after)

    def raw(self):
        return T._agent_hook_raw(self.row)

    def test_no_file_means_no_hook(self):
        self.assertIsNone(self.raw())

    def test_plain_states(self):
        for s in ("working", "blocked", "idle", "background"):
            self.write(s)
            self.assertEqual(self.raw(), s)

    def test_esc_after_background_is_idle(self):
        # the sub-agents are done, claude went on and was stopped: no hook
        marker = b'{"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user]"}]}}\n'
        self.write("background", after=marker)
        self.assertEqual(self.raw(), "idle")

    def test_esc_before_background_does_not_count(self):
        marker = b'{"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user]"}]}}\n'
        self.write("background", before=marker)
        self.assertEqual(self.raw(), "background")

    def test_esc_after_the_event_is_idle(self):
        marker = b'{"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user]"}]}}\n'
        self.write("working", after=marker)
        self.assertEqual(self.raw(), "idle")
        self.write("blocked", after=marker.replace(b"user]", b"user for tool use]"))
        self.assertEqual(self.raw(), "idle")

    def test_old_esc_before_the_event_does_not_count(self):
        marker = b'{"type":"user","message":{"content":[{"type":"text","text":"[Request interrupted by user]"}]}}\n'
        self.write("working", before=marker, after=b'{"type":"assistant"}\n')
        self.assertEqual(self.raw(), "working")

    def test_quoted_marker_does_not_count(self):
        quoted = b'{"type":"user","content":"\\"text\\":\\"[Request interrupted by user]\\""}\n'
        self.write("working", after=quoted)
        self.assertEqual(self.raw(), "working")

    def test_tmux_tab_key_is_its_session(self):
        inner = T._ai_argv("claude", "/tmp/x", [])
        row = types.SimpleNamespace(argv=T._ai_tmux_argv(inner, "ai-claude-x-1"))
        self.assertEqual(T._agent_hook_key(row), "ai-claude-x-1")


class TestHookBeatsScreen(unittest.TestCase):
    """With a hook state the screen only decides an Esc that left no trace."""

    # Claude Code v2.1.300 screens, while thinking and after an early Esc
    FOOT = ("─" * 40 + "\n❯ {}\n" + "─" * 40 + "\n"
            "  ◆ Haiku 5.5 │ ░░░░░░░░░░ 0% 1M │ $0.00\n  w2\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n")
    BUSY = "❯ Write an essay.\n· Simmering… (2s · thinking)\n" + FOOT.format("")
    # past a minute the timer reads "1m 5s"
    BUSY_LONG = ("❯ Write an essay.\n✻ Moonwalking… (1m 5s · ↓ 2.2k tokens · thinking)\n"
                 + FOOT.format(""))
    # claude asks: the busy line is not on screen
    ASKING = ("● Bash(mkdir newdir)\n  ⎿  Waiting…\n Do you want to proceed?\n"
              " ❯ 1. Yes\n   2. No\n")
    QUIET = "\n" + FOOT.format("Write an essay.")
    # Claude Code 2.1.301: the permission dialog, as a sub-agent's shows too
    DIALOG = ("● Bash(touch /tmp/t)\n  ⎿  Waiting…\n\n Bash command\n  touch /tmp/t\n"
              " Do you want to proceed?\n ❯ 1. Yes\n   2. Yes, and always allow access"
              " to /tmp from this project\n   3. No\n\n Esc to cancel · Tab to amend\n")
    # idle screens a loose "blocked" pattern took for a question
    NOT_ASKING = (
        "\n" + FOOT.replace("bypass permissions on", "accept edits on").format(""),
        "● Two agents still run. Do you want to merge once they finish?\n\n"
        + FOOT.format(""))
    # v2.1.301 drops the busy line while the reply text streams in
    STREAMING = ("● The Cat\n\n  One\n\n  At the end of the lane, an old bookshop.\n\n\n"
                 + FOOT.format(""))

    def run_update(self, state, screen, esc_age=None, event_age=10.0, pty_age=None,
                   scrolled=False, back="", busy_age=None):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        old = tabit.AGENT_HOOK_DIR
        tabit.AGENT_HOOK_DIR = d.name
        self.addCleanup(setattr, tabit, "AGENT_HOOK_DIR", old)
        path = os.path.join(d.name, "k1")
        with open(path, "w") as f:
            f.write(f"{state}\n\n0\n{back}\n")
        t = time.time() - event_age
        os.utime(path, (t, t))
        import agent_status as as_
        got = []
        row = types.SimpleNamespace(
            icon_name=tabit.ICON_AI, _agent_key="k1", argv=["claude"], dead=False,
            term=object(), agent_status="idle", _agent_episode=as_.Episode(
                last_pty=0.0 if pty_age is None else time.time() - pty_age),
            _agent_display=as_.Display(ui="idle"), get_parent=lambda: object(),
            # Esc or Ctrl+C pressed in tabit that long ago
            _agent_esc_at=None if esc_age is None else time.time() - esc_age,
            _agent_hook_busy_at=None if busy_age is None else time.time() - busy_age,
            _agent_last_pty_activity=0.0 if pty_age is None else time.time() - pty_age)
        self.row = row
        read = []

        def tail(*_a, **_k):
            read.append(1)
            return screen
        app = types.SimpleNamespace(
            _agent_status_mod=lambda: as_, _term_tail_text=tail,
            listbox=types.SimpleNamespace(get_selected_row=lambda: None),
            is_active=lambda: True, _AGENT_TAIL_LINES=40, _AGENT_READY_STABLE_S=1.0,
            _AGENT_HOOK_QUIET_S=T._AGENT_HOOK_QUIET_S,
            _agent_hook_raw=T._agent_hook_raw, _agent_hook_scrolled=lambda r: scrolled,
            _set_agent_status=lambda r, s: got.append(s))
        app._agent_hook_still_busy = lambda *a: T._agent_hook_still_busy(app, *a)
        app._agent_hook_screen_says = lambda *a: T._agent_hook_screen_says(app, *a)
        app._ai_follow_session = lambda r: None
        T._update_agent_status(app, row)
        return got[-1], bool(read)

    def test_idle_does_not_read_the_screen(self):
        self.assertEqual(self.run_update("idle", self.BUSY), ("idle", False))

    def test_background_with_nobody_asking(self):
        for screen in (self.QUIET,) + self.NOT_ASKING:
            self.assertEqual(self.run_update("background", screen)[0], "background", screen)

    def test_background_while_a_subagent_asks_is_blocked(self):
        # Stop does not know a sub-agent is asking; the screen does
        self.assertEqual(self.run_update("background", self.DIALOG)[0], "blocked")

    def test_subagent_answered_goes_back(self):
        # approved or denied, the question is gone from the screen
        for screen in (self.QUIET,) + self.NOT_ASKING:
            self.assertEqual(self.run_update(
                "blocked", screen, back="background")[0], "background", screen)

    def test_subagent_still_asking_stays_blocked(self):
        self.assertEqual(self.run_update(
            "blocked", self.DIALOG, back="background")[0], "blocked")

    def test_main_agent_asking_needs_no_question_on_screen(self):
        # its own question ends with a hook; nothing to go back to
        self.assertEqual(self.run_update("blocked", self.QUIET)[0], "blocked")

    def test_blocked_while_claude_asks(self):
        self.assertEqual(self.run_update("blocked", self.ASKING)[0], "blocked")

    def test_blocked_then_approved_tool_runs(self):
        # no hook runs between the approval and the end of the tool
        self.assertEqual(self.run_update("blocked", self.BUSY)[0], "working")

    def test_working_hook_over_idle_looking_screen(self):
        # the screen rules read this busy screen as idle; the hook knows
        self.assertEqual(self.run_update("working", self.BUSY, esc_age=5)[0], "working")

    def test_turn_past_a_minute_stays_working(self):
        self.assertEqual(self.run_update("working", self.BUSY_LONG, esc_age=5)[0],
                         "working")

    def test_a_stalled_reply_with_no_esc_stays_working(self):
        # a reply that streams in shows no busy line; a pause in it is not
        # the end unless someone pressed Esc
        for screen in (self.STREAMING, self.QUIET):
            self.assertEqual(self.run_update("working", screen, event_age=60)[0],
                             "working")

    def test_esc_then_quiet_is_idle(self):
        self.assertEqual(self.run_update("working", self.QUIET, esc_age=5)[0], "idle")

    def test_esc_just_now_is_still_working(self):
        self.assertEqual(self.run_update("working", self.QUIET, esc_age=1)[0], "working")

    def test_esc_before_the_last_event_does_not_count(self):
        self.assertEqual(self.run_update(
            "working", self.QUIET, esc_age=5, event_age=1)[0], "working")

    def test_typing_after_an_esc_does_not_hold_working(self):
        # the prompt claude put back after an early Esc, being typed into
        self.assertEqual(self.run_update(
            "working", self.QUIET, esc_age=5, pty_age=0.2)[0], "idle")

    def test_question_on_screen_beats_working(self):
        # another tool of the turn ended after claude asked: its hook
        # wrote working, but the question is still up
        self.assertEqual(self.run_update("working", self.DIALOG)[0], "blocked")

    def test_busy_line_after_an_esc_holds_working(self):
        # the key did not stop claude; it waits for the busy line to go
        self.assertEqual(self.run_update("working", self.BUSY, esc_age=5)[0], "working")
        self.assertEqual(self.run_update(
            "working", self.QUIET, esc_age=5, busy_age=1)[0], "working")


class TestScreenSaysOncePerEvent(unittest.TestCase):
    """tmux is asked once per hook event, not on every look."""

    def test_tmux_asked_once(self):
        calls = []
        row = types.SimpleNamespace(_agent_hook_mtime=5.0)
        app = types.SimpleNamespace(_agent_hook_scrolled=lambda r: calls.append(1) or False)
        for _ in range(5):
            self.assertTrue(T._agent_hook_screen_says(app, row, "answered", True))
        self.assertEqual(len(calls), 1)
        row._agent_hook_mtime = 6.0     # a new hook event asks again
        T._agent_hook_screen_says(app, row, "answered", True)
        self.assertEqual(len(calls), 2)

    def test_screen_still_decides(self):
        row = types.SimpleNamespace(_agent_hook_mtime=5.0)
        app = types.SimpleNamespace(_agent_hook_scrolled=lambda r: False)
        T._agent_hook_screen_says(app, row, "answered", True)
        self.assertFalse(T._agent_hook_screen_says(app, row, "answered", False))

    def test_scrolled_up_says_nothing(self):
        row = types.SimpleNamespace(_agent_hook_mtime=5.0)
        app = types.SimpleNamespace(_agent_hook_scrolled=lambda r: True)
        self.assertFalse(T._agent_hook_screen_says(app, row, "asking", True))


class TestInterruptKey(unittest.TestCase):
    """tabit sees Esc and Ctrl+C itself: claude runs no hook for them."""

    def press(self, keyval, ctrl=False, scrolled=False, key="k1", shift=False, alt=False):
        row = types.SimpleNamespace(_agent_key=key, argv=["claude"])
        app = types.SimpleNamespace(_agent_hook_key=T._agent_hook_key,
                                    _agent_hook_scrolled=lambda r: scrolled,
                                    _key_in_first_layout=T._key_in_first_layout)
        app._agent_note_interrupt = lambda r: T._agent_note_interrupt(app, r)
        mods = tabit.Gdk.ModifierType(0)
        if ctrl:
            mods |= tabit.Gdk.ModifierType.CONTROL_MASK
        if shift:
            mods |= tabit.Gdk.ModifierType.SHIFT_MASK
        if alt:
            mods |= tabit.Gdk.ModifierType.MOD1_MASK
        ev = types.SimpleNamespace(keyval=keyval, state=mods)
        self.assertFalse(T._on_ai_term_key(app, None, ev, row))  # the key goes on
        return getattr(row, "_agent_esc_at", None)

    def test_esc_and_ctrl_c(self):
        self.assertIsNotNone(self.press(tabit.Gdk.KEY_Escape))
        self.assertIsNotNone(self.press(tabit.Gdk.KEY_c, ctrl=True))

    def test_other_keys(self):
        self.assertIsNone(self.press(tabit.Gdk.KEY_c))
        self.assertIsNone(self.press(tabit.Gdk.KEY_Return))

    def test_ctrl_c_on_a_cyrillic_layout(self):
        # the C key gives Cyrillic es there; the first layout says c
        km = tabit.Gdk.Keymap.get_default()
        ok, keys = km.get_entries_for_keyval(tabit.Gdk.KEY_c) if km else (False, [])
        if not ok:
            self.skipTest("no keymap")
        ev = types.SimpleNamespace(keyval=tabit.Gdk.KEY_Cyrillic_es,
                                   hardware_keycode=keys[0].keycode)
        self.assertEqual(T._key_in_first_layout(ev), tabit.Gdk.KEY_c)

    def test_alt_is_not_an_interrupt(self):
        self.assertIsNone(self.press(tabit.Gdk.KEY_Escape, alt=True))

    def test_copy_is_not_an_interrupt(self):
        # Ctrl+Shift+C copies; claude never gets it
        for kv in (tabit.Gdk.KEY_c, tabit.Gdk.KEY_C):
            self.assertIsNone(self.press(kv, ctrl=True, shift=True))

    def test_scrolled_up_tmux_takes_the_key(self):
        self.assertIsNone(self.press(tabit.Gdk.KEY_Escape, scrolled=True))


class TestTmuxScrolled(unittest.TestCase):
    """tmux itself says whether a tab is scrolled up (copy mode)."""

    def setUp(self):
        # a tmux server of its own, never the one these tests may run in
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TMUX_TMPDIR"] = d.name
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], capture_output=True)
        subprocess.run(["tmux", "new-session", "-d", "-s", "ai-t-1", "sleep 60"], check=True)
        inner = T._ai_argv("claude", "/tmp/x", [])
        self.row = types.SimpleNamespace(argv=T._ai_tmux_argv(inner, "ai-t-1"))

    def test_at_the_bottom(self):
        self.assertFalse(T._agent_hook_scrolled(self.row))

    def test_scrolled_up(self):
        subprocess.run(["tmux", "copy-mode", "-t", "=ai-t-1:"], check=True)
        self.assertTrue(T._agent_hook_scrolled(self.row))

    def test_plain_tab_is_never_scrolled(self):
        self.assertFalse(T._agent_hook_scrolled(types.SimpleNamespace(argv=["claude"])))


if __name__ == "__main__":
    unittest.main()
