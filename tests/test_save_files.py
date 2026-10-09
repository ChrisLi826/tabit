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


if __name__ == "__main__":
    unittest.main()
