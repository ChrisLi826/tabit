"""Clickable terminal links: URL pattern and the OSC 8 / regex fallback order."""
import os
import re
import sys
import unittest

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkSource", "4")
gi.require_version("Vte", "2.91")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tabit  # noqa: E402
from tabit import (  # noqa: E402
    TERM_URL_PATTERN, TERM_URL_REGEX, Tabit, _strip_url_tail,
)


class _FakeTerm:
    """Stands in for Vte.Terminal: VTE does the matching, we test the glue."""

    def __init__(self, hyperlink=None, match=None, hyperlink_raises=False):
        self._hyperlink = hyperlink
        self._match = match
        self._raises = hyperlink_raises

    def hyperlink_check_event(self, _event):
        if self._raises:
            raise TypeError("older VTE")
        return self._hyperlink

    def match_check_event(self, _event):
        return self._match, 0


class TestUrlPattern(unittest.TestCase):
    """The pattern is PCRE2 in VTE; this mirror keeps its intent pinned."""

    def setUp(self):
        self.rx = re.compile(TERM_URL_PATTERN)

    def first(self, text):
        m = self.rx.search(text)
        return _strip_url_tail(m.group(0)) if m else None

    def test_vte_accepts_the_pattern(self):
        self.assertIsNotNone(TERM_URL_REGEX, "VTE rejected TERM_URL_PATTERN")

    def test_plain_url(self):
        self.assertEqual(self.first("see https://herdr.dev/docs for info"),
                         "https://herdr.dev/docs")

    def test_ftp(self):
        self.assertEqual(self.first("get ftp://mirror.org/f.tar.gz now"),
                         "ftp://mirror.org/f.tar.gz")

    def test_stops_at_quote(self):
        self.assertEqual(self.first('{"url": "http://a.b/c"}'), "http://a.b/c")

    def test_stops_at_bracket(self):
        self.assertEqual(self.first("(see https://x.io/p)"), "https://x.io/p")

    def test_drops_sentence_period(self):
        self.assertEqual(self.first("visit https://a.io/x. Then stop."),
                         "https://a.io/x")

    def test_keeps_query_string(self):
        self.assertEqual(self.first("https://a.io/x?q=1&b=2"),
                         "https://a.io/x?q=1&b=2")

    def test_no_match_in_connect_argv(self):
        self.assertIsNone(self.first("python3 connect.py --sn 2150X4213495"))

    def test_no_match_on_serial_log(self):
        self.assertIsNone(self.first("[2026-09-21] serial /dev/ttyUSB2 115200"))

    def test_no_match_on_email(self):
        self.assertIsNone(self.first("mail chris@senao.com ok"))


class TestLinkLookupOrder(unittest.TestCase):
    """OSC 8 wins over the text match; both get their tail stripped."""

    def test_hyperlink_preferred(self):
        term = _FakeTerm(hyperlink="https://osc8.example/real",
                         match="https://text.example/other")
        self.assertEqual(Tabit._term_link_at_event(term, None),
                         "https://osc8.example/real")

    def test_falls_back_to_text_match(self):
        term = _FakeTerm(hyperlink=None, match="https://text.example/x")
        self.assertEqual(Tabit._term_link_at_event(term, None),
                         "https://text.example/x")

    def test_none_when_nothing_under_pointer(self):
        self.assertIsNone(Tabit._term_link_at_event(_FakeTerm(), None))

    def test_survives_vte_without_hyperlink_support(self):
        term = _FakeTerm(match="https://text.example/x", hyperlink_raises=True)
        self.assertEqual(Tabit._term_link_at_event(term, None),
                         "https://text.example/x")

    def test_strips_tail_from_text_match(self):
        term = _FakeTerm(match="https://a.io/x.")
        self.assertEqual(Tabit._term_link_at_event(term, None),
                         "https://a.io/x")


class TestOpenUri(unittest.TestCase):
    """A launch context leaves the desktop's busy cursor stuck on tabit.

    Browsers ship StartupNotify=true. Launched with a context, an already
    running one hands the URL to its existing process and exits without
    mapping a window, so the startup sequence never completes.
    """

    def setUp(self):
        self.calls = []
        self._real = tabit.Gio.AppInfo.launch_default_for_uri

        def spy(uri, context):
            self.calls.append((uri, context))
            return True

        tabit.Gio.AppInfo.launch_default_for_uri = staticmethod(spy)
        self.addCleanup(setattr, tabit.Gio.AppInfo,
                        "launch_default_for_uri", self._real)

    def test_passes_the_uri_through(self):
        Tabit._open_uri("https://herdr.dev/docs")
        self.assertEqual(self.calls[0][0], "https://herdr.dev/docs")

    def test_never_passes_a_launch_context(self):
        Tabit._open_uri("https://herdr.dev/docs")
        self.assertIsNone(self.calls[0][1])


if __name__ == "__main__":
    unittest.main()
