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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tabit import (  # noqa: E402
    TERM_PATH_PATTERN, TERM_PATH_REGEX, TERM_URL_PATTERN, TERM_URL_REGEX,
    Tabit, _is_text_file, _rejoin_wrapped, _split_path_line,
    _stitch_rows,
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
        src = self.write("notes.md", "hello\n")
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
        src = self.write("notes.md", "hello\n")
        Tabit._open_path(self.sink, src)
        self.assertEqual(self.sink.calls, [("note", src)])

    def test_a_zero_stat_file_never_reaches_a_note(self):
        # /proc reports size 0 and still reads 15MB, so the size check has
        # to read rather than trust stat.
        Tabit._open_path(self.sink, "/proc/kallsyms")
        self.assertEqual(self.sink.calls, [("uri", "file:///proc/kallsyms")])

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
            _is_browser_page = staticmethod(lambda p: True)

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


class TestStitchRows(unittest.TestCase):
    """Rows of a folded line, put back the way the text ran.

    VTE only matches inside one row once an app has folded its own output,
    so a click on the second or third row of a long path finds nothing.
    Stitching the rows back gives the click something to match against.
    """

    def test_the_first_row_keeps_its_left_side(self):
        text, _ = _stitch_rows(["  saved /tmp/a", "b/c.txt"])
        self.assertEqual(text, "  saved /tmp/ab/c.txt")

    def test_padding_and_indent_both_go(self):
        text, _ = _stitch_rows(["/tmp/a     ", "     b/c.txt   "])
        self.assertEqual(text, "/tmp/ab/c.txt")

    def test_spans_say_which_row_each_piece_came_from(self):
        text, spans = _stitch_rows(["abc  ", "  de", "f"])
        self.assertEqual(text, "abcdef")
        self.assertEqual(spans, [(0, 3), (3, 5), (5, 6)])

    def test_no_rows(self):
        self.assertEqual(_stitch_rows([]), ("", []))

    def test_a_span_locates_a_match_for_the_clicked_row(self):
        # The rule the click uses: a match must run through the row that
        # was clicked, or a neighbouring line would open it too.
        text, spans = _stitch_rows(["/tmp/aa", "bb.txt", "cd /somewhere"])
        lo, hi = spans[2]
        path_end = len("/tmp/aabb.txt")
        self.assertTrue(path_end <= lo)  # the path stops before that row

