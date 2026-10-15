#!/usr/bin/env python3
"""Claude AI tabs report their status through Claude Code hooks."""
import json
import os
import subprocess
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
        r = subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps(
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
        for s in ("working", "blocked", "idle"):
            self.write(s)
            self.assertEqual(self.raw(), s)

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

    def run_update(self, state, screen, quiet_for=0.0, event_age=10.0):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        old = tabit.AGENT_HOOK_DIR
        tabit.AGENT_HOOK_DIR = d.name
        self.addCleanup(setattr, tabit, "AGENT_HOOK_DIR", old)
        path = os.path.join(d.name, "k1")
        with open(path, "w") as f:
            f.write(f"{state}\n\n0\n")
        t = time.time() - event_age
        os.utime(path, (t, t))
        import agent_status as as_
        got = []
        row = types.SimpleNamespace(
            icon_name=tabit.ICON_AI, _agent_key="k1", argv=["claude"], dead=False,
            term=object(), agent_status="idle", _agent_episode=as_.Episode(),
            _agent_display=as_.Display(ui="idle"), get_parent=lambda: object(),
            _agent_hook_quiet=(time.time() - quiet_for) if quiet_for else None)
        read = []

        def tail(*_a, **_k):
            read.append(1)
            return screen
        app = types.SimpleNamespace(
            _agent_status_mod=lambda: as_, _term_tail_text=tail,
            listbox=types.SimpleNamespace(get_selected_row=lambda: None),
            is_active=lambda: True, _AGENT_TAIL_LINES=40, _AGENT_READY_STABLE_S=1.0,
            _AGENT_HOOK_QUIET_S=T._AGENT_HOOK_QUIET_S,
            _agent_hook_raw=T._agent_hook_raw,
            _set_agent_status=lambda r, s: got.append(s))
        app._agent_hook_still_busy = lambda *a: T._agent_hook_still_busy(app, *a)
        T._update_agent_status(app, row)
        return got[-1], bool(read)

    def test_working_hook_over_idle_looking_screen(self):
        # the screen rules read this busy screen as idle; the hook knows
        self.assertEqual(self.run_update("working", self.BUSY, quiet_for=5)[0], "working")

    def test_idle_does_not_read_the_screen(self):
        self.assertEqual(self.run_update("idle", self.BUSY), ("idle", False))

    def test_blocked_while_claude_asks(self):
        self.assertEqual(self.run_update("blocked", self.ASKING)[0], "blocked")

    def test_blocked_then_approved_tool_runs(self):
        # no hook runs between the approval and the end of the tool
        self.assertEqual(self.run_update("blocked", self.BUSY)[0], "working")

    def test_turn_past_a_minute_stays_working(self):
        self.assertEqual(self.run_update("working", self.BUSY_LONG, quiet_for=5)[0],
                         "working")

    def test_busy_line_gone_for_a_while_is_idle(self):
        self.assertEqual(self.run_update("working", self.QUIET, quiet_for=5)[0], "idle")

    def test_busy_line_just_gone_is_still_working(self):
        self.assertEqual(self.run_update("working", self.QUIET, quiet_for=1)[0], "working")

    def test_a_fresh_hook_event_restarts_the_wait(self):
        self.assertEqual(self.run_update(
            "working", self.QUIET, quiet_for=5, event_age=1)[0], "working")


if __name__ == "__main__":
    unittest.main()
