#!/usr/bin/env python3
"""Each AI tab starts in a systemd scope of its own, so systemd-oomd,
which kills a whole cgroup, takes one tab and not tabit with all of them.
The tmux server tabit starts goes in tabit-tmux.scope."""
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
INNER = ["/bin/sh", "-c", "cd '/w x' || exit 1; exec claude --resume abc || exec claude"]


def wrap(on, inner=INNER, session="ai-claude-w-123456"):
    with mock.patch.object(tabit, "_ai_scopes_on", return_value=on):
        return T._ai_tmux_argv(inner, session)


class TestArgv(unittest.TestCase):

    def test_scoped_command(self):
        cmd = wrap(True)[2]
        self.assertIn("--unit=tabit-ai-claude-w-123456.scope -- \"$@\"", cmd)
        self.assertIn("--unit=tabit-tmux.scope -- tmux new-session -d -s ai-claude-w-123456", cmd)
        self.assertIn("-p Delegate=yes", cmd)
        self.assertTrue(cmd.startswith("tmux ") or cmd.startswith("tmux source-file"))

    def test_without_systemd_it_is_the_plain_command(self):
        cmd = wrap(False)[2]
        self.assertNotIn("systemd", cmd)
        self.assertIn(" || tmux new-session -d -s ai-claude-w-123456 ", cmd)

    def test_round_trip(self):
        for on in (True, False):
            argv = wrap(on)
            self.assertEqual(T._ai_tmux_unwrap(argv), (INNER, "ai-claude-w-123456"))
            self.assertEqual(T._tmux_session_from_argv(argv), "ai-claude-w-123456")
            self.assertEqual(T._ai_cli_path_of(argv), ("claude", "/w x"))
            with mock.patch.object(tabit, "_ai_scopes_on", return_value=on):
                self.assertEqual(T._ai_tmux_argv(*T._ai_tmux_unwrap(argv)), argv)

    def test_a_plain_wrapper_saved_before_unwraps_into_a_scoped_one(self):
        inner, sess = T._ai_tmux_unwrap(wrap(False))
        self.assertEqual(wrap(True, inner, sess), wrap(True))

    def test_unit_name_is_as_ai_scopes_gives_it(self):
        self.assertEqual(tabit._ai_scope_unit("ai-claude-x-1"), "tabit-ai-claude-x-1.scope")
        self.assertEqual(tabit._ai_scope_unit("ai-a b"), "tabit-ai-a_b.scope")

    def test_launch_pair_wraps_both(self):
        with mock.patch.object(tabit, "_ai_scopes_on", return_value=True):
            launch, stored, _ = T._ai_launch_pair("claude", "/tmp", ["--continue"], tmux=True)
        for argv in (launch, stored):
            self.assertIn("systemd-run", argv[2])
            self.assertEqual(T._ai_cli_path_of(argv), ("claude", "/tmp"))


class TestScopesOn(unittest.TestCase):

    def setUp(self):
        self.run = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.run, True)
        p = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.run})
        p.start()
        self.addCleanup(p.stop)

    def test_needs_systemd_run_and_a_user_systemd(self):
        os.makedirs(os.path.join(self.run, "systemd"))
        with mock.patch.object(tabit.shutil, "which", return_value="/usr/bin/systemd-run"):
            self.assertFalse(tabit._ai_scopes_on())            # no user systemd
            open(os.path.join(self.run, "systemd", "private"), "w").close()
            self.assertTrue(tabit._ai_scopes_on())
        with mock.patch.object(tabit.shutil, "which", return_value=None):
            self.assertFalse(tabit._ai_scopes_on())            # no systemd-run


FAKE = r"""#!/bin/sh
# fake %(prog)s: log the call; answer from files in $FAKE
echo "%(prog)s $*" >> "$FAKE/log"
case "%(prog)s $1 $2" in
  "tmux has-session"*) [ -e "$FAKE/session" ] ;;
  "tmux list-sessions"*) [ -e "$FAKE/server" ] ;;
  "tmux new-session"*) touch "$FAKE/server" "$FAKE/session" ;;
  "systemctl --user is-active") [ -e "$FAKE/active" ] ;;
  "systemd-run"*)
    [ -e "$FAKE/refuse" ] && exit 1
    while [ "$1" != "--" ]; do shift; done; shift; "$@" ;;
  *) exit 0 ;;
esac
"""


class TestShell(unittest.TestCase):
    """Run the wrapper with fake tmux, systemctl and systemd-run."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.d, True)
        for prog in ("tmux", "systemctl", "systemd-run"):
            path = os.path.join(self.d, prog)
            with open(path, "w") as f:
                f.write(FAKE % {"prog": prog})
            os.chmod(path, 0o755)

    def run_it(self, *have):
        for name in have:
            open(os.path.join(self.d, name), "w").close()
        env = dict(os.environ, FAKE=self.d, PATH=self.d + ":/usr/bin:/bin")
        with mock.patch.object(tabit, "USER_TMUX_CONF", "/nonexistent"):
            cmd = wrap(True, ["/bin/sh", "-c", "exec claude"], "ai-a")[2]
        subprocess.run(["/bin/sh", "-c", cmd], env=env, check=False)
        with open(os.path.join(self.d, "log")) as f:
            return [l for l in f.read().splitlines()
                    if not l.startswith(("tmux set-option", "tmux attach-session"))]

    UNIT = ("systemd-run --user --scope -q --collect --slice=app.slice -p Delegate=yes"
            " --unit=tabit-ai-a.scope -- /bin/sh -c exec claude")

    def test_no_server_starts_it_in_its_scope_and_the_tab_in_its_own(self):
        log = self.run_it()
        self.assertIn("systemd-run --user --scope -q --collect --slice=app.slice -p Delegate=yes"
                      " --unit=tabit-tmux.scope -- tmux new-session -d -s ai-a " + self.UNIT, log)
        self.assertIn("tmux new-session -d -s ai-a " + self.UNIT, log)
        self.assertEqual(sum(l.startswith("tmux new-session") for l in log), 1)

    def test_a_running_server_only_gets_the_tab_scope(self):
        log = self.run_it("server")
        self.assertNotIn("tabit-tmux.scope --", " ".join(log))
        self.assertIn("tmux new-session -d -s ai-a " + self.UNIT, log)

    def test_an_open_session_is_only_attached(self):
        log = self.run_it("server", "session")
        self.assertEqual(log, ["tmux has-session -t =ai-a:"])

    def test_a_scope_still_up_starts_the_tab_without_one(self):
        log = self.run_it("server", "active")
        self.assertIn("tmux new-session -d -s ai-a /bin/sh -c exec claude", log)
        self.assertFalse(any(l.startswith("systemd-run") for l in log))

    def test_a_refused_server_scope_still_starts_the_session(self):
        log = self.run_it("refuse")
        self.assertIn("tmux new-session -d -s ai-a " + self.UNIT, log)

    def test_failed_scopes_are_reset_first(self):
        log = self.run_it()
        self.assertIn("systemctl --user reset-failed tabit-ai-a.scope tabit-tmux.scope", log)


if __name__ == "__main__":
    unittest.main()
