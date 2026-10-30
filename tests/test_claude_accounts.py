#!/usr/bin/env python3
"""Claude accounts: one wrapper per account, picked per +AI tab."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402

T = tabit.Tabit
A = "aaaaaaaa-1111-2222-3333-444444444444"


class AccountDirs(unittest.TestCase):
    """A default ~/.claude and an accounts folder of their own."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = d.name
        self.home = os.path.join(d.name, "dot-claude")
        os.makedirs(os.path.join(self.home, "projects", "-p"))
        os.makedirs(os.path.join(self.home, "skills"))
        for name in ("settings.json", "CLAUDE.md", ".credentials.json"):
            with open(os.path.join(self.home, name), "w") as f:
                f.write("{}")
        self.json = os.path.join(d.name, "dot-claude.json")
        with open(self.json, "w") as f:
            json.dump({"hasCompletedOnboarding": True, "lastOnboardingVersion": "2.1",
                       "oauthAccount": {"email": "me@x"}, "userID": "u1"}, f)
        for name, value in (("CLAUDE_ACCOUNTS_DIR", os.path.join(d.name, "accounts")),
                            ("CLAUDE_DEFAULT_DIR", self.home),
                            ("CLAUDE_DEFAULT_JSON", self.json)):
            p = mock.patch.object(tabit, name, value)
            p.start()
            self.addCleanup(p.stop)

    def run_wrapper(self, cli):
        """Run the wrapper with a stand-in claude that reports its env."""
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake, exist_ok=True)
        with open(os.path.join(fake, "claude"), "w") as f:
            f.write('#!/bin/sh\necho "$CLAUDE_CONFIG_DIR|$*"\n')
        os.chmod(os.path.join(fake, "claude"), 0o755)
        env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"])
        return subprocess.run([cli, "--resume", A], env=env, capture_output=True,
                              text=True, check=True).stdout.strip()


class TestAddAndRemove(AccountDirs):

    def test_add_makes_a_claude_named_wrapper(self):
        cli = T._claude_account_add("work")
        self.assertEqual(os.path.basename(cli), "claude")
        self.assertTrue(os.access(cli, os.X_OK))
        self.assertTrue(T._ai_is_claude(cli))
        self.assertEqual(T._claude_account_of(cli), "work")
        self.assertIsNone(T._claude_account_of("claude"))
        self.assertEqual(T._claude_accounts(), [("work", cli)])

    def test_bad_or_taken_names_are_refused(self):
        T._claude_account_add("work")
        for bad in ("work", "", "a b", "../x", "Default", "x" * 40):
            with self.assertRaises(ValueError, msg=bad):
                T._claude_account_add(bad)

    def test_onboarding_is_seeded_but_not_the_login(self):
        cli = T._claude_account_add("work")
        cfg = os.path.join(os.path.dirname(os.path.dirname(cli)), "config")
        with open(os.path.join(cfg, ".claude.json")) as f:
            seed = json.load(f)
        self.assertEqual(seed, {"hasCompletedOnboarding": True,
                                "lastOnboardingVersion": "2.1"})

    def test_remove_leaves_the_shared_files_alone(self):
        cli = T._claude_account_add("work")
        self.run_wrapper(cli)
        T._claude_account_remove("work")
        self.assertEqual(T._claude_accounts(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.home, "projects", "-p")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "settings.json")))

class TestWrapper(AccountDirs):

    def test_it_runs_claude_with_its_own_config(self):
        cli = T._claude_account_add("work")
        cfg = os.path.join(os.path.dirname(os.path.dirname(cli)), "config")
        self.assertEqual(self.run_wrapper(cli), f"{cfg}|--resume {A}")

    def test_settings_and_sessions_are_shared_the_login_is_not(self):
        cli = T._claude_account_add("work")
        cfg = os.path.join(os.path.dirname(os.path.dirname(cli)), "config")
        # made after the account: still shared, on the next start
        os.makedirs(os.path.join(self.home, "agents"))
        self.run_wrapper(cli)
        for name in ("settings.json", "CLAUDE.md", "skills", "projects", "agents"):
            link = os.path.join(cfg, name)
            self.assertEqual(os.readlink(link), os.path.join(self.home, name), name)
        self.assertFalse(os.path.lexists(os.path.join(cfg, ".credentials.json")))

    def test_its_own_file_is_not_replaced(self):
        cli = T._claude_account_add("work")
        cfg = os.path.join(os.path.dirname(os.path.dirname(cli)), "config")
        with open(os.path.join(cfg, "settings.json"), "w") as f:
            f.write('{"own": 1}')
        self.run_wrapper(cli)
        self.assertFalse(os.path.islink(os.path.join(cfg, "settings.json")))


