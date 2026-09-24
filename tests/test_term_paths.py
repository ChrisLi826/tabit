"""Clickable file paths: what matches, and what a match resolves to."""
import os
import re
import sys
import tempfile
import unittest

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkSource", "4")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabit import (  # noqa: E402
    TERM_PATH_PATTERN, TERM_PATH_REGEX, TERM_URL_PATTERN, TERM_URL_REGEX,
    Tabit, _is_text_file, _mnemonic_label, _rejoin_wrapped,
    _split_path_line,
)
import tabit  # noqa: E402


class TestPathPattern(unittest.TestCase):
    """PCRE2 does the matching in VTE; this mirror pins the intent."""

    def setUp(self):
        self.rx = re.compile(TERM_PATH_PATTERN)

    def first(self, text):
        m = self.rx.search(text)
        return m.group(0) if m else None

    def test_vte_accepts_the_pattern(self):
        self.assertIsNotNone(TERM_PATH_REGEX)

    def test_source_file(self):
        p = ("/home/chris/cloudcamsdk/SENAO/package/repo/"
             "libcloudsnipcam/src/live_counts.c")
        self.assertEqual(self.first(p), p)

    def test_long_scratchpad_path(self):
        p = ("/tmp/claude-1000/-home-chris-cloudcamsdk-SENAO/"
             "0ca008c1-7766-4bcd/scratchpad/IMPL-phase1.md")
        self.assertEqual(self.first(p), p)

    def test_no_extension(self):
        p = "/home/chris/repo/senao-openapi-module/src/Makefile"
        self.assertEqual(self.first(p), p)

    def test_dotted_name(self):
        p = "/home/chris/repo/senao-web/files/snweb.cc.init"
        self.assertEqual(self.first(p), p)

    def test_tilde_path(self):
        p = "~/knowledge-base/topics/ip-camera-firmware/deploy.md"
        self.assertEqual(self.first(p), p)

    def test_line_suffix_is_part_of_the_match(self):
        self.assertEqual(self.first("at /home/chris/tabit/tabit.py:4231 here"),
                         "/home/chris/tabit/tabit.py:4231")

    def test_device_node(self):
        self.assertEqual(self.first("[2026-09-21] serial /dev/ttyUSB2 115200"),
                         "/dev/ttyUSB2")

    def test_does_not_eat_a_url(self):
        """The // inside a URL must not read as an absolute path."""
        self.assertIsNone(self.first("see https://herdr.dev/docs for info"))
        self.assertIsNone(self.first("visit http://a.io/x/y/z now"))

    def test_not_a_fraction(self):
        self.assertIsNone(self.first("ratio 3/4 done"))

    def test_not_a_bare_slash(self):
        self.assertIsNone(self.first("cd / then stop"))

    def test_url_pattern_still_wins_its_own_text(self):
        self.assertIsNotNone(re.compile(TERM_URL_PATTERN)
                             .search("go to https://a.io/x"))


class TestSplitPathLine(unittest.TestCase):
    def test_splits_a_line_number(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "b.c")
            with open(src, "w") as f:
                f.write("int main(void) { return 0; }\n")
            self.assertEqual(_split_path_line(src + ":42"), (src, 42))

    def test_no_suffix(self):
        self.assertEqual(_split_path_line("/a/b.c"), ("/a/b.c", 0))

    def test_a_real_file_is_never_split(self):
        with tempfile.TemporaryDirectory() as d:
            odd = os.path.join(d, "report:2026")
            open(odd, "w").close()
            self.assertEqual(_split_path_line(odd), (odd, 0))

    def test_trailing_colon_without_digits(self):
        self.assertEqual(_split_path_line("/a/b.c:"), ("/a/b.c:", 0))


