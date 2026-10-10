#!/usr/bin/env python3
"""Input tabit does not control: fetched manifests, and the note close prompt."""
import io
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

import agent_detect  # noqa: E402
import tabit  # noqa: E402
from gi.repository import Gtk  # noqa: E402


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        pass


class TestManifestNames(unittest.TestCase):
    def test_a_fetched_path_never_leaves_the_cache(self):
        tmp = tempfile.mkdtemp()
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache)
        index = ('[[agents]]\nid="up"\npath="../escaped.toml"\n'
                 '[[agents]]\nid="abs"\npath="%s"\n'
                 '[[agents]]\nid="ok"\npath="ok.toml"\n'
                 % os.path.join(tmp, "abs.toml")).encode()

        def fake(req, timeout=0):
            url = req.full_url
            if url.endswith("index.toml"):
                return _Body(index)
            return _Body(b'id="ok"\nversion="9"\n')

        with unittest.mock.patch("urllib.request.urlopen", fake):
            store = agent_detect.ManifestStore(
                vendored_dir=cache, cache_dir=cache, remote_check=True)
            store.maybe_update_remote(force=True)
        self.assertEqual(sorted(os.listdir(tmp)), ["cache"])
        self.assertIn("ok.toml", os.listdir(cache))


class TestNoteClosePrompt(unittest.TestCase):
    def _answer(self, resp):
        row = unittest.mock.Mock(kind="note", file_path="/tmp/n.md")
        row.buffer.get_modified.return_value = True
        win = unittest.mock.Mock()
        win._save_note.return_value = True
        with unittest.mock.patch.object(Gtk.MessageDialog, "run",
                                        return_value=resp), \
                unittest.mock.patch.object(Gtk.MessageDialog, "__init__",
                                           return_value=None), \
                unittest.mock.patch.object(Gtk.MessageDialog, "destroy"), \
                unittest.mock.patch.object(Gtk.MessageDialog, "add_buttons"), \
                unittest.mock.patch.object(Gtk.MessageDialog,
                                           "format_secondary_text"), \
                unittest.mock.patch.object(Gtk.MessageDialog,
                                           "set_default_response"):
            return tabit.Tabit._confirm_close_row(win, row)

    def test_only_discard_throws_it_away(self):
        self.assertTrue(self._answer(Gtk.ResponseType.REJECT))
        self.assertTrue(self._answer(Gtk.ResponseType.ACCEPT))   # saved
        # Esc and the window X answer DELETE_EVENT: keep the note open
        self.assertFalse(self._answer(Gtk.ResponseType.DELETE_EVENT))
        self.assertFalse(self._answer(Gtk.ResponseType.CANCEL))


if __name__ == "__main__":
    unittest.main()
