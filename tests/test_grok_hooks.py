#!/usr/bin/env python3
"""Grok AI tabs report their status through Grok hooks."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import grok_status_hook as hook  # noqa: E402
import tabit  # noqa: E402

SCRIPT = os.path.join(ROOT, "grok_status_hook.py")


class TestGrokHookFile(unittest.TestCase):
    """The file Grok loads, and the command it runs."""

    def test_events_and_timeouts(self):
        got = tabit._grok_hooks_settings("/run/user/1/tabit-agent", SCRIPT)["hooks"]
        self.assertEqual(got["SessionStart"][0]["matcher"], "new|load")
        self.assertEqual(got["Stop"][0]["hooks"][0]["timeout"], 10)
        notes = [e.get("matcher") for e in got["Notification"]]
        self.assertEqual(notes, ["idle_prompt", "permission_prompt"])
        for name in ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                     "PostToolUseFailure", "Stop", "StopFailure", "StopCancelled"):
            self.assertIn(name, got)
        cmd = got["Stop"][0]["hooks"][0]["command"]
        self.assertIn(SCRIPT, cmd)
        self.assertTrue(cmd.endswith("|| exit 0"))

    def test_grok_argv_gains_no_flag(self):
        argv = tabit.Tabit._ai_argv("grok", "/tmp/x", ["--resume abc"])
        self.assertNotIn("--settings", argv[2])
        self.assertIn("--minimal", argv[2])

    def test_ensure_writes_only_its_own_file(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "hooks", "tabit-status.json")
        other = os.path.join(d.name, "hooks", "from-claude.json")
        os.makedirs(os.path.dirname(path))
        with open(other, "w") as handle:
            handle.write('{"hooks": {}}')
        old = (tabit.GROK_HOOKS_FILE, tabit.GROK_HOOK_SCRIPT, tabit.AGENT_HOOK_DIR)
        tabit.GROK_HOOKS_FILE = path
        tabit.GROK_HOOK_SCRIPT = SCRIPT
        tabit.AGENT_HOOK_DIR = os.path.join(d.name, "state")
        self.addCleanup(setattr, tabit, "GROK_HOOKS_FILE", old[0])
        self.addCleanup(setattr, tabit, "GROK_HOOK_SCRIPT", old[1])
        self.addCleanup(setattr, tabit, "AGENT_HOOK_DIR", old[2])
        tabit._ensure_grok_hooks()
        with open(path) as handle:
            got = json.load(handle)
        self.assertEqual(got, tabit._grok_hooks_settings(
            tabit.AGENT_HOOK_DIR, SCRIPT))
        with open(other) as handle:
            self.assertEqual(handle.read(), '{"hooks": {}}')
        tabit._ensure_grok_hooks()
        with open(path) as handle:
            self.assertEqual(json.load(handle), got)


class TestGrokHookCommand(unittest.TestCase):
    """The command writes the state tabit reads, and never fails the turn."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        self.tx = os.path.join(self.dir, "s.jsonl")
        with open(self.tx, "w") as handle:
            handle.write("x" * 10)
        self.cmd = tabit._grok_hook_command(self.dir, SCRIPT)

    def run_hook(self, payload, env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        e.update(env or {"TABIT_AGENT": "k1"})
        r = subprocess.run(
            ["/bin/sh", "-c", self.cmd],
            input=json.dumps(payload, separators=(",", ":")),
            text=True, env=e, timeout=10, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def state(self, key="k1"):
        with open(os.path.join(self.dir, key)) as handle:
            return handle.read().split("\n")

    def test_prompt_and_tool_are_working(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit",
                       "transcript_path": self.tx, "promptId": "p1"})
        self.assertEqual(self.state()[0], "working")
        self.assertEqual(self.state()[1], self.tx)
        self.assertEqual(self.state()[2], "10")
        self.assertEqual(self.state()[4], "p1")
        self.run_hook({"hook_event_name": "PreToolUse", "transcript_path": self.tx,
                       "promptId": "p1"})
        self.assertEqual(self.state()[0], "working")

    def test_permission_is_blocked_and_stop_is_idle(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p1",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "Notification",
                       "notification_type": "permission_prompt", "promptId": "p1",
                       "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "blocked")
        self.run_hook({"hook_event_name": "Stop", "promptId": "p1",
                       "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "idle")

    def test_a_running_background_task_is_background(self):
        for kind in ("subagent", "shell", "monitor"):
            self.run_hook({"hook_event_name": "Stop", "promptId": "p1",
                           "transcript_path": self.tx,
                           "backgroundTasks": [{"id": "t", "type": kind,
                                                "status": "running"}]})
            self.assertEqual(self.state()[0], "background", kind)

    def test_a_schedule_alone_is_idle(self):
        self.run_hook({"hook_event_name": "Stop", "promptId": "p1",
                       "transcript_path": self.tx,
                       "backgroundTasks": [],
                       "sessionCrons": [{"id": "c", "schedule": "every 5 minutes"}]})
        self.assertEqual(self.state()[0], "idle")

    def test_old_cancel_does_not_cover_the_new_turn(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p1",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p2",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "StopCancelled", "promptId": "p1",
                       "reason": "user_interrupt", "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "working")
        self.assertEqual(self.state()[4], "p2")

    def test_a_cancel_with_no_earlier_prompt_is_idle(self):
        self.run_hook({"hook_event_name": "StopCancelled", "promptId": "bash1",
                       "reason": "user_interrupt", "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "idle")

    def test_idle_prompt_settles(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p1",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "Notification",
                       "notification_type": "idle_prompt", "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "idle")

    def test_a_child_agent_does_not_change_the_tab(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p1",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "Stop", "promptId": "child",
                       "subagentType": "explore", "transcript_path": self.tx})
        self.assertEqual(self.state()[0], "working")
        self.assertEqual(self.state()[4], "p1")

    def test_session_start_is_idle(self):
        self.run_hook({"hook_event_name": "UserPromptSubmit", "promptId": "p1",
                       "transcript_path": self.tx})
        self.run_hook({"hook_event_name": "SessionStart", "transcript_path": self.tx})
        self.assertEqual(self.state()[:5], ["idle", self.tx, "10", "", ""])

    def test_no_key_and_bad_json_write_nothing(self):
        self.run_hook({"hook_event_name": "Stop"}, env={"TABIT_AGENT": ""})
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TABIT_AGENT"] = "k1"
        r = subprocess.run(
            ["/bin/sh", "-c", self.cmd], input="not json", text=True, timeout=10,
            env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(os.listdir(self.dir), ["s.jsonl"])

    def test_a_missing_script_still_exits_0(self):
        cmd = tabit._grok_hook_command(self.dir, os.path.join(self.dir, "missing.py"))
        r = subprocess.run(
            ["/bin/sh", "-c", cmd], input="{}", text=True, timeout=10,
            capture_output=True,
            env={"TABIT_AGENT": "k1", "PATH": os.environ.get("PATH", "")})
        self.assertEqual(r.returncode, 0)


class TestDecide(unittest.TestCase):
    """The same choices, without the shell."""

    def test_snake_case_event_name(self):
        name = hook.event_name({"hookEventName": "user_prompt_submit"})
        self.assertEqual(name, "UserPromptSubmit")
        self.assertEqual(hook.decide(name, {"promptId": "p"}, ""), ("working", "p"))

    def test_task_complete_is_ignored(self):
        self.assertIsNone(hook.decide(
            "Notification", {"notification_type": "task_complete"}, "p1"))


if __name__ == "__main__":
    unittest.main()
