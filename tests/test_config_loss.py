#!/usr/bin/env python3
"""Saves must never write over a file tabit could not read or set aside."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        for name in ("SESSIONS_FILE", "SETTINGS_FILE"):
            old = getattr(tabit, name)
            setattr(tabit, name, os.path.join(d.name, os.path.basename(old)))
            self.addCleanup(setattr, tabit, name, old)
        for a in ("_sessions_unreadable", "_settings_unreadable"):
            self.addCleanup(lambda a=a: tabit.Tabit.__dict__.get(a) is not None
                            and delattr(tabit.Tabit, a))


class TestSetAsideFails(_Base):
    def test_settings_hand_edit_is_kept(self):
        orig = '{"theme": "dracula", "font_size": 14,'
        with open(tabit.SETTINGS_FILE, "w") as f:
            f.write(orig)
        os.mkdir(tabit.SETTINGS_FILE + ".broken")   # cannot be moved there
        tabit.Tabit._load_settings()
        tabit.Tabit._save_settings({"group_names": {}})
        with open(tabit.SETTINGS_FILE) as f:
            self.assertEqual(f.read(), orig)

    def test_sessions_are_not_saved_this_run(self):
        with open(tabit.SESSIONS_FILE, "w") as f:
            f.write('[{"label": "work",')
        os.mkdir(tabit.SESSIONS_FILE + ".broken")
        self.assertEqual(tabit.Tabit._load_sessions(), [])
        self.assertTrue(tabit.Tabit._sessions_unreadable)


class TestDeadTabKeepsCwd(_Base):
    def test_a_tab_that_died_at_once_keeps_its_folder(self):
        proj = os.path.join(self.dir, "proj")
        os.mkdir(proj)
        p = subprocess.Popen(["sh", "-c", "exit 3"], cwd=proj)
        p.wait()                            # gone: no /proc/<pid>/cwd
        row = type("Row", (), {
            "title_text": "t", "sub_text": "", "argv": ["sh", "-c", "exit 3"],
            "icon_name": "x", "dead": True, "_died_young": True,
            "pid": p.pid, "_cwd_wanted": None, "_cwd_saved": proj})()
        win = tabit.Tabit.__new__(tabit.Tabit)
        win._session_rows = lambda: [row]
        tabit.Tabit._save_sessions(win)
        with open(tabit.SESSIONS_FILE) as f:
            self.assertEqual(json.load(f)[0]["cwd"], proj)


class TestPasswordFileMode(_Base):
    def test_connect_last_is_private_new_or_old(self):
        path = os.path.join(self.dir, "connect_last.json")
        old = tabit.CONNECT_LAST_FILE
        tabit.CONNECT_LAST_FILE = path
        self.addCleanup(setattr, tabit, "CONNECT_LAST_FILE", old)
        mask = os.umask(0o002)
        self.addCleanup(os.umask, mask)
        tabit.Tabit._save_connect_last({"password": "x"})      # a new file
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        os.chmod(path, 0o664)                                    # an old one
        tabit.Tabit._save_connect_last({"password": "y"})
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            self.assertEqual(json.load(f), {"password": "y"})


class TestStatusPollSurvives(unittest.TestCase):
    def test_one_bad_tab_does_not_end_the_timer(self):
        win = tabit.Tabit.__new__(tabit.Tabit)
        rows = [type("R", (), {"icon_name": "x", "dead": False, "term": 1})()
                for _ in range(2)]
        win._session_rows = lambda: rows
        seen = []

        def upd(r):
            seen.append(r)
            if r is rows[0]:
                raise ValueError("odd screen")
        win._update_agent_status = upd
        with unittest.mock.patch.object(tabit, "_is_ai_icon", lambda _i: True):
            self.assertTrue(tabit.Tabit._poll_ai_agent_statuses(win))
        self.assertEqual(seen, rows)                # the second tab still ran


class TestYamlTreePaths(unittest.TestCase):
    def test_no_key_is_built_into_script(self):
        # a YAML key is data: never inside onclick or an unescaped attribute
        with open(os.path.join(ROOT, "tabit.py")) as f:
            src = f.read()
        self.assertNotIn("copyPath('${{currentPath}}')", src)
        self.assertNotIn("copy path: ${{currentPath}}", src)
        self.assertIn("onclick=\"copyPath(this.dataset.path)\"", src)


if __name__ == "__main__":
    unittest.main()
