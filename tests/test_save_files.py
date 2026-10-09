#!/usr/bin/env python3
"""sessions.json and settings.json: never a half file, never lost to a bad one."""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")

import tabit  # noqa: E402


class TestSaveFiles(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = d.name
        for name in ("SESSIONS_FILE", "SETTINGS_FILE"):
            old = getattr(tabit, name)
            setattr(tabit, name, os.path.join(d.name, os.path.basename(old)))
            self.addCleanup(setattr, tabit, name, old)

    def test_save_leaves_one_whole_file(self):
        row = type("Row", (), {"title_text": "shell", "sub_text": "", "argv": ["bash"],
                               "icon_name": "x", "dead": False})()
        app = type("App", (), {"_session_rows": lambda _s: [row],
                               "_term_cwd": staticmethod(lambda _r: None)})()
        tabit.Tabit._save_sessions(app)
        self.assertEqual(os.listdir(self.dir), ["sessions.json"])
        with open(tabit.SESSIONS_FILE) as f:
            self.assertEqual(json.load(f)[0]["label"], "shell")

    def test_a_broken_sessions_file_is_kept(self):
        with open(tabit.SESSIONS_FILE, "w") as f:
            f.write("[{")
        self.assertEqual(tabit.Tabit._load_sessions(), [])
        with open(tabit.SESSIONS_FILE + ".broken") as f:
            self.assertEqual(f.read(), "[{")
        self.assertFalse(os.path.exists(tabit.SESSIONS_FILE))

    def test_settings(self):
        with open(tabit.SETTINGS_FILE, "w") as f:
            f.write('{"theme": ')
        self.assertEqual(tabit.Tabit._load_settings()["theme"], tabit.DEFAULT_SETTINGS["theme"])
        self.assertTrue(os.path.exists(tabit.SETTINGS_FILE + ".broken"))
        tabit.Tabit._save_settings({"theme": "x"})
        self.assertEqual(sorted(os.listdir(self.dir)), ["settings.json", "settings.json.broken"])
        self.assertEqual(tabit.Tabit._load_settings()["theme"], "x")


    def test_wrong_shape_is_kept(self):
        with open(tabit.SESSIONS_FILE, "w") as f:
            f.write('{"not": "a list"}')
        self.assertEqual(tabit.Tabit._load_sessions(), [])
        self.assertTrue(os.path.exists(tabit.SESSIONS_FILE + ".broken"))

    def test_a_tab_that_died_at_once_stays_saved(self):
        def row(young):
            return type("Row", (), {"title_text": "ai", "sub_text": "", "argv": ["x"], "icon_name": "x",
                                    "dead": True, "_died_young": young})()
        app = type("App", (), {"_session_rows": lambda _s: [row(True), row(False)],
                               "_term_cwd": staticmethod(lambda _r: None)})()
        tabit.Tabit._save_sessions(app)
        self.assertEqual(len(tabit.Tabit._load_sessions()), 1)

    def test_a_missing_folder_is_saved_until_it_moves(self):
        r = type("Row", (), {"title_text": "sh", "sub_text": "", "argv": ["bash"], "icon_name": "x",
                             "dead": False, "_cwd_wanted": "/mnt/work"})()
        home = tabit.GLib.get_home_dir()
        cwd = [home]
        app = type("App", (), {"_session_rows": lambda _s: [r],
                               "_term_cwd": staticmethod(lambda _r: cwd[0])})()
        tabit.Tabit._save_sessions(app)
        self.assertEqual(tabit.Tabit._load_sessions()[0]["cwd"], "/mnt/work")
        cwd[0] = "/tmp"
        tabit.Tabit._save_sessions(app)
        self.assertEqual(tabit.Tabit._load_sessions()[0]["cwd"], "/tmp")
        self.assertIsNone(r._cwd_wanted)

if __name__ == "__main__":
    unittest.main()