class TestTheAccountStaysWithTheTab(AccountDirs):
    """The account is the CLI path, so every argv helper keeps it."""

    def test_round_trip_through_the_argv_helpers(self):
        cli = T._claude_account_add("work")
        launch, stored, _icon = T._ai_launch_pair(
            cli, "/home/max/proj", ["--continue"], tmux=True,
            continue_now=True, resume_later=True)
        for argv in (launch, stored):
            self.assertEqual(T._ai_cli_path_of(argv), (cli, "/home/max/proj"))
        moved = T._ai_argv_with_session(stored, A)
        self.assertEqual(T._ai_cli_path_of(moved)[0], cli)
        self.assertIn(A, moved[2])
        self.assertEqual(T._ai_cli_path_of(T._ai_argv_plain(stored))[0], cli)

    def test_tmux_name_says_the_account(self):
        cli = T._claude_account_add("work")
        name = T._ai_tmux_session(cli, "/home/max/proj")
        self.assertTrue(name.startswith("ai-claude-work-proj-"), name)
        self.assertNotEqual(name, T._ai_tmux_session("claude", "/home/max/proj"))
        self.assertTrue(T._ai_tmux_session("claude", "/home/max/proj")
                        .startswith("ai-claude-proj-"))

    def test_running_sessions_are_asked_of_every_account(self):
        cli = T._claude_account_add("work")
        asked = []

        def run(argv, **_kw):
            asked.append(argv[0])
            out = [{"sessionId": A}] if argv[0] == cli else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")
        with mock.patch.object(tabit.subprocess, "run", run):
            got = T._claude_active_sessions()
        self.assertEqual(asked, ["claude", cli])
        self.assertEqual(got, [{"sessionId": A}])


class TestTheTabSaysItsAccount(AccountDirs):
    """An account tab's second line starts with @account, a default one not."""

    def test_new_tab_line(self):
        cli = T._claude_account_add("work")
        app = mock.Mock(_short_path=T._short_path)
        self.assertEqual(T._ai_sub(app, "/tmp/proj", cli), "@work · /tmp/proj")
        self.assertEqual(T._ai_sub(app, "/tmp/proj", "claude"), "/tmp/proj")
        self.assertEqual(T._ai_sub(app, "/tmp/proj"), "/tmp/proj")

    def test_a_saved_tab_gets_it_on_reopen(self):
        cli = T._claude_account_add("work")
        argv = T._ai_launch_pair(cli, "/tmp/proj", [], tmux=True)[1]
        self.assertEqual(T._ai_account_sub(argv, "/tmp/proj"), "@work · /tmp/proj")
        self.assertEqual(T._ai_account_sub(argv, "@work · /tmp/proj"), "@work · /tmp/proj")
        plain = T._ai_launch_pair("claude", "/tmp/proj", [], tmux=True)[1]
        self.assertEqual(T._ai_account_sub(plain, "/tmp/proj"), "/tmp/proj")
        self.assertEqual(T._ai_account_sub(["bash"], None), None)


class TestRemoteNewTab(AccountDirs):
    """The phone's New tab picks an account the way +AI does."""

    def app(self):
        app = mock.Mock()
        app._load_ai_clis.return_value = [{"cli": "claude", "try": []}]
        app._load_settings.return_value = {}
        app._ai_takes_new_id.return_value = False
        app._ai_is_claude = T._ai_is_claude
        app._ai_launch_pair = T._ai_launch_pair
        app._claude_accounts = T._claude_accounts
        app._ai_sub = lambda p, cli=None: p
        return app

    def test_an_account_opens_its_own_claude(self):
        cli = T._claude_account_add("work")
        app = self.app()
        code, _ = T._remote_new(app, {"kind": "ai", "cli": "claude",
                                      "cwd": self.tmp, "account": "work"})
        self.assertEqual(code, 200)
        title, argv = app._add_session.call_args[0][:2]
        self.assertEqual(title, "claude · work")
        self.assertEqual(T._ai_cli_path_of(argv)[0], cli)

    def test_no_account_is_the_default_one(self):
        T._claude_account_add("work")
        app = self.app()
        T._remote_new(app, {"kind": "ai", "cli": "claude", "cwd": self.tmp})
        title, argv = app._add_session.call_args[0][:2]
        self.assertEqual((title, T._ai_cli_path_of(argv)[0]), ("claude", "claude"))

    def test_an_unknown_account_is_refused(self):
        app = self.app()
        code, body = T._remote_new(app, {"kind": "ai", "cli": "claude",
                                         "cwd": self.tmp, "account": "gone"})
        self.assertEqual(code, 400)
        app._add_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