class TestIsTextFile(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)

    def write(self, name, data):
        path = os.path.join(self.d.name, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_plain_text(self):
        self.assertTrue(_is_text_file(self.write("a.md", b"# hello\n")))

    def test_a_short_file_with_one_bad_byte_is_binary(self):
        # The cut-character exemption only applies to a probe that filled up.
        self.assertFalse(_is_text_file(self.write("s.bin", b"hello\xff")))

    def test_a_four_byte_binary_is_binary(self):
        self.assertFalse(
            _is_text_file(self.write("t.bin", bytes([0xFF, 0xFE, 0xFD, 0xFC]))))

    def test_a_cut_multibyte_character_at_a_full_probe_is_text(self):
        data = b"a" * 8190 + "中".encode()  # last char straddles the probe
        self.assertTrue(_is_text_file(self.write("u.txt", data)))

    def test_no_extension_is_still_text(self):
        self.assertTrue(_is_text_file(self.write("Makefile", b"all:\n\tcc\n")))

    def test_utf8_text(self):
        self.assertTrue(_is_text_file(
            self.write("b.md", "燒進去的韌體\n".encode())))

    def test_nul_byte_is_binary(self):
        self.assertFalse(_is_text_file(self.write("f.bin", b"ELF\x00\x01\x02")))

    def test_multibyte_cut_by_the_probe_is_still_text(self):
        data = "檔".encode() * 4000          # 12000 bytes, cut mid-character
        self.assertTrue(_is_text_file(self.write("c.md", data)))

    def test_missing_file(self):
        self.assertFalse(_is_text_file(os.path.join(self.d.name, "nope")))

    def test_directory_is_not_text(self):
        self.assertFalse(_is_text_file(self.d.name))


if __name__ == "__main__":
    unittest.main()

class TestPathRegexRejectsRelative(unittest.TestCase):
    """A match may not start at the slash that follows a dot.

    `./etc/passwd` in a log line used to open the real /etc/passwd, because
    _open_path resolves against tabit's own cwd, not the terminal's.
    """

    def _hit(self, text):
        return "[[" in TERM_PATH_REGEX.substitute(text, "[[$0]]", 0x100)

    def test_dot_slash_does_not_match(self):
        self.assertFalse(self._hit("./etc/passwd"))

    def test_dot_dot_slash_does_not_match(self):
        self.assertFalse(self._hit("../etc/passwd"))

    def test_nested_parent_walk_does_not_match(self):
        self.assertFalse(self._hit("foo/../../home/chris/.ssh/id_rsa"))

    def test_a_bracketed_tag_still_leaves_the_path(self):
        # Build logs print `[build]/src/main.c:12` and that stays clickable.
        self.assertTrue(self._hit("[build]/src/main.c:12"))

    def test_a_plain_path_still_matches(self):
        self.assertTrue(self._hit("/etc/hosts"))


class TestUrlRegexTakesIpv6(unittest.TestCase):
    """An IPv6 URL has to match as a URL, or its path half is taken as a file.

    The URL matcher is registered first, so once it covers the whole thing
    VTE stops handing `/home/you/.ssh/id_rsa` to the path matcher.
    """

    def _hit(self, text):
        return "[[" in TERM_URL_REGEX.substitute(text, "[[$0]]", 0x100)

    def test_plain_ipv6_host(self):
        self.assertTrue(self._hit("http://[::1]/home/you/.ssh/id_rsa"))

    def test_ipv6_with_port(self):
        self.assertTrue(self._hit("http://[::1]:8080/x/y"))

    def test_ipv6_with_zone_id(self):
        self.assertTrue(self._hit("http://[fe80::1%eth0]/etc/passwd"))

    def test_a_url_in_parens_still_ends_at_the_paren(self):
        self.assertEqual(
            TERM_URL_REGEX.substitute("(see https://a.io/x)", "[[$0]]", 0x100),
            "(see [[https://a.io/x]])")


class TestLineSuffixIsReallyALine(unittest.TestCase):
    """`:number` is a line number only when the rest opens as a note.

    A serial log says `open /dev/ttyUSB0:115200 failed`, and handing a live
    port to the desktop would disturb it.
    """

    def test_a_baud_rate_is_not_a_line(self):
        self.assertEqual(_split_path_line("/dev/ttyUSB0:115200"),
                         ("/dev/ttyUSB0:115200", 0))

    def test_a_pid_after_a_binary_is_not_a_line(self):
        self.assertEqual(_split_path_line("/usr/bin/python3:4321"),
                         ("/usr/bin/python3:4321", 0))

    def test_a_huge_line_number_is_left_alone(self):
        # Past 2**31 GTK raises, and 5000 digits trips Python's int limit.
        self.assertEqual(_split_path_line("/etc/hosts:2147483649"),
                         ("/etc/hosts:2147483649", 0))
        raw = "/etc/hosts:" + "9" * 5000
        self.assertEqual(_split_path_line(raw), (raw, 0))


class _Sink:
    """Stands in for the window: records where _open_path sent the click."""

    _note_file_too_big = staticmethod(Tabit._note_file_too_big)
    _resolve_term_path = staticmethod(Tabit._resolve_term_path)
    _is_browser_page = staticmethod(Tabit._is_browser_page)

    def __init__(self):
        self.calls = []

    def _open_in_browser(self, path):
        self.calls.append(("browser", path))
        return True

    def _open_uri(self, uri):
        self.calls.append(("uri", uri))
        return True

    def _add_note_session(self, path=None):
        self.calls.append(("note", path))
        return object()

    def _note_set_preview(self, _row, on):
        self.calls.append(("preview", bool(on)))

    def _open_big_file(self, path):
        self.calls.append(("big", path))


class TestOpenPathRouting(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.sink = _Sink()

    def write(self, name, text):
        path = os.path.join(self.d.name, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_sentence_period_does_not_stop_the_open(self):
        # Agent output ends sentences with paths, and the matcher keeps the
        # period because a file may really end in one.
        src = self.write("notes.txt", "hello\n")
        Tabit._open_path(self.sink, src + ".")
        self.assertEqual(self.sink.calls, [("note", src)])

    def test_an_exact_path_wins_over_the_trim(self):
        odd = self.write("odd.", "hello\n")
        Tabit._open_path(self.sink, odd)
        self.assertEqual(self.sink.calls, [("note", odd)])

    def test_html_renders_in_a_note_tab(self):
        # A page is meant to be looked at, so the preview opens beside the
        # source rather than handing the file to the browser.
        page = self.write("page.html", "<html><body>hi</body></html>")
        Tabit._open_path(self.sink, page)
        self.assertEqual(self.sink.calls,
                         [("note", page), ("preview", True)])

    def test_a_plain_text_file_gets_no_preview(self):
        src = self.write("notes.txt", "hello\n")
        Tabit._open_path(self.sink, src)
        self.assertEqual(self.sink.calls, [("note", src)])

    def test_markdown_opens_showing_the_document(self):
        # Markdown is written to be read, so the preview is on, and it
        # takes the whole tab the way a page does.
        src = self.write("notes.md", "# hello\n")
        Tabit._open_path(self.sink, src)
        self.assertEqual(self.sink.calls, [("note", src), ("preview", True)])

    def test_a_huge_single_line_file_is_not_thrown_at_the_desktop(self):
        # The note editor freezes on one long line, and so does whatever
        # the desktop opens it with -- it is a GtkSourceView too. Ask.
        src = self.write("acl.txt", "x" * (tabit.NOTE_MAX_OPEN_LINE + 1))
        Tabit._open_path(self.sink, src)
        self.assertEqual(self.sink.calls, [("big", src)])

    def test_a_binary_file_still_goes_to_the_desktop(self):
        src = self.write("blob.bin", "\x00\xff\xfe" * 40)
        Tabit._open_path(self.sink, src)
        self.assertEqual([c[0] for c in self.sink.calls], ["uri"])

    def test_a_zero_stat_file_never_reaches_a_note(self):
        # /proc reports size 0 and still reads 15MB, so the size check has
        # to read rather than trust stat. It is text, so it goes to the
        # big-file question, not to the desktop, which would freeze on it
        # just as the note editor would.
        Tabit._open_path(self.sink, "/proc/kallsyms")
        self.assertEqual(self.sink.calls, [("big", "/proc/kallsyms")])

    def test_a_binary_file_is_never_written_back(self):
        """Neither Save nor Save As may write the empty buffer to an image.

        Nothing was read into the buffer, and the Save As chooser opens on
        the image's own path, so both would replace it with an empty file.
        """
        class Row:
            kind = "note"

        row = Row()
        row.file_path = os.path.join(self.d.name, "shot.png")
        with open(row.file_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
        size = os.path.getsize(row.file_path)

        class App:
            _save_note = Tabit._save_note
            _is_binary_render = staticmethod(Tabit._is_binary_render)
        app = App()
        self.assertTrue(app._save_note(row))
        self.assertTrue(app._save_note(row, save_as=True))
        self.assertEqual(os.path.getsize(row.file_path), size)

    def test_a_serial_port_click_does_nothing(self):
        # `open /dev/ttyUSB0:115200 failed` must not hand the port to the
        # desktop, which could change its settings.
        self.assertFalse(Tabit._open_path(self.sink, "/dev/ttyUSB0:115200"))
        self.assertEqual(self.sink.calls, [])

    def test_open_in_browser_hands_the_page_over(self):
        # Ctrl+click renders a page in tabit; right-click sends it out.
        page = self.write("page.html", "<html><body>hi</body></html>")
        Tabit._open_path(self.sink, page, in_browser=True)
        self.assertEqual(self.sink.calls, [("browser", page)])

    def test_only_a_page_gets_the_escape_hatch(self):
        # A binary has no readable source, so the menu must not offer one.
        plain = self.write("notes.md", "hello\n")
        self.assertTrue(Tabit._is_browser_page(self.write("p.html", "<i>x")))
        self.assertFalse(Tabit._is_browser_page(plain))

    def test_an_image_renders_without_a_source_pane(self):
        # A PNG has no text behind it, so it opens as a picture.
        png = os.path.join(self.d.name, "shot.png")
        with open(png, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\0" * 40)
        Tabit._open_path(self.sink, png)
        self.assertEqual(self.sink.calls, [("note", png), ("preview", True)])

    def test_a_pdf_renders_too(self):
        pdf = os.path.join(self.d.name, "a.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        Tabit._open_path(self.sink, pdf)
        self.assertEqual(self.sink.calls, [("note", pdf), ("preview", True)])

    def test_svg_counts_as_source_as_well_as_a_picture(self):
        self.assertTrue(Tabit._has_source("/x/a.svg"))
        self.assertFalse(Tabit._is_binary_render("/x/a.svg"))
        self.assertTrue(Tabit._is_binary_render("/x/a.png"))
        self.assertTrue(Tabit._is_binary_render("/x/a.pdf"))

    def test_a_line_number_is_ignored_on_a_rendered_file(self):
        # `report.html:42` means the page, not line 42 of its markup.
        page = self.write("page.html", "<html><body>hi</body></html>")
        Tabit._open_path(self.sink, page + ":42")
        self.assertEqual(self.sink.calls, [("note", page), ("preview", True)])


class _Pane:
    """Stands in for Gtk widgets: only visibility and no_show_all matter."""

    def __init__(self):
        self.visible = True
        self.no_show_all = False

    def hide(self):
        self.visible = False

    def show(self):
        self.visible = True

    show_all = show

    def set_no_show_all(self, v):
        self.no_show_all = bool(v)

    def grab_focus(self):
        pass

    def get_child1(self):
        return self.source


class TestPreviewSurvivesATabSwitch(unittest.TestCase):
    """Switching tabs runs show_all on the page.

    hide() alone does not survive that, so a rendered page came back as
    plain markup with the Preview button still lit.
    """

    def setUp(self):
        self.source = _Pane()
        self.webview = _Pane()
        self.webview.visible = False
        self.paned = _Pane()
        self.paned.source = self.source

        class Row:
            pass
        self.row = Row()
        self.row.file_path = "/x/report.html"
        self.row.webview = self.webview
        self.row.content_paned = self.paned
        self.row.view = _Pane()
        self.row.preview_on = False
        self.row._preview_pos_set = False
        self.row.preview_btn = None

        class App:
            _note_set_preview = Tabit._note_set_preview
            _opens_full_width = staticmethod(lambda p: True)

            @staticmethod
            def _note_render_preview(_row):
                pass
        self.app = App()

    def switch_away_and_back(self):
        # what Gtk does to the page when the tab is selected again
        for w in (self.source, self.webview):
            if not w.no_show_all:
                w.show_all()

    def test_a_rendered_page_stays_rendered(self):
        self.app._note_set_preview(self.row, True)
        self.switch_away_and_back()
        self.assertFalse(self.source.visible)
        self.assertTrue(self.webview.visible)

    def test_preview_off_stays_off(self):
        self.app._note_set_preview(self.row, True)
        self.app._note_set_preview(self.row, False)
        self.switch_away_and_back()
        self.assertTrue(self.source.visible)
        self.assertFalse(self.webview.visible)


class TestRejoinWrappedPath(unittest.TestCase):
    """An app that wraps its own output writes a real newline.

    VTE matches straight through a fold the terminal made, but not through
    one the app made, so the match ends early. The rejoin is only tried on
    a path that does not resolve, and only kept if the result does.
    """

    def rejoin(self, screen, raw, real=()):
        return _rejoin_wrapped(screen, raw, lambda p: p in set(real))

    def test_a_split_path_comes_back_together(self):
        screen = "saved /tmp/a/b/fig3.\npng\ndone\n"
        self.assertEqual(
            self.rejoin(screen, "/tmp/a/b/fig3.", ["/tmp/a/b/fig3.png"]),
            "/tmp/a/b/fig3.png")

    def test_a_path_split_twice_comes_back_together(self):
        screen = "/tmp/aaa\nbbb\nccc.png\n"
        self.assertEqual(
            self.rejoin(screen, "/tmp/aaa", ["/tmp/aaabbbccc.png"]),
            "/tmp/aaabbbccc.png")

    def test_a_log_line_keeps_its_own_meaning(self):
        # /etc/hosts resolves by itself, so the caller never gets here --
        # but even reaching here, `retry` must not be swallowed.
        screen = "ERROR in /etc/hosts\nretry now\n"
        self.assertEqual(self.rejoin(screen, "/etc/hosts"), "/etc/hosts")

    def test_no_join_when_the_result_is_not_a_file(self):
        screen = "cd /home/chris\nls -la\n"
        self.assertEqual(self.rejoin(screen, "/home/chris"), "/home/chris")

    def test_an_indented_continuation_is_followed(self):
        # Claude Code aligns its wrap under the start of the line, so the
        # rest of the path arrives behind a run of spaces.
        screen = "saved /tmp/a/b/scratchpad/\n      fig3.png\n"
        self.assertEqual(
            self.rejoin(screen, "/tmp/a/b/scratchpad",
                        ["/tmp/a/b/scratchpad/fig3.png"]),
            "/tmp/a/b/scratchpad/fig3.png")

    def test_the_cut_component_is_carried_over(self):
        # The match stops before a trailing slash, which is still on the
        # first line and has to come along.
        screen = "/tmp/x/y/\nz.txt\n"
        self.assertEqual(self.rejoin(screen, "/tmp/x/y", ["/tmp/x/y/z.txt"]),
                         "/tmp/x/y/z.txt")

    def test_a_longer_path_elsewhere_on_screen_is_not_borrowed(self):
        """The same text can be a prefix of another path further up.

        Clicking /home/you in `cd /home/you` must not pick up the tail of
        /home/you/deep/file.txt printed on an earlier line.
        """
        screen = ("saved /home/you/deep/\n      file.txt\n"
                  "cd /home/you\n   ls -la\n")
        self.assertEqual(
            self.rejoin(screen, "/home/you", ["/home/you/deep/file.txt"]),
            "/home/you")

    def test_a_directory_left_by_the_fold_is_completed(self):
        # A fold on a slash leaves a real directory behind, so resolving is
        # not enough to stop -- only landing on a file is.
        screen = "saved /home/you/deep/\n      file.txt\n"
        self.assertEqual(
            self.rejoin(screen, "/home/you/deep", ["/home/you/deep/file.txt"]),
            "/home/you/deep/file.txt")

    def test_tmux_pads_the_row_out_to_the_right_edge(self):
        """tmux clears to the edge, so the fold is behind a run of spaces.

        Captured from a real terminal: the row holding the path is padded
        to the full width before the newline, which is why matching the
        newline straight after the path found nothing.
        """
        screen = ("  saved /home/you/deep/" + " " * 60 + "\n"
                  "     file.txt" + "\n")
        self.assertEqual(
            self.rejoin(screen, "/home/you/deep", ["/home/you/deep/file.txt"]),
            "/home/you/deep/file.txt")

    def test_a_fold_in_the_middle_of_a_name(self):
        # The same capture, folded mid-component rather than on a slash.
        screen = ("     saved /home/you/de" + " " * 60 + "\n"
                  "     ep/file.txt" + " " * 60 + "\n")
        self.assertEqual(
            self.rejoin(screen, "/home/you/de", ["/home/you/deep/file.txt"]),
            "/home/you/deep/file.txt")

    def test_an_indented_next_line_still_needs_the_file(self):
        screen = "see /etc/fstab\n   and reboot\n"
        self.assertEqual(self.rejoin(screen, "/etc/fstab"), "/etc/fstab")

    def test_a_second_occurrence_is_tried_too(self):
        # The same text can be on screen twice; only one of them is split.
        screen = "/tmp/x.\nnope\nagain /tmp/x.\ntxt\n"
        self.assertEqual(self.rejoin(screen, "/tmp/x.", ["/tmp/x.txt"]),
                         "/tmp/x.txt")

    def test_a_raw_that_is_not_on_screen_is_left_alone(self):
        self.assertEqual(self.rejoin("nothing here\n", "/tmp/a"), "/tmp/a")

    def test_it_stops_after_a_few_lines(self):
        screen = "/tmp/a" + "\nx" * 9 + "\n"
        self.assertEqual(self.rejoin(screen, "/tmp/a", ["/tmp/axxxxxxxxx"]),
                         "/tmp/a")


class TestFullWidthRule(unittest.TestCase):
    """What takes the whole tab rather than sharing it with the editor."""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)

    def make(self, name):
        path = os.path.join(self.d.name, name)
        open(path, "w").close()
        return path

    def test_pages_pictures_and_markdown_fill_the_tab(self):
        for name in ("a.html", "a.svg", "a.png", "a.pdf",
                     "a.md", "a.markdown"):
            self.assertTrue(Tabit._opens_full_width(self.make(name)), name)

    def test_plain_text_keeps_the_split(self):
        for name in ("a.txt", "Makefile", "a.py", "a.json"):
            self.assertFalse(Tabit._opens_full_width(self.make(name)), name)

    def test_markdown_is_still_rendered_as_markdown(self):
        # _is_browser_page decides raw-vs-converted in the renderer, so
        # markdown must stay out of it or its source would show as HTML.
        self.assertFalse(Tabit._is_browser_page("/x/a.md"))


class _Ev:
    """Only .y is read off the click."""

    def __init__(self, y):
        self.y = y


class _FakeTerm:
    """A VTE stand-in with the three coordinate systems the real one has.

    get_text_range rows are buffer-absolute and count the primary
    scrollback even while the alternate screen is showing, and its end_col
    stops one short. The scrollbar reads 0 on the alternate screen. Only
    match_check and the cursor tell the truth, and they tell different
    truths: match_check is screen-relative, the cursor is not.
    """

    def __init__(self, rows, cols, base=0, cursor_row=None):
        self.rows, self.cols, self.base = rows, cols, base
        self.cursor_row = len(rows) - 1 if cursor_row is None else cursor_row
        self.tabit_path_tag = 7

    def get_column_count(self):
        return self.cols

    def get_row_count(self):
        return len(self.rows)

    def get_char_height(self):
        return 1

    def get_vadjustment(self):
        class Adj:
            @staticmethod
            def get_value():
                return 0          # stuck at 0 on the alternate screen
        return Adj()

    def get_cursor_position(self):
        return (0, self.base + self.cursor_row)

    def get_style_context(self):
        class Pad:
            top = left = 0

        class St:
            @staticmethod
            def get_state():
                return 0

            @staticmethod
            def get_padding(_state):
                return Pad()
        return St()

    def _screen_row(self, i):
        return self.rows[i] if 0 <= i < len(self.rows) else ""

    def get_text_range(self, start_row, start_col, end_row, end_col, _fn):
        out = []
        for r in range(start_row, end_row + 1):
            row = self._screen_row(r - self.base)
            lo = start_col if r == start_row else 0
            hi = end_col if r == end_row else self.cols
            out.append(row[lo:hi])
        return ("\n".join(out), None)

    def match_check(self, col, screen_row):
        for m in re.finditer(TERM_PATH_PATTERN, self._screen_row(screen_row)):
            if m.start() <= col < m.end():
                return (m.group(0), self.tabit_path_tag)
        return (None, -1)


class TestScreenTextKeepsTheLastColumn(unittest.TestCase):
    """VTE's get_text_range stops before end_col, so it takes the count.

    Passing cols - 1 loses the last character of every row the text fills,
    and a path long enough to fold is exactly what fills a row.
    """

    def test_a_full_row_comes_back_whole(self):
        rows = ["x" * 40, "/tmp/end" + "y" * 32]
        term = _FakeTerm(rows, 40)
        self.assertEqual(Tabit._term_screen_text(term), "\n".join(rows))


class TestFoldedPathInsideTmux(unittest.TestCase):
    """tmux runs on the alternate screen, where row numbering shifts.

    The scrollbar reads 0 there while the screen really sits further down
    the buffer, so reading rows by the scrollbar gets the shell history
    from before tmux started. Clicking a folded path must not care.
    """

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        deep = os.path.join(self.d.name, "a-very-long-directory-name")
        os.makedirs(deep)
        self.path = os.path.join(deep, "report.md")
        with open(self.path, "w") as f:
            f.write("hi\n")
        cut = len(self.path) - len("report.md")
        self.rows = [""] * 30
        self.rows.append("  saved " + self.path[:cut])
        self.rows.append("  " + self.path[cut:])
        self.rows += [""] * 12
        self.term = _FakeTerm(self.rows, 141, base=25, cursor_row=43)

    def test_the_second_row_of_the_fold_opens_the_file(self):
        self.assertEqual(Tabit._path_near_pointer(self.term, _Ev(31)),
                         self.path)

    def test_a_blank_line_under_the_fold_opens_nothing(self):
        # The rejoin has to reach the clicked row, or every line near a
        # folded path would open it.
        self.assertIsNone(Tabit._path_near_pointer(self.term, _Ev(33)))

    def test_the_line_right_under_the_fold_opens_nothing(self):
        self.assertIsNone(Tabit._path_near_pointer(self.term, _Ev(32)))

    def test_the_head_row_still_rejoins_by_text(self):
        self.assertEqual(
            Tabit._rejoin_path(self.term, self.path[:len(self.path)
                                                    - len("report.md") - 1]),
            self.path)


class TestMnemonicLabel(unittest.TestCase):
    """The shortcut key marked in the button text, menu style."""

    def test_the_letter_is_bracketed_where_the_text_has_it(self):
        for text, key, want in (
                ("+ Serial", "S", "+ (S)erial"),
                ("+ AI", "A", "+ (A)I"),
                ("+ Open", "N", "+ Ope(N)"),
                ("+ tmux", "M", "+ t(M)ux"),
        ):
            self.assertEqual(_mnemonic_label(text, key), want)

    def test_a_letter_the_text_lacks_goes_on_the_end(self):
        for text, key, want in (
                ("+ Shell", "T", "+ Shell(T)"),
                ("+ Connect", "K", "+ Connect(K)"),
                ("+ Command", "R", "+ Command(R)"),
        ):
            self.assertEqual(_mnemonic_label(text, key), want)

    def test_the_first_match_wins(self):
        self.assertEqual(_mnemonic_label("+ Command", "M"), "+ Co(M)mand")

    def test_a_key_that_is_not_one_letter_goes_on_the_end(self):
        # Rebinding to F5 or Page Up must not bracket a stray character.
        self.assertEqual(_mnemonic_label("+ tmux", "F5"), "+ tmux(F5)")
        self.assertEqual(_mnemonic_label("+ Open", "Page Up"),
                         "+ Open(Page Up)")

    def test_no_key_leaves_the_text_alone(self):
        self.assertEqual(_mnemonic_label("+ Shell", ""), "+ Shell")


class TestDefaultKeysAreUnique(unittest.TestCase):
    """Two actions on one key means the second never fires."""

    def test_no_two_actions_share_a_default(self):
        seen = {}
        for action, _label, accel, _group in tabit.KEY_ACTIONS:
            self.assertNotIn(
                accel, seen,
                "%s and %s both default to %s" % (seen.get(accel), action,
                                                  accel))
            seen[accel] = action


class TestAgentNotifyRule(unittest.TestCase):
    """Which change of agent status earns a desktop popup."""

    def test_leaving_work_for_a_wait_is_news(self):
        for prev in ("working", "idle", "unknown", None):
            self.assertIsNotNone(Tabit._agent_notify_text(prev, "ready"), prev)
            self.assertIsNotNone(Tabit._agent_notify_text(prev, "blocked"),
                                 prev)

    def test_staying_put_is_not_news(self):
        # The status is re-applied whenever the icon has to be redrawn.
        self.assertIsNone(Tabit._agent_notify_text("ready", "ready"))
        self.assertIsNone(Tabit._agent_notify_text("blocked", "blocked"))

    def test_one_kind_of_waiting_to_the_other_is_not_news(self):
        self.assertIsNone(Tabit._agent_notify_text("blocked", "ready"))
        self.assertIsNone(Tabit._agent_notify_text("ready", "blocked"))

    def test_going_back_to_work_is_not_news(self):
        for status in ("working", "idle", "exited", "unknown"):
            self.assertIsNone(Tabit._agent_notify_text("ready", status),
                              status)


class TestAgentNotifyTitle(unittest.TestCase):
    """Which tab the popup is about: group first, as the sidebar reads."""

    def test_the_group_comes_first(self):
        self.assertEqual(
            Tabit._agent_notify_title("QCA2ECW536", "[claude] ACL 6K test"),
            "QCA2ECW536 · [claude] ACL 6K test")

    def test_an_ungrouped_tab_is_just_its_name(self):
        self.assertEqual(Tabit._agent_notify_title("", "[claude] build"),
                         "[claude] build")

    def test_a_tab_with_no_name_still_says_something(self):
        self.assertEqual(Tabit._agent_notify_title("BUILD", None),
                         "BUILD · AI session")
        self.assertEqual(Tabit._agent_notify_title("", None), "AI session")


class TestNotifyUrgency(unittest.TestCase):
    """The settings string picks how hard the popup insists."""

    def setUp(self):
        if not tabit.HAS_NOTIFY:
            self.skipTest("libnotify not installed")
        from gi.repository import Notify
        self.N = Notify

    def test_each_name_maps_to_its_level(self):
        for name, want in (("critical", "CRITICAL"), ("normal", "NORMAL"),
                           ("low", "LOW")):
            self.assertEqual(Tabit._notify_urgency(name),
                             getattr(self.N.Urgency, want), name)

    def test_anything_unreadable_stays_critical(self):
        # A hand-edited settings.json must not quietly silence the popup
        # an agent blocked on you depends on.
        for name in (None, "", "  ", "URGENT", "true", 5):
            self.assertEqual(Tabit._notify_urgency(name),
                             self.N.Urgency.CRITICAL, repr(name))

    def test_the_name_is_read_loosely(self):
        self.assertEqual(Tabit._notify_urgency("  Normal "),
                         self.N.Urgency.NORMAL)


class TestNotifyDelayOutlastsAPoll(unittest.TestCase):
    """The wait before believing a status change has to outlast one poll."""

    def test_the_delay_clears_the_poll_interval(self):
        # Statuses come from a poll over terminal text. A delay shorter
        # than the gap between polls confirms nothing, because no second
        # reading has happened yet.
        self.assertGreater(Tabit._AGENT_NOTIFY_DELAY_S,
                           Tabit._AGENT_POLL_SEC)


class _ToastSink:
    """Stands in for the window: holds the note list and counts redraws."""

    _AGENT_NOTIFY = Tabit._AGENT_NOTIFY
    _TOAST_SHOWN = Tabit._TOAST_SHOWN
    _follow_toast_status = Tabit._follow_toast_status
    _clear_toasts_for = Tabit._clear_toasts_for
    _toast_shown = Tabit._toast_shown

    def __init__(self, notes=None, expanded=False):
        self._toast_notes = list(notes or [])
        self._toast_expanded = expanded
        self.draws = 0

    def _render_toasts(self):
        self.draws += 1


def _note(row, status="ready"):
    return {"row": row, "status": status, "at": 0.0,
            "sticky": True, "left": None}


class TestShouldArmNotify(unittest.TestCase):
    """Which status changes are worth telling anyone about."""

    def test_stopping_work_is_news(self):
        for status in ("ready", "blocked"):
            self.assertTrue(
                Tabit._should_arm_notify("working", status, False), status)

    def test_going_back_to_work_is_not(self):
        for status in ("working", "idle", "exited", None):
            self.assertFalse(
                Tabit._should_arm_notify("blocked", status, False), status)

    def test_saying_it_twice_is_noise(self):
        # Already told: swapping one kind of waiting for the other adds
        # nothing the tab row is not already showing.
        self.assertFalse(Tabit._should_arm_notify("blocked", "ready", False))
        self.assertFalse(Tabit._should_arm_notify("ready", "blocked", False))

    def test_but_not_while_it_is_still_only_pending(self):
        # Nothing has been shown yet, so the popup has to follow the
        # change instead of being dropped as a repeat -- both used to be
        # lost, the armed one to a status that no longer matched.
        self.assertTrue(Tabit._should_arm_notify("blocked", "ready", True))
        self.assertTrue(Tabit._should_arm_notify("ready", "blocked", True))

    def test_a_pending_one_still_dies_when_it_goes_back_to_work(self):
        self.assertFalse(Tabit._should_arm_notify("ready", "working", True))


class TestFollowToastStatus(unittest.TestCase):
    """A popup says what its tab says, or it goes."""

    def test_it_follows_a_change_worth_a_popup(self):
        row = object()
        sink = _ToastSink([_note(row, "ready")])
        Tabit._follow_toast_status(sink, row, "blocked")
        self.assertEqual(sink._toast_notes[0]["status"], "blocked")
        self.assertEqual(sink.draws, 1)

    def test_back_to_work_takes_the_popup_with_it(self):
        row = object()
        for status in ("working", "idle", "exited"):
            sink = _ToastSink([_note(row, "ready")])
            Tabit._follow_toast_status(sink, row, status)
            self.assertEqual(sink._toast_notes, [], status)
            self.assertEqual(sink.draws, 1, status)

    def test_the_same_status_again_redraws_nothing(self):
        row = object()
        sink = _ToastSink([_note(row, "ready")])
        Tabit._follow_toast_status(sink, row, "ready")
        self.assertEqual(sink.draws, 0)

    def test_a_row_with_no_popup_redraws_nothing(self):
        sink = _ToastSink([_note(object(), "ready")])
        Tabit._follow_toast_status(sink, object(), "blocked")
        self.assertEqual(sink.draws, 0)
        self.assertEqual(len(sink._toast_notes), 1)


class TestClearToastsFor(unittest.TestCase):
    """Opening a tab answers its popup, wherever the popup is."""

    def test_it_drops_by_identity_not_by_equal_contents(self):
        # Two tabs that have got no further than a default title hold
        # equal dicts; only one of them has been opened.
        a, b = object(), object()
        sink = _ToastSink([_note(a), _note(b)])
        Tabit._clear_toasts_for(sink, a)
        self.assertEqual(len(sink._toast_notes), 1)
        self.assertIs(sink._toast_notes[0]["row"], b)

    def test_it_reaches_one_that_has_no_card(self):
        rows = [object() for _ in range(5)]
        sink = _ToastSink([_note(r) for r in rows])
        Tabit._clear_toasts_for(sink, rows[0])   # oldest, so not shown
        self.assertEqual(len(sink._toast_notes), 4)
        self.assertEqual(sink.draws, 1)

    def test_a_tab_with_no_popup_redraws_nothing(self):
        sink = _ToastSink([_note(object())])
        Tabit._clear_toasts_for(sink, object())
        self.assertEqual(sink.draws, 0)

    def test_an_empty_list_is_left_alone(self):
        sink = _ToastSink()
        Tabit._clear_toasts_for(sink, object())
        self.assertEqual(sink.draws, 0)


class TestToastShown(unittest.TestCase):
    """Which notifications have a card."""

    def test_folded_shows_the_newest_few(self):
        notes = [_note(object()) for _ in range(5)]
        sink = _ToastSink(notes)
        self.assertEqual(Tabit._toast_shown(sink), notes[-3:])

    def test_fewer_than_the_limit_all_show(self):
        notes = [_note(object()) for _ in range(2)]
        self.assertEqual(Tabit._toast_shown(_ToastSink(notes)), notes)

    def test_opened_shows_every_one_oldest_first(self):
        notes = [_note(object()) for _ in range(9)]
        sink = _ToastSink(notes, expanded=True)
        self.assertEqual(Tabit._toast_shown(sink), notes)


class TestToastListHeight(unittest.TestCase):
    """How tall a list of cards is, worked out rather than asked for.

    An unshown card reports a size it does not keep, so the opened list
    decides whether it needs a scroller from arithmetic.
    """

    def test_no_cards_no_height(self):
        for n in (0, -1):
            self.assertEqual(Tabit._toast_list_height(n), 0)

    def test_one_card_has_no_gap_under_it(self):
        self.assertEqual(Tabit._toast_list_height(1), Tabit._TOAST_CARD_H)

    def test_the_gaps_are_between_them_only(self):
        for n in (2, 3, 12):
            self.assertEqual(
                Tabit._toast_list_height(n),
                n * Tabit._TOAST_CARD_H + (n - 1) * Tabit._TOAST_GAP, n)

    def test_a_short_list_stays_under_the_open_ceiling(self):
        # Four cards is what the user had open; it must not need a
        # scroller, whose empty part is what lifted the stack before.
        cap = Tabit._toast_expanded_height(1000)
        self.assertLessEqual(Tabit._toast_list_height(4), cap)
        self.assertGreater(Tabit._toast_list_height(12), cap)


class TestToastExpandedHeight(unittest.TestCase):
    """The opened list's height, which is where its constants are used."""

    def test_a_tall_window_still_stops_at_the_cap(self):
        want = (Tabit._TOAST_EXPANDED_MAX - Tabit._TOAST_CTRL_H
                - Tabit._TOAST_GAP)
        for room in (Tabit._TOAST_EXPANDED_MAX, 900, 5000):
            self.assertEqual(Tabit._toast_expanded_height(room), want, room)

    def test_a_short_window_gives_what_it_has(self):
        self.assertEqual(Tabit._toast_expanded_height(200),
                         200 - Tabit._TOAST_CTRL_H - Tabit._TOAST_GAP)

    def test_it_never_opens_to_less_than_one_card(self):
        for room in (0, 30, 60):
            self.assertEqual(Tabit._toast_expanded_height(room),
                             Tabit._TOAST_CARD_H, room)


class TestToastMoreLabel(unittest.TestCase):
    """The control row says what the AI status row below cannot."""

    def test_nothing_hidden_says_nothing(self):
        self.assertEqual(Tabit._toast_more_label(0, 0, False), "")

    def test_it_counts_what_has_no_card(self):
        self.assertEqual(Tabit._toast_more_label(3, 0, False),
                         "3 more \u25b4")

    def test_someone_waiting_is_worth_saying_in_words(self):
        # Words, not the ? glyph: the status row below counts every AI
        # tab with that glyph, and this one counts hidden popups.
        got = Tabit._toast_more_label(5, 2, False)
        self.assertEqual(got, "5 more \u00b7 2 need input \u25b4")
        self.assertNotIn("?", got)
        self.assertNotIn("\u2714", got)

    def test_expanded_offers_the_way_back(self):
        for hidden, blocked in ((0, 0), (5, 2)):
            self.assertEqual(Tabit._toast_more_label(hidden, blocked, True),
                             "Collapse \u25be")

    def test_the_arrow_points_the_way_the_stack_moves(self):
        # Pinned to the foot of the tab list: opening grows upward.
        self.assertTrue(Tabit._toast_more_label(3, 0, False).endswith("\u25b4"))
        self.assertTrue(Tabit._toast_more_label(3, 0, True).endswith("\u25be"))


class TestToastAlign(unittest.TestCase):
    """The in-app popup lines up on whichever side the tab list is on."""

    def test_it_follows_the_list(self):
        self.assertEqual(Tabit._toast_align("left"), Gtk.Align.START)
        self.assertEqual(Tabit._toast_align("right"), Gtk.Align.END)

    def test_a_centered_list_centers_it(self):
        # The list sits between the two content panes there.
        self.assertEqual(Tabit._toast_align("center"), Gtk.Align.CENTER)

    def test_an_unreadable_side_reads_as_left(self):
        # sidebar_position comes from a hand-editable settings file.
        for bad in ("middle", "", None, 3):
            self.assertEqual(Tabit._toast_align(bad), Gtk.Align.START, bad)
