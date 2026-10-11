#!/usr/bin/env python3
"""sessions.json and settings.json: never a half file, never lost to a bad one."""
import json
import os
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

class TestRestoreSafety(TestSaveFiles):
    """Second review: what a restore or a bad file must not do."""

    def app(self, rows, **kw):
        return type("App", (), dict({"_session_rows": lambda _s: rows,
                                     "_term_cwd": staticmethod(lambda _r: None)}, **kw))()

    def test_only_a_restored_tab_that_failed_is_kept(self):
        def exited(restored, status):
            row = type("Row", (), {"_spawned_at": tabit.time.monotonic(),
                                   "get_style_context": lambda _s: unittest.mock.Mock(),
                                   "dot": unittest.mock.Mock(), "subtitle": unittest.mock.Mock()})()
            if restored:
                row._restored = True
            app = unittest.mock.Mock()
            tabit.Tabit._on_child_exited(app, None, status, row)
            return row._died_young
        self.assertTrue(exited(True, 256))
        self.assertFalse(exited(True, 0))        # "exit" typed at once
        self.assertFalse(exited(False, 256))     # a new quick command tab

    def test_an_unreadable_sessions_file_is_not_written_over(self):
        if os.geteuid() == 0:
            self.skipTest("root reads anything")
        with open(tabit.SESSIONS_FILE, "w") as f:
            f.write("[]")
        os.chmod(tabit.SESSIONS_FILE, 0)
        self.addCleanup(lambda: tabit.Tabit.__dict__.get("_sessions_unreadable")
                        and delattr(tabit.Tabit, "_sessions_unreadable"))
        self.assertEqual(tabit.Tabit._load_sessions(), [])
        row = type("Row", (), {"title_text": "s", "sub_text": "", "argv": ["bash"],
                               "icon_name": "x", "dead": False})()
        app = self.app([row])
        app._sessions_unreadable = tabit.Tabit._sessions_unreadable
        tabit.Tabit._save_sessions(app)
        os.chmod(tabit.SESSIONS_FILE, 0o600)
        with open(tabit.SESSIONS_FILE) as f:
            self.assertEqual(f.read(), "[]")

    def test_an_unrestored_entry_stays_in_the_file(self):
        app = self.app([], _unrestored=[{"label": "from a newer tabit"}])
        tabit.Tabit._save_sessions(app)
        self.assertEqual(tabit.Tabit._load_sessions(), [{"label": "from a newer tabit"}])

    def test_a_linked_file_stays_a_link(self):
        real = os.path.join(self.dir, "dotfiles.json")
        with open(real, "w") as f:
            f.write("[]")
        os.symlink(real, tabit.SESSIONS_FILE)
        tabit.Tabit._save_sessions(self.app([], _unrestored=[{"label": "x"}]))
        self.assertTrue(os.path.islink(tabit.SESSIONS_FILE))
        with open(real) as f:
            self.assertEqual(json.load(f), [{"label": "x"}])

    def test_a_setting_of_the_wrong_type_reads_as_the_default(self):
        with open(tabit.SETTINGS_FILE, "w") as f:
            json.dump({"ui_font_size": "11", "theme": ["x"], "term_line_spacing": 1,
                       "extra": "kept"}, f)
        st = tabit.Tabit._load_settings()
        self.assertEqual(st["ui_font_size"], tabit.DEFAULT_SETTINGS["ui_font_size"])
        self.assertEqual(st["theme"], tabit.DEFAULT_SETTINGS["theme"])
        self.assertEqual(st["term_line_spacing"], 1)     # an int is a fine number
        self.assertEqual(st["extra"], "kept")

    def test_the_last_tab_is_not_closed_from_the_phone(self):
        row = object()
        app = unittest.mock.Mock()
        app._tg_row_by_id = lambda _t: row
        app._session_rows = lambda: [row]
        code, _ = tabit.Tabit._remote_manage_op(app, "close", "t", {})
        self.assertEqual(code, 409)
        app._close_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
