#!/usr/bin/env python3
"""Keep tmux's "what terminal is this?" question off the pane.

tmux 3.2 asks the outer terminal (ESC [ > c) as soon as a client
attaches, and after one second it stops listening. Tabit builds the
terminal before it is on screen, so VTE's answer often arrives late.
tmux then types that answer into the pane: 65;6800;1c in the input.

VTE's answer adds no feature tmux uses (the id is 65). Dropping the
question means VTE never answers, and nothing is typed into the pane.
Every other byte is copied straight through.
"""

import os
import select
import signal
import struct
import sys

try:
    import fcntl
    import termios
    import tty
except ImportError:  # not a Unix tty; the pump still runs
    fcntl = termios = tty = None

# Longer strings first. ESC [ > c is the secondary DA query, ESC [ > q
# asks for a name. A leading 0 is the same query. ESC [ > 4 ; 2 m
# (modifyOtherKeys) must pass: it is not one of these.
_QUERIES = (b"\x1b[>0c", b"\x1b[>0q", b"\x1b[>c", b"\x1b[>q")


def strip_queries(buf):
    """Return (forward, hold). Queries are removed. A cut-off query stays
    in hold so the next read can finish it."""
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        if buf[i] != 0x1B:
            out.append(buf[i])
            i += 1
            continue
        rest = buf[i:]
        hit = next((q for q in _QUERIES if rest.startswith(q)), None)
        if hit is not None:
            i += len(hit)
            continue
        if any(q.startswith(rest) for q in _QUERIES):
            break
        out.append(buf[i])
        i += 1
    return bytes(out), buf[i:]


def _winsize(fd):
    if fcntl is None:
        return None
    return fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))


def _set_winsize(fd, size):
    if fcntl is None or not size:
        return
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


def _raw(fd):
    if tty is None or not os.isatty(fd):
        return
    tty.setraw(fd, termios.TCSANOW)


def main(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: tty_shim.py CMD...\n")
        return 2
    import pty

    _raw(0)
    master, slave = pty.openpty()
    try:
        _set_winsize(master, _winsize(0))
    except OSError:
        pass

    pid = os.fork()
    if pid == 0:
        try:
            os.setsid()
            if fcntl is not None:
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(master)
        if slave > 2:
            os.close(slave)
        os.execvp(argv[1], argv[1:])
        os._exit(127)

    os.close(slave)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)

    def _on_winch(_signum, _frame):
        try:
            os.write(wake_w, b"x")
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, _on_winch)
    hold = b""
    status = 0
    try:
        while True:
            try:
                ready, _, _ = select.select([0, master, wake_r], [], [])
            except InterruptedError:
                continue
            if wake_r in ready:
                try:
                    os.read(wake_r, 64)
                except OSError:
                    pass
                try:
                    _set_winsize(master, _winsize(0))
                except OSError:
                    pass
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                out, hold = strip_queries(hold + data)
                if out:
                    try:
                        os.write(1, out)
                    except OSError:
                        break
            if 0 in ready:
                try:
                    data = os.read(0, 65536)
                except OSError:
                    break
                if not data:
                    break
                try:
                    os.write(master, data)
                except OSError:
                    break
    finally:
        if hold:
            try:
                os.write(1, hold)
            except OSError:
                pass
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            _, status = os.waitpid(pid, 0)
        except OSError:
            status = 0
        os.close(master)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
