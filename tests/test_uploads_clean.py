#!/usr/bin/env python3
"""Files the pages upload (and Nearby receives) pile up in day folders:
Settings shows their size, and can delete the old ones, now or every hour."""
import datetime
import os
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
TODAY = datetime.date(2026, 11, 18)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        p = mock.patch.object(tabit, "REMOTE_UPLOAD_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def put(self, day, name, size):
        os.makedirs(os.path.join(self.dir, day), exist_ok=True)
        with open(os.path.join(self.dir, day, name), "wb") as f:
            f.write(b"x" * size)

    def days(self):
        return sorted(os.listdir(self.dir))


class TestUsage(Base):

    def test_counts_every_day_folder(self):
        self.put("2026-10-09", "a.png", 100)
        self.put("2026-11-18", "b.png", 20)
        self.put("2026-11-18", "c.md", 3)
        self.assertEqual(tabit.uploads_usage(), {"bytes": 123, "files": 3, "days": 2})

    def test_no_folder_yet(self):
        os.rmdir(self.dir)
        self.assertEqual(tabit.uploads_usage(), {"bytes": 0, "files": 0, "days": 0})


class TestSizeLabel(unittest.TestCase):

    def test_units(self):
        self.assertEqual([tabit._size_label(b) for b in (0, 1023, 1536, 52 << 20, 3 << 30)],
                         ["0 B", "1023 B", "1.5 KB", "52.0 MB", "3.0 GB"])


class TestClean(Base):

    def test_deletes_only_folders_older_than_the_days(self):
        self.put("2026-10-18", "old.png", 10)       # 31 days old
        self.put("2026-10-19", "edge.png", 20)      # 30 days old: kept
        self.put("2026-11-18", "new.png", 30)
        got = tabit.uploads_clean(30, today=TODAY)
        self.assertEqual(got, {"files": 1, "bytes": 10, "days": 1})
        self.assertEqual(self.days(), ["2026-10-19", "2026-11-18"])

    def test_leaves_what_it_did_not_make(self):
        # only day folders: a stray file, another folder, a link stay
        self.put("2026-01-01", "old.png", 10)
        self.put("keep", "x", 1)
        with open(os.path.join(self.dir, "1999-01-01"), "w") as f:
            f.write("a file named like a day")
        outside = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        with open(os.path.join(outside, "precious"), "w") as f:
            f.write("!")
        os.symlink(outside, os.path.join(self.dir, "2000-01-01"))
        tabit.uploads_clean(1, today=TODAY)
        self.assertEqual(self.days(), ["1999-01-01", "2000-01-01", "keep"])
        self.assertTrue(os.path.exists(os.path.join(outside, "precious")))

    def test_no_folder_yet(self):
        os.rmdir(self.dir)
        self.assertEqual(tabit.uploads_clean(30, today=TODAY), {"files": 0, "bytes": 0, "days": 0})


def app(**settings):
    s = {"uploads_auto_clean": False, "uploads_keep_days": 30}
    s.update(settings)
    a = mock.Mock(_remote_send_on=True, _REMOTE_SETTINGS=T._REMOTE_SETTINGS)
    a._load_settings.return_value = s
    a._remote_admin_state = T._remote_admin_state.__get__(a)
    a._ai_accounts.return_value = []
    a._ai_auth_status.return_value = {}
    a._remote_login = a._remote_update = None
    return a


class TestPhoneSettings(Base):

    def test_state_shows_the_size_and_the_setting(self):
        self.put("2026-11-18", "b.png", 20)
        st = T._remote_admin(app(uploads_auto_clean=True, uploads_keep_days=7), "state", {})[1]
        self.assertEqual(st["uploads"], {"bytes": 20, "files": 1, "days": 1,
                                         "auto": True, "keep_days": 7})

    def test_set_the_cleaning(self):
        a = app()
        body = {"uploads_auto_clean": True, "uploads_keep_days": 14}
        self.assertEqual(T._remote_admin(a, "set", body)[0], 200)
        a._save_settings.assert_called_once_with(body)
        a._uploads_tick.assert_called_once_with()     # on now: it runs at once

    def test_set_refuses_bad_days(self):
        for bad in ({"uploads_keep_days": 0}, {"uploads_keep_days": 5000},
                    {"uploads_keep_days": "30"}, {"uploads_keep_days": True},
                    {"uploads_auto_clean": "yes"}):
            a = app()
            self.assertEqual(T._remote_admin(a, "set", bad)[0], 400, bad)
            a._save_settings.assert_not_called()

    def test_clean_now(self):
        old = (datetime.date.today() - datetime.timedelta(days=40)).isoformat()
        self.put(old, "old.png", 10)
        self.put(datetime.date.today().isoformat(), "new.png", 5)
        code, out = T._remote_admin(app(), "uploads-clean", {"days": 30})
        self.assertEqual(code, 200)
        self.assertEqual(out["deleted"], {"files": 1, "bytes": 10, "days": 1})
        self.assertEqual(out["uploads"]["bytes"], 5)

    def test_clean_now_needs_days(self):
        for bad in ({}, {"days": 0}, {"days": "7"}):
            self.assertEqual(T._remote_admin(app(), "uploads-clean", bad)[0], 400, bad)

    def test_nothing_without_typing_from_the_phone(self):
        a = app()
        a._remote_send_on = False
        self.assertEqual(T._remote_admin(a, "uploads-clean", {"days": 1})[0], 403)


class TestTick(Base):

    def test_cleans_only_when_on(self):
        self.put("2000-01-01", "old.png", 10)
        a = app()
        a._uploads_cleaning = False
        T._uploads_tick(a, wait=True)
        self.assertEqual(self.days(), ["2000-01-01"])
        a = app(uploads_auto_clean=True, uploads_keep_days=30)
        a._uploads_cleaning = False
        self.assertTrue(T._uploads_tick(a, wait=True))     # True: the timer goes on
        self.assertEqual(self.days(), [])


if __name__ == "__main__":
    unittest.main()
