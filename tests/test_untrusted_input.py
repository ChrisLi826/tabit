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


class TestPreviewLinks(unittest.TestCase):
    """A link clicked in a note preview opens in the browser, not in the pane."""

    def _click(self, uri, here="file:///home/u/notes/", kind="LINK_CLICKED"):
        if not tabit.HAS_WEBKIT:
            self.skipTest("no WebKit2")
        W = tabit.WebKit2
        decision = unittest.mock.Mock()
        action = decision.get_navigation_action.return_value
        action.get_navigation_type.return_value = getattr(W.NavigationType, kind)
        action.get_request.return_value.get_uri.return_value = uri
        wv = unittest.mock.Mock()
        wv.get_uri.return_value = here
        win = unittest.mock.Mock()
        took = tabit.Tabit._on_preview_policy(
            win, wv, decision, W.PolicyDecisionType.NAVIGATION_ACTION)
        return took, decision, win

    def test_outside_link_goes_to_the_browser(self):
        took, decision, win = self._click("https://example.com/login")
        self.assertTrue(took)
        decision.ignore.assert_called_once()
        win._open_uri.assert_called_once_with("https://example.com/login")

    def test_anchor_in_the_note_scrolls_here(self):
        took, decision, win = self._click("file:///home/u/notes/#todo")
        self.assertFalse(took)
        decision.ignore.assert_not_called()
        win._open_uri.assert_not_called()

    def test_a_refresh_tag_or_iframe_loads_no_site(self):
        took, decision, win = self._click("https://example.com/x", kind="OTHER")
        self.assertTrue(took)
        decision.ignore.assert_called_once()
        win._open_uri.assert_not_called()

    def test_the_note_itself_still_loads(self):
        took, decision, _ = self._click("file:///home/u/notes/a.md", kind="OTHER")
        self.assertFalse(took)
        decision.ignore.assert_not_called()


class TestPreviewScripts(unittest.TestCase):
    """A Markdown preview runs no script from the file, even with Find's on."""

    def test_new_webkit_turns_off_only_the_page_scripts(self):
        w = unittest.mock.Mock()
        tabit.Tabit._set_page_scripts(w, False)
        w.set_enable_javascript.assert_called_once_with(True)
        w.set_enable_javascript_markup.assert_called_once_with(False)

    def test_old_webkit_turns_off_all_script(self):
        w = unittest.mock.Mock(spec=["set_enable_javascript"])
        tabit.Tabit._set_page_scripts(w, False)
        w.set_enable_javascript.assert_called_once_with(False)

    def test_a_page_keeps_its_scripts(self):
        w = unittest.mock.Mock()
        tabit.Tabit._set_page_scripts(w, True)
        w.set_enable_javascript_markup.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
