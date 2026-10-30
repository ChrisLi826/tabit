#!/usr/bin/env python3
"""tmux's terminal-id question must not reach the pane."""
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import time
import unittest

import fcntl
import termios

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tty_shim  # noqa: E402
from tabit import Tabit, _guard_tmux_client  # noqa: E402


class TestStripQueries(unittest.TestCase):
    def test_drops_da_and_keeps_the_rest(self):
        out, hold = tty_shim.strip_queries(b"abc\x1b[>cdef")
        self.assertEqual(out, b"abcdef")
        self.assertEqual(hold, b"")

    def test_passes_modify_other_keys(self):
        raw = b"\x1b[>4;2mhello\x1b[>4m"
        out, hold = tty_shim.strip_queries(raw)
        self.assertEqual(out, raw)
        self.assertEqual(hold, b"")

    def test_holds_a_cut_off_query(self):
        out, hold = tty_shim.strip_queries(b"xy\x1b[>")
        self.assertEqual(out, b"xy")
        self.assertEqual(hold, b"\x1b[>")
        out, hold = tty_shim.strip_queries(hold + b"cZ")
        self.assertEqual(out, b"Z")
        self.assertEqual(hold, b"")

    def test_drops_zero_form_and_name_query(self):
        out, hold = tty_shim.strip_queries(b"\x1b[>0c\x1b[>q\x1b[>0q.")
        self.assertEqual(out, b".")
        self.assertEqual(hold, b"")


class TestGuard(unittest.TestCase):
    def test_wraps_attach_and_leaves_the_saved_shape(self):
        argv = Tabit._ai_tmux_argv(
            ["/bin/sh", "-c", "cd /tmp || exit 1; exec grok"], "ai-x")
        wrapped = _guard_tmux_client(argv)
        self.assertIn("tty_shim.py", wrapped[2])
        self.assertIn("exec tmux attach-session ", argv[2])
        self.assertNotIn("tty_shim.py", argv[2])
        # wrapping again does not stack filters
        self.assertEqual(_guard_tmux_client(wrapped), wrapped)

    def test_wraps_a_plain_tmux_client(self):
        argv = ["tmux", "new-session", "-A", "-s", "demo"]
        wrapped = _guard_tmux_client(argv)
        self.assertEqual(wrapped[0], sys.executable)
        self.assertTrue(wrapped[1].endswith("tty_shim.py"))
        self.assertEqual(wrapped[2:], argv)

    def test_leaves_a_shell_alone(self):
        argv = ["/bin/sh", "-c", "exec bash"]
        self.assertEqual(_guard_tmux_client(argv), argv)


class TestLateReply(unittest.TestCase):
    def test_late_answer_does_not_land_in_the_pane(self):
        sock = "tabit-shim-%s" % os.getpid()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env.pop("TMUX", None)
        subprocess.run(["tmux", "-L", sock, "kill-server"],
                       stderr=subprocess.DEVNULL)
        try:
            subprocess.run(
                ["tmux", "-L", sock, "-f", "/dev/null", "new-session",
                 "-d", "-s", "s", "cat > /tmp/tabit-shim-pane-%s.txt" % os.getpid()],
                check=True, env=env)
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, 80, 0, 0))
            proc = subprocess.Popen(
                [sys.executable, os.path.join(ROOT, "tty_shim.py"),
                 "tmux", "-L", sock, "-f", "/dev/null", "attach", "-t", "s"],
                stdin=slave, stdout=slave, stderr=slave, env=env,
                start_new_session=True)
            os.close(slave)
            # past tmux 3.2's one-second give-up
            time.sleep(1.3)
            buf = b""
            end = time.time() + 0.5
            while time.time() < end:
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    continue
                try:
                    buf += os.read(master, 8192)
                except OSError:
                    break
            # a terminal that answers late, the way VTE does
            if b"\x1b[>c" in buf:
                os.write(master, b"\x1b[>65;6800;1c")
                time.sleep(0.3)
            os.write(master, b"Z\n")
            time.sleep(0.3)
            # a resize has to reach the session, or the pane stays 80x24
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 40, 100, 0, 0))
            os.kill(proc.pid, signal.SIGWINCH)
            time.sleep(0.3)
            size = subprocess.run(
                ["tmux", "-L", sock, "display-message", "-p", "-t", "s",
                 "#{window_width} #{window_height}"],
                capture_output=True, text=True, env=env)
            cap = subprocess.run(
                ["tmux", "-L", sock, "capture-pane", "-p", "-t", "s"],
                capture_output=True, env=env)
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
            os.close(master)
            text = cap.stdout.decode("utf-8", "replace")
            self.assertNotIn("65;6800", text)
            self.assertNotIn(b"\x1b[>c", buf)
            self.assertEqual(size.stdout.strip(), "100 39")
            pane = "/tmp/tabit-shim-pane-%s.txt" % os.getpid()
            got = b""
            if os.path.exists(pane):
                with open(pane, "rb") as fh:
                    got = fh.read()
                os.remove(pane)
            self.assertIn(b"Z\n", got)
        finally:
            subprocess.run(["tmux", "-L", sock, "kill-server"],
                           stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
