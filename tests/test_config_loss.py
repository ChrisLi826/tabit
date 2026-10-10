#!/usr/bin/env python3
"""Saves must never write over a file tabit could not read or set aside."""
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
