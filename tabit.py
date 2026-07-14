#!/usr/bin/env python3
"""tabit - terminal sessions as vertical tabs on the left.

Each tab is a real terminal (VTE, the same engine xfce4-terminal uses):
a local shell, a serial console (screen.sh / kermit / picocom), an AI
CLI, a GtkSourceView note, or any command. Click a tab to switch,
press its x to close, use the + buttons to add.
 When a session's process ends the tab stays
(greyed) so the scrollback is not lost; only the x really closes it.
The set of tabs is remembered and restored (fresh processes) on the
next start.

Keyboard shortcuts are user-editable (sidebar → Shortcuts…, stored in
~/.config/tabit/keys.json).
"""

import base64
import fcntl
import glob
import html as html_module
import json
import os
import shlex
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkSource", "4")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkSource, Pango, Vte

try:
    gi.require_version("WebKit2", "4.0")
    from gi.repository import WebKit2
    HAS_WEBKIT = True
except (ValueError, ImportError):
    WebKit2 = None
    HAS_WEBKIT = False

try:
    import markdown as markdown_lib
    HAS_MARKDOWN = True
except ImportError:
    markdown_lib = None
    HAS_MARKDOWN = False

SIDEBAR_WIDTH = 200
DEFAULT_BAUD = "115200"
# custom sidebar icon for +AI tabs (stored as icon_name in sessions.json)
ICON_AI = "tabit-ai"
# sessions.json argv[0] for note tabs; argv[1] is path or ""
NOTE_SENTINEL = "__tabit_note__"
ICON_NOTE = "text-editor-symbolic"
# note performance: long lines / huge buffers can freeze GtkSourceView
NOTE_BIG_CHARS = 200_000   # total characters
NOTE_LONG_LINE = 8_000     # any single line
# 16×16 badge with path-drawn "AI" (no font dependency in SVG loaders)
AI_ICON_SVG = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect x="0.5" y="0.5" width="15" height="15" rx="3" ry="3"
        fill="#1a2744" stroke="#7aa2f7" stroke-width="1"/>
  <!-- A -->
  <path fill="#b4ccff"
    d="M2.2 12.2 L4.6 4.2 L5.4 4.2 L7.8 12.2 L6.7 12.2 L6.15 10.3 L3.85 10.3
       L3.3 12.2 Z M4.2 9.2 L5.8 9.2 L5 6.4 Z"/>
  <!-- I -->
  <path fill="#b4ccff"
    d="M9.2 4.2 H13.2 V5.3 H11.85 V11.1 H13.2 V12.2 H9.2 V11.1 H10.55 V5.3 H9.2 Z"/>
</svg>
"""
# serial backends shown in the +Serial dialog (first = default)
SERIAL_BACKENDS = ("screen.sh", "kermit", "picocom")
# default AI CLI list for +AI (user-editable → ~/.config/tabit/ai_clis.json)
# Each entry: {"cli": name, "try": ["args after cli", ...]} then plain cli.
DEFAULT_AI_CLIS = [
    {"cli": "claude", "try": ["--continue"]},
    {"cli": "codex", "try": ["resume --last"]},
    {"cli": "grok", "try": ["--continue"]},
    {"cli": "gemini", "try": ["--resume latest"]},
    {"cli": "antigravity", "try": ["--continue"]},
]
# used when user types a CLI not in the list
DEFAULT_AI_TRY = ["--continue", "resume --last", "--resume latest"]
KERMRC = os.path.expanduser("~/senaoenv/kermrc")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "tabit")
SESSIONS_FILE = os.path.join(CONFIG_DIR, "sessions.json")
KEYS_FILE = os.path.join(CONFIG_DIR, "keys.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
AI_LAST_FILE = os.path.join(CONFIG_DIR, "ai_last.json")
AI_CLIS_FILE = os.path.join(CONFIG_DIR, "ai_clis.json")
TERM_FG = "#d5d5df"
TERM_BG = "#101016"
DEFAULT_SETTINGS = {
    "note_wrap": True,
}

# (action_id, label, default GTK accelerator string)
KEY_ACTIONS = (
    ("new_shell", "New shell", "<Primary><Shift>t"),
    ("new_serial", "New serial", "<Primary><Shift>s"),
    ("new_ai", "New AI session", "<Primary><Shift>a"),
    ("new_note", "New note", "<Primary><Shift>n"),
    ("save_note", "Save note", "<Primary>s"),
    ("note_b64_enc", "Note: Base64 encode", "<Primary><Alt>b"),
    ("note_b64_dec", "Note: Base64 decode", "<Primary><Alt><Shift>b"),
    ("note_json_fmt", "Note: JSON format", "<Primary><Alt>j"),
    ("note_preview", "Note: Markdown preview", "<Primary><Alt>m"),
    ("close_session", "Close session", "<Primary><Shift>w"),
    ("rename_session", "Rename session", "F2"),
    ("prev_session", "Previous session", "<Primary>Page_Up"),
    ("next_session", "Next session", "<Primary>Page_Down"),
    ("move_tab_up", "Move tab up", "<Primary><Shift>Page_Up"),
    ("move_tab_down", "Move tab down", "<Primary><Shift>Page_Down"),
    ("copy", "Copy", "<Primary><Shift>c"),
    ("paste", "Paste", "<Primary><Shift>v"),
)
DEFAULT_KEYS = {a: d for a, _label, d in KEY_ACTIONS}
MOD_MASK = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK |
            Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK |
            Gdk.ModifierType.META_MASK)

CSS = b"""
.sidebar { background-color: #15151c; border-right: 1px solid #2c2c38; }
.sidebar list { background: transparent; }
.sidebar row { border-radius: 6px; border-left: 3px solid transparent;
               padding: 4px 6px 4px 4px; color: #d5d5df; }
.sidebar row:hover { background: rgba(255,255,255,0.11); }
.sidebar row:selected { background: rgba(122,162,247,0.18);
                        border-left-color: #7aa2f7; color: #ececf4; }
.sidebar row.dead label { color: #6a6a78; }
.sidebar row .close { opacity: 0; }
.sidebar row:hover .close, .sidebar row:selected .close { opacity: 1; }
.sidebar button { background: transparent; border: none; border-radius: 6px;
                  padding: 3px 6px; color: #8a8a98; }
.sidebar button:hover { color: #ececf4; background: rgba(255,255,255,0.11); }
.session-sub { color: #7a7a88; font-size: 8pt; }
.activity { color: #7aa2f7; font-size: 8pt; }
/* actions strip: slightly different surface so it is not the tab list */
.adder { background-color: #0e0e14; border-top: 1px solid #2c2c38;
         padding-top: 4px; }
.adder button { padding: 4px 8px; font-size: 9pt; color: #9a9aa8; }
.adder button:hover { color: #ececf4; background: rgba(255,255,255,0.11); }
.section { color: #7a7a88; font-size: 8pt; font-weight: 600;
           padding: 12px 8px 3px 8px; }
/* EventBox on each tab must have a window to receive double/right-click */
.sidebar eventbox { background-color: transparent; }
.note-tools { background-color: #15151c; border-top: 1px solid #2c2c38;
              padding: 4px 6px; }
.note-tools button { padding: 2px 8px; font-size: 9pt; }
"""


class Tabit(Gtk.Window):
    def __init__(self):
        super().__init__(title="tabit")
        self.set_default_size(1100, 700)
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_window_key)
        self._counter = 0
        self._order_seq = 0
        self._save_src = None  # debounced sessions.json write
        self._keys = self._load_keys()  # action -> (keyval, mods)

        self.stack = Gtk.Stack()
        self.listbox = Gtk.ListBox()
        # sort by row._order so reorder is a swap, not remove/insert
        self.listbox.set_sort_func(lambda a, b, _d: a._order - b._order, None)
        self.listbox.connect("row-selected", self._on_row_selected)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.set_size_request(SIDEBAR_WIDTH, -1)
        sidebar.pack_start(self._section("SESSIONS"), False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)
        sidebar.pack_start(scroll, True, True, 0)
        adders = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        adders.get_style_context().add_class("adder")
        for side in ("start", "end", "bottom"):
            getattr(adders, f"set_margin_{side}")(4)
        # icons match the session tab icons
        for text, icon, handler in (
                ("+ Serial", "network-wired-symbolic", self._on_add_serial),
                ("+ Shell", "utilities-terminal-symbolic", self._on_add_shell),
                ("+ AI", ICON_AI, self._on_add_ai),
                ("+ Note", ICON_NOTE, self._on_add_note),
                ("+ Command", "utilities-terminal-symbolic",
                 self._on_add_command)):
            btn = Gtk.Button(label=text)
            btn.set_image(self._session_icon(icon))
            btn.set_always_show_image(True)
            btn.set_image_position(Gtk.PositionType.LEFT)
            btn.connect("clicked", handler)
            adders.pack_start(btn, False, False, 0)
        for text, icon, handler in (
                ("Settings…", "preferences-system-symbolic",
                 self._on_edit_settings),
                ("Shortcuts…", "input-keyboard-symbolic",
                 self._on_edit_keys)):
            btn = Gtk.Button(label=text)
            btn.set_image(Gtk.Image.new_from_icon_name(
                icon, Gtk.IconSize.MENU))
            btn.set_always_show_image(True)
            btn.set_image_position(Gtk.PositionType.LEFT)
            btn.connect("clicked", handler)
            adders.pack_start(btn, False, False, 0)
        sidebar.pack_start(adders, False, False, 0)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(sidebar, False, False, 0)
        root.pack_start(self.stack, True, True, 0)
        self.add(root)

        for s in self._load_sessions():
            try:
                argv = s.get("argv") or []
                if argv and argv[0] == NOTE_SENTINEL:
                    path = argv[1] if len(argv) > 1 and argv[1] else None
                    lab = s.get("label") or None
                    if lab and lab.endswith(" *"):
                        lab = lab[:-2]
                    self._add_note_session(path=path, label=lab,
                                           sub=s.get("sub"))
                else:
                    self._add_session(s["label"], s["argv"], s["icon"],
                                      s.get("sub"))
            except (KeyError, TypeError, OSError):
                continue  # skip broken entries in a hand-edited file
        if not self.listbox.get_children():
            self._on_add_shell(None)

    # --- sessions ---------------------------------------------------------

    @staticmethod
    def _load_sessions():
        try:
            with open(SESSIONS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_sessions(self):
        data = [{"label": r.title_text, "sub": r.sub_text,
                 "argv": r.argv, "icon": r.icon_name}
                for r in self.listbox.get_children()]
        os.makedirs(os.path.dirname(SESSIONS_FILE), exist_ok=True)
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _save_sessions_soon(self):
        # key-repeat can fire many moves; coalesce disk writes
        if self._save_src is not None:
            GLib.source_remove(self._save_src)
        self._save_src = GLib.timeout_add(150, self._save_sessions_now)

    def _save_sessions_now(self):
        self._save_src = None
        self._save_sessions()
        return False

    @staticmethod
    def _session_icon(icon_name):
        if icon_name == ICON_AI:
            try:
                loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
                loader.set_size(16, 16)
                loader.write(AI_ICON_SVG)
                loader.close()
                return Gtk.Image.new_from_pixbuf(loader.get_pixbuf())
            except GLib.Error:
                pass
            return Gtk.Image.new_from_icon_name("applications-science-symbolic",
                                                Gtk.IconSize.MENU)
        return Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)

    def _make_sidebar_row(self, label, sub, icon_name, tooltip):
        """Build the shared left-tab chrome; caller fills row.page / kind."""
        row = Gtk.ListBoxRow()
        hit = Gtk.EventBox()
        hit.set_visible_window(True)
        hit.set_above_child(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(self._session_icon(icon_name), False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label=label)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_xalign(0)
        titles.pack_start(title, False, False, 0)
        subtitle = Gtk.Label(label=sub or "")
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_xalign(0)
        subtitle.get_style_context().add_class("session-sub")
        subtitle.set_no_show_all(not sub)
        titles.pack_start(subtitle, False, False, 0)
        box.pack_start(titles, True, True, 0)
        dot = Gtk.Label(label="●")
        dot.get_style_context().add_class("activity")
        dot.set_no_show_all(True)
        box.pack_start(dot, False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                              Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("close")
        close.connect("clicked", lambda *_: self._close_session(row))
        box.pack_start(close, False, False, 0)
        hit.add(box)
        hit.connect("button-press-event", self._on_tab_button, row)
        row.add(hit)
        row.set_tooltip_text(tooltip or "")
        row.session_label = f"{label} {sub}" if sub else label
        row.title_text = label
        row.title_label = title
        row.sub_text = sub
        row.icon_name = icon_name
        row.subtitle = subtitle
        row.dot = dot
        row.dead = False
        row.kind = "term"
        row.term = None
        row.view = None
        row.buffer = None
        row.file_path = None
        return row

    def _place_tab_row(self, row, page):
        """Insert row under selection, show, select, persist."""
        self._counter += 1
        self.stack.add_named(page, f"session-{self._counter}")
        row.page = page
        selected = self.listbox.get_selected_row()
        if selected is not None:
            row._order = selected._order + 1
            for r in self.listbox.get_children():
                if r._order >= row._order:
                    r._order += 1
        else:
            row._order = self._order_seq
        self._order_seq = max(self._order_seq, row._order) + 1
        self.listbox.add(row)
        self.listbox.invalidate_sort()
        self.listbox.show_all()
        self.stack.show_all()
        self.listbox.select_row(row)
        self._save_sessions()

    def _add_session(self, label, argv, icon_name, sub=None):
        term = Vte.Terminal()
        term.set_scrollback_lines(10000)
        fg, bg = Gdk.RGBA(), Gdk.RGBA()
        fg.parse(TERM_FG)
        bg.parse(TERM_BG)
        term.set_colors(fg, bg, [])
        term.connect("key-press-event", self._on_term_key)

        # VTE scrolls itself; do not wrap in ScrolledWindow.
        row = self._make_sidebar_row(label, sub, icon_name, " ".join(argv))
        row.argv = argv
        row.kind = "term"
        row.term = term
        self._place_tab_row(row, term)

        term.connect("child-exited", self._on_child_exited, row)
        term.connect("contents-changed", self._on_activity, row)
        term.spawn_async(Vte.PtyFlags.DEFAULT, GLib.get_home_dir(), argv,
                         None, GLib.SpawnFlags.SEARCH_PATH, None, None,
                         -1, None, None, None)

    def _add_note_session(self, path=None, label=None, sub=None):
        """GtkSourceView note tab; path=None means untitled."""
        path = path or None
        if path:
            path = os.path.abspath(os.path.expanduser(path))
        view = GtkSource.View()
        view.set_show_line_numbers(True)
        view.set_auto_indent(True)
        view.set_monospace(True)
        view.set_highlight_current_line(True)
        view.set_tab_width(4)
        view.set_insert_spaces_instead_of_tabs(True)
        wrap_on = self._load_settings().get("note_wrap", True)
        view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if wrap_on else Gtk.WrapMode.NONE)
        buf = view.get_buffer()
        scheme_mgr = GtkSource.StyleSchemeManager.get_default()
        scheme = (scheme_mgr.get_scheme("oblivion")
                  or scheme_mgr.get_scheme("solarized-dark")
                  or scheme_mgr.get_scheme("classic"))
        if scheme:
            buf.set_style_scheme(scheme)

        wanted_lang = None
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                text = ""
                sub = sub or f"open failed: {e}"
            buf.begin_not_undoable_action()
            buf.set_text(text)
            buf.end_not_undoable_action()
            buf.set_modified(False)
            wanted_lang = GtkSource.LanguageManager.get_default().guess_language(
                path, None)
            base = os.path.basename(path)
            label = label or base
            sub = sub if sub is not None else os.path.dirname(path)
            tooltip = path
        else:
            path = None
            label = label or "untitled"
            sub = sub if sub is not None else "note"
            tooltip = "untitled note"
            buf.set_modified(False)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)

        content = Gtk.Stack()
        content.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        content.add_named(scroll, "edit")

        webview = None
        if HAS_WEBKIT:
            webview = WebKit2.WebView()
            wset = webview.get_settings()
            wset.set_enable_javascript(False)
            wset.set_allow_file_access_from_file_urls(True)
            content.add_named(webview, "preview")

        row = self._make_sidebar_row(label, sub, ICON_NOTE, tooltip)
        row.argv = [NOTE_SENTINEL, path or ""]
        row.kind = "note"
        row.term = None
        row.view = view
        row.buffer = buf
        row.file_path = path
        row.webview = webview
        row.content_stack = content
        row.preview_on = False
        row._wanted_lang = wanted_lang  # restore after heavy-mode ends
        row._note_heavy = False
        row._tune_src = None
        row.dot.set_no_show_all(True)

        # tools bar at bottom; labels include shortcut hints
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        tools.get_style_context().add_class("note-tools")
        preview_btn = None
        if HAS_WEBKIT:
            acc = self._action_accel_label("note_preview")
            plab = f"Preview  ({acc})" if acc else "Preview"
            preview_btn = Gtk.ToggleButton(label=plab)
            preview_btn.set_tooltip_text(
                "Markdown preview" + (f" — {acc}" if acc else ""))
            tools.pack_start(preview_btn, False, False, 0)
            row.preview_btn = preview_btn
            row._preview_syncing = False

            def on_preview_toggle(btn):
                if getattr(row, "_preview_syncing", False):
                    return
                self._note_set_preview(row, btn.get_active())

            preview_btn.connect("toggled", on_preview_toggle)
        tool_specs = (
            ("Base64 Enc", "note_b64_enc", self._note_b64_encode),
            ("Base64 Dec", "note_b64_dec", self._note_b64_decode),
            ("JSON Format", "note_json_fmt", self._note_json_format),
        )
        for text, action_id, handler in tool_specs:
            acc = self._action_accel_label(action_id)
            lab = f"{text}  ({acc})" if acc else text
            b = Gtk.Button(label=lab)
            b.set_tooltip_text(
                f"{text} — {acc}" if acc else text)
            tools.pack_start(b, False, False, 0)
            b.connect("clicked", lambda _b, fn=handler, r=row: fn(r))
        tip = Gtk.Label(
            label="  (selection, or whole note if none)",
            xalign=0)
        tip.get_style_context().add_class("session-sub")
        tools.pack_start(tip, False, False, 0)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(content, True, True, 0)
        page.pack_start(tools, False, False, 0)
        self._place_tab_row(row, page)

        buf.connect("modified-changed",
                    lambda _b: self._refresh_note_title(row))
        buf.connect("changed", lambda _b: self._note_schedule_tune(row))
        view.connect("key-press-event", self._on_editor_key)
        view.connect("button-press-event", self._on_note_button, row)
        self._note_tune_perf(row)  # apply language only if not huge
        self._refresh_note_title(row)
        view.grab_focus()

    def _note_buffer_stats(self, buf):
        start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        n = len(text)
        max_line = 0
        for line in text.splitlines() or [text]:
            if len(line) > max_line:
                max_line = len(line)
        return n, max_line

    def _note_schedule_tune(self, row):
        if getattr(row, "_tune_src", None):
            GLib.source_remove(row._tune_src)

        def run():
            row._tune_src = None
            self._note_tune_perf(row)
            return False

        row._tune_src = GLib.timeout_add(300, run)

    def _note_tune_perf(self, row):
        """Disable syntax / current-line highlight for huge or long-line notes."""
        if getattr(row, "kind", None) != "note":
            return
        buf, view = row.buffer, row.view
        n, max_line = self._note_buffer_stats(buf)
        heavy = n >= NOTE_BIG_CHARS or max_line >= NOTE_LONG_LINE
        was_heavy = getattr(row, "_note_heavy", False)
        row._note_heavy = heavy
        if heavy:
            lang = buf.get_language()
            if lang is not None:
                row._wanted_lang = lang
            buf.set_language(None)
            view.set_highlight_current_line(False)
        else:
            view.set_highlight_current_line(True)
            if was_heavy or buf.get_language() is None:
                lang = getattr(row, "_wanted_lang", None)
                if lang is None and row.file_path:
                    lang = GtkSource.LanguageManager.get_default().guess_language(
                        row.file_path, None)
                    row._wanted_lang = lang
                if lang is not None:
                    buf.set_language(lang)

    @staticmethod
    def _md_to_html(text, base_path=None):
        """Render markdown to a dark-themed HTML document."""
        if HAS_MARKDOWN:
            exts = ["fenced_code", "tables", "nl2br", "sane_lists"]
            try:
                body = markdown_lib.markdown(
                    text,
                    extensions=exts + ["codehilite"],
                    extension_configs={
                        "codehilite": {
                            "guess_lang": False, "noclasses": True,
                        },
                    },
                )
            except Exception:
                body = markdown_lib.markdown(text, extensions=exts)
        else:
            body = ("<p><em>python3-markdown not installed; "
                    "showing plain text. "
                    "sudo apt install python3-markdown</em></p>"
                    f"<pre>{html_module.escape(text)}</pre>")
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  body {{ background:#101016; color:#d5d5df; font-family:sans-serif;
         padding:16px 22px; line-height:1.55; max-width:52em; }}
  a {{ color:#7aa2f7; }}
  h1,h2,h3,h4 {{ color:#ececf4; border-bottom:1px solid #2c2c38;
                 padding-bottom:.2em; }}
  code, pre {{ background:#1a1a24; border-radius:4px; font-size:.92em; }}
  code {{ padding:.1em .35em; }}
  pre {{ padding:12px; overflow:auto; }}
  pre code {{ padding:0; background:none; }}
  blockquote {{ border-left:3px solid #7aa2f7; margin-left:0;
                padding-left:12px; color:#9a9aa8; }}
  table {{ border-collapse:collapse; }}
  th, td {{ border:1px solid #2c2c38; padding:6px 10px; }}
  hr {{ border:none; border-top:1px solid #2c2c38; }}
  img {{ max-width:100%; }}
</style></head><body>{body}</body></html>"""

    def _note_set_preview(self, row, on):
        if not HAS_WEBKIT or getattr(row, "webview", None) is None:
            return
        on = bool(on)
        row.preview_on = on
        btn = getattr(row, "preview_btn", None)
        if btn is not None and btn.get_active() != on:
            row._preview_syncing = True
            try:
                btn.set_active(on)
            finally:
                row._preview_syncing = False
        if on:
            start, end = row.buffer.get_bounds()
            text = row.buffer.get_text(start, end, True)
            doc = self._md_to_html(text, row.file_path)
            base = "file:///"
            if row.file_path:
                base = "file://" + os.path.dirname(row.file_path) + "/"
            row.webview.load_html(doc, base)
            row.content_stack.set_visible_child_name("preview")
        else:
            row.content_stack.set_visible_child_name("edit")
            row.view.grab_focus()

    def _note_toggle_preview(self, row=None):
        row = row or self.listbox.get_selected_row()
        if row is None or getattr(row, "kind", None) != "note":
            return False
        if not HAS_WEBKIT:
            self._note_msg(
                Gtk.MessageType.WARNING,
                "Markdown preview needs WebKit2",
                "Install gir1.2-webkit2-4.0 and python3-markdown.")
            return False
        self._note_set_preview(row, not getattr(row, "preview_on", False))
        return True

    def _note_get_range(self, row):
        """Text + iters for selection, or whole buffer if nothing selected."""
        buf = row.buffer
        bounds = buf.get_selection_bounds()
        if bounds:
            start, end = bounds
        else:
            start, end = buf.get_bounds()
        text = buf.get_text(start, end, True)
        return text, start, end

    def _note_replace_range(self, row, start, end, new_text):
        buf = row.buffer
        buf.begin_user_action()
        buf.delete(start, end)
        buf.insert(start, new_text)
        buf.end_user_action()
        self._note_tune_perf(row)

    def _note_msg(self, kind, text, secondary=None):
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=kind,
            buttons=Gtk.ButtonsType.OK,
            text=text)
        if secondary:
            dialog.format_secondary_text(secondary)
        dialog.run()
        dialog.destroy()

    def _note_b64_encode(self, row):
        text, start, end = self._note_get_range(row)
        if text == "":
            return
        try:
            out = base64.b64encode(text.encode("utf-8")).decode("ascii")
        except Exception as e:
            self._note_msg(Gtk.MessageType.ERROR, "Base64 encode failed", str(e))
            return
        self._note_replace_range(row, start, end, out)

    def _note_b64_decode(self, row):
        text, start, end = self._note_get_range(row)
        if text == "":
            return
        raw = "".join(text.split())  # allow wrapped base64
        try:
            out = base64.b64decode(raw, validate=False).decode("utf-8")
        except Exception as e:
            self._note_msg(Gtk.MessageType.ERROR, "Base64 decode failed", str(e))
            return
        self._note_replace_range(row, start, end, out)

    def _note_json_format(self, row):
        text, start, end = self._note_get_range(row)
        if text.strip() == "":
            return
        try:
            data = json.loads(text)
            out = json.dumps(data, indent=2, ensure_ascii=False)
            if text.endswith("\n"):
                out += "\n"
        except json.JSONDecodeError as e:
            self._note_msg(
                Gtk.MessageType.ERROR, "Invalid JSON",
                f"Line {e.lineno}, col {e.colno}: {e.msg}")
            return
        self._note_replace_range(row, start, end, out)
        lang = GtkSource.LanguageManager.get_default().get_language("json")
        if lang:
            row._wanted_lang = lang
            # only apply if buffer is not in heavy mode
            if not getattr(row, "_note_heavy", False):
                row.buffer.set_language(lang)

    def _on_note_button(self, view, event, row):
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        menu = Gtk.Menu()
        items = (
            ("Markdown Preview", "note_preview",
             lambda r: self._note_toggle_preview(r)),
            ("Base64 Encode", "note_b64_enc", self._note_b64_encode),
            ("Base64 Decode", "note_b64_dec", self._note_b64_decode),
            ("JSON Format", "note_json_fmt", self._note_json_format),
        )
        for label, action_id, fn in items:
            acc = self._action_accel_label(action_id)
            text = f"{label}    {acc}" if acc else label
            item = Gtk.MenuItem(label=text)
            item.connect("activate", lambda _i, f=fn: f(row))
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _action_accel_label(self, action_id):
        pair = self._keys.get(action_id)
        if not pair:
            return ""
        return self._accel_label(*pair)

    def _refresh_note_title(self, row):
        if getattr(row, "kind", None) != "note":
            return
        base = (os.path.basename(row.file_path) if row.file_path
                else "untitled")
        star = " *" if row.buffer.get_modified() else ""
        row.title_text = base + star
        row.title_label.set_text(row.title_text)
        row.session_label = (f"{row.title_text} {row.sub_text}"
                             if row.sub_text else row.title_text)
        if self.listbox.get_selected_row() is row:
            self.set_title(f"{row.session_label} — tabit")

    def _save_note(self, row, save_as=False):
        if getattr(row, "kind", None) != "note":
            return False
        path = row.file_path
        if save_as or not path:
            chooser = Gtk.FileChooserDialog(
                title="Save note", parent=self,
                action=Gtk.FileChooserAction.SAVE)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                                "Save", Gtk.ResponseType.OK)
            chooser.set_do_overwrite_confirmation(True)
            if path:
                chooser.set_filename(path)
            else:
                chooser.set_current_name("untitled.txt")
            if chooser.run() != Gtk.ResponseType.OK:
                chooser.destroy()
                return False
            path = chooser.get_filename()
            chooser.destroy()
        try:
            start, end = row.buffer.get_bounds()
            text = row.buffer.get_text(start, end, True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            err = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.ERROR,
                buttons=Gtk.ButtonsType.OK,
                text=f"Could not save:\n{e}")
            err.run()
            err.destroy()
            return False
        row.file_path = os.path.abspath(path)
        row.argv = [NOTE_SENTINEL, row.file_path]
        row.sub_text = os.path.dirname(row.file_path)
        row.subtitle.set_text(row.sub_text)
        row.subtitle.set_no_show_all(False)
        row.subtitle.show()
        row.set_tooltip_text(row.file_path)
        lang = GtkSource.LanguageManager.get_default().guess_language(
            row.file_path, None)
        row.buffer.set_language(lang)
        row.buffer.set_modified(False)
        self._refresh_note_title(row)
        self._save_sessions()
        return True

    def _confirm_close_row(self, row):
        """Return True if row may be closed (notes prompt when dirty)."""
        if getattr(row, "kind", None) != "note":
            return True
        if not row.buffer.get_modified():
            return True
        name = row.file_path or "untitled"
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f'"{os.path.basename(name)}" has unsaved changes.')
        dialog.format_secondary_text("Save before closing?")
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Discard", Gtk.ResponseType.REJECT,
                           "Save", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.ACCEPT)
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.CANCEL:
            return False
        if resp == Gtk.ResponseType.ACCEPT:
            return self._save_note(row)
        return True  # discard

    def _on_delete_event(self, *_a):
        for row in list(self.listbox.get_children()):
            if not self._confirm_close_row(row):
                return True  # abort window close
        return False

    def _move_session(self, delta):
        row = self.listbox.get_selected_row()
        if row is None:
            return
        rows = self.listbox.get_children()
        i = rows.index(row)
        j = i + delta
        if j < 0 or j >= len(rows):
            return
        # swap sort keys only — same row stays selected, no focus thrash,
        # so key-repeat can move one step per event.
        other = rows[j]
        row._order, other._order = other._order, row._order
        self.listbox.invalidate_sort()
        self._save_sessions_soon()

    def _on_child_exited(self, _term, _status, row):
        # keep the tab and its scrollback; only the x really closes it
        row.dead = True
        row.get_style_context().add_class("dead")
        row.dot.hide()
        row.subtitle.set_text("exited")
        row.subtitle.set_no_show_all(False)
        row.subtitle.show()

    def _on_activity(self, _term, row):
        if not row.dead and self.listbox.get_selected_row() is not row:
            row.dot.show()

    def _close_session(self, row):
        if row.get_parent() is None:
            return
        if not self._confirm_close_row(row):
            return
        was_selected = self.listbox.get_selected_row() is row
        rows = self.listbox.get_children()
        idx = rows.index(row)
        self.listbox.remove(row)
        self.stack.remove(row.page)
        row.page.destroy()  # term: SIGHUP child; note: destroys view
        self._save_sessions()
        rows = self.listbox.get_children()
        if not rows:
            Gtk.main_quit()
        elif was_selected:
            # focus the next tab (same index after remove); if we closed
            # the last one, fall back to the new last
            self.listbox.select_row(rows[min(idx, len(rows) - 1)])

    def _focus_row_content(self, row):
        if getattr(row, "kind", None) == "note":
            if getattr(row, "preview_on", False):
                return  # WebKit keeps its own focus
            if row.view is not None and not row.view.has_focus():
                row.view.grab_focus()
        elif row.term is not None and not row.term.has_focus():
            row.term.grab_focus()

    def _on_row_selected(self, _listbox, row):
        if row is None:
            return
        row.dot.hide()
        self.stack.set_visible_child(row.page)
        self.set_title(f"{row.session_label} — tabit")
        self._focus_row_content(row)

    def _on_tab_button(self, _hit, event, row):
        """EventBox on each tab: double-click / right-click → rename."""
        if event.button == 1 and event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self.listbox.select_row(row)
            # defer so ListBox finishes its own click handling first
            GLib.idle_add(self._rename_session, row)
            return True
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            self.listbox.select_row(row)
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="Rename…")
            item.connect("activate", lambda *_: self._rename_session(row))
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _rename_session(self, row=None):
        """Rename in a popover bubble anchored to the right of the tab."""
        row = row or self.listbox.get_selected_row()
        if row is None:
            return False
        initial = row.title_text
        if getattr(row, "kind", None) == "note" and initial.endswith(" *"):
            initial = initial[:-2]
        # one popover at a time
        old = getattr(self, "_rename_pop", None)
        if old is not None:
            old.popdown()

        pop = Gtk.Popover.new(row)
        pop.set_position(Gtk.PositionType.RIGHT)
        pop.set_modal(True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin=8)
        entry = Gtk.Entry(text=initial, width_chars=18)
        ok = Gtk.Button(label="OK")
        ok.get_style_context().add_class("suggested-action")
        box.pack_start(entry, True, True, 0)
        box.pack_start(ok, False, False, 0)
        pop.add(box)
        box.show_all()
        self._rename_pop = pop

        def apply(*_a):
            name = entry.get_text().strip()
            if name:
                if getattr(row, "kind", None) == "note":
                    dirty = row.buffer.get_modified()
                    row.title_text = name + (" *" if dirty else "")
                    row.title_label.set_text(row.title_text)
                else:
                    row.title_text = name
                    row.title_label.set_text(name)
                shown = row.title_label.get_text()
                row.session_label = (f"{shown} {row.sub_text}"
                                     if row.sub_text else shown)
                if self.listbox.get_selected_row() is row:
                    self.set_title(f"{row.session_label} — tabit")
                self._save_sessions()
            pop.popdown()

        def on_key(_w, event):
            name = (Gdk.keyval_name(event.keyval) or "").lower()
            if name in ("return", "kp_enter"):
                apply()
                return True
            if name == "escape":
                pop.popdown()
                return True
            return False

        def on_closed(*_a):
            self._rename_pop = None
            if self.listbox.get_selected_row() is row:
                self._focus_row_content(row)

        entry.connect("activate", apply)
        entry.connect("key-press-event", on_key)
        ok.connect("clicked", apply)
        pop.connect("closed", on_closed)
        pop.popup()
        entry.grab_focus()
        entry.select_region(0, -1)
        return False  # for idle_add

    # --- add buttons --------------------------------------------------------

    def _on_add_shell(self, _btn):
        self._add_session("shell", [os.environ.get("SHELL", "/bin/bash")],
                          "utilities-terminal-symbolic")

    def _on_add_note(self, _btn):
        dialog = Gtk.Dialog(title="New note", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Blank note", Gtk.ResponseType.YES,
                           "Open file…", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.YES)
        lab = Gtk.Label(
            label="GtkSourceView note: blank buffer or open a file.",
            margin=12, xalign=0)
        dialog.get_content_area().add(lab)
        dialog.show_all()
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            self._add_note_session()
        elif resp == Gtk.ResponseType.OK:
            chooser = Gtk.FileChooserDialog(
                title="Open note", parent=self,
                action=Gtk.FileChooserAction.OPEN)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                                "Open", Gtk.ResponseType.OK)
            if chooser.run() == Gtk.ResponseType.OK:
                self._add_note_session(path=chooser.get_filename())
            chooser.destroy()

    @staticmethod
    def _serial_argv(backend, dev, rate):
        if backend == "screen.sh":
            return ["screen.sh", dev, rate]
        if backend == "kermit":
            argv = ["kermit", "-l", dev, "-b", rate]
            if os.path.isfile(KERMRC):
                argv += ["-y", KERMRC]
            return argv + ["-c", "-E"]
        return ["picocom", "-b", rate, dev]

    @staticmethod
    def _dialog_enter_is_ok(dialog, response=Gtk.ResponseType.OK):
        """Enter in any field acts like the default Open/OK/Run button."""
        dialog.set_default_response(response)
        ok = dialog.get_widget_for_response(response)
        if ok is not None:
            ok.set_can_default(True)
            dialog.set_default(ok)

        def wire(widget):
            if isinstance(widget, Gtk.Entry):
                widget.set_activates_default(True)
            elif isinstance(widget, Gtk.ComboBox):
                child = widget.get_child()
                if isinstance(child, Gtk.Entry):
                    child.set_activates_default(True)
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    wire(child)

        wire(dialog.get_content_area())

        def on_key(_w, event):
            name = (Gdk.keyval_name(event.keyval) or "").lower()
            if name not in ("return", "kp_enter"):
                return False
            focus = dialog.get_focus()
            # leave multiline editors alone
            if isinstance(focus, Gtk.TextView):
                return False
            dialog.response(response)
            return True

        dialog.connect("key-press-event", on_key)

    def _on_add_serial(self, _btn):
        dialog = Gtk.Dialog(title="New serial session", transient_for=self,
                            modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Open", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        combo = Gtk.ComboBoxText.new_with_entry()
        for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            combo.append_text(dev)
        combo.set_active(0)
        baud = Gtk.Entry(text=DEFAULT_BAUD)
        backend = Gtk.ComboBoxText()
        for name in SERIAL_BACKENDS:
            backend.append_text(name)
        backend.set_active(0)  # screen.sh
        grid.attach(Gtk.Label(label="Device", xalign=0), 0, 0, 1, 1)
        grid.attach(combo, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Baud", xalign=0), 0, 1, 1, 1)
        grid.attach(baud, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Tool", xalign=0), 0, 2, 1, 1)
        grid.attach(backend, 1, 2, 1, 1)
        dialog.get_content_area().add(grid)
        self._dialog_enter_is_ok(dialog)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            dev = (combo.get_active_text() or "").strip()
            rate = baud.get_text().strip() or DEFAULT_BAUD
            tool = backend.get_active_text() or SERIAL_BACKENDS[0]
            if dev:
                self._add_session(os.path.basename(dev),
                                  self._serial_argv(tool, dev, rate),
                                  "network-wired-symbolic",
                                  sub=f"{tool} @{rate}")
        dialog.destroy()

    def _on_add_command(self, _btn):
        dialog = Gtk.Dialog(title="New command session", transient_for=self,
                            modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Run", Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="e.g. ssh root@192.168.1.1",
                          margin=12, width_chars=40)
        dialog.get_content_area().add(entry)
        self._dialog_enter_is_ok(dialog)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            cmd = entry.get_text().strip()
            if cmd:
                parts = cmd.split(maxsplit=1)
                self._add_session(parts[0], ["/bin/sh", "-c", cmd],
                                  "utilities-terminal-symbolic",
                                  sub=parts[1] if len(parts) > 1 else None)
        dialog.destroy()

    @staticmethod
    def _load_ai_last():
        try:
            with open(AI_LAST_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    @staticmethod
    def _save_ai_last(cli, path):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(AI_LAST_FILE, "w") as f:
            json.dump({"cli": cli, "path": path}, f, indent=2)

    @staticmethod
    def _normalize_ai_entry(item):
        """Return {"cli": str, "try": [str, ...]} or None."""
        if isinstance(item, str):
            name = item.strip()
            if not name:
                return None
            # legacy plain string → keep old multi-try behaviour
            return {"cli": name, "try": list(DEFAULT_AI_TRY)}
        if isinstance(item, dict):
            name = str(item.get("cli") or item.get("name") or "").strip()
            if not name:
                return None
            tries = item.get("try") or item.get("resume") or []
            if isinstance(tries, str):
                tries = [t.strip() for t in tries.split("||")]
            tries = [str(t).strip() for t in tries if str(t).strip()]
            return {"cli": name, "try": tries}
        return None

    @classmethod
    def _load_ai_clis(cls):
        """List of {cli, try}; try = args after the CLI name, in order."""
        try:
            with open(AI_CLIS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                out = []
                seen = set()
                for item in data:
                    ent = cls._normalize_ai_entry(item)
                    if ent and ent["cli"] not in seen:
                        seen.add(ent["cli"])
                        out.append(ent)
                if out:
                    return out
        except (OSError, ValueError):
            pass
        return [{"cli": e["cli"], "try": list(e["try"])}
                for e in DEFAULT_AI_CLIS]

    @staticmethod
    def _save_ai_clis(entries):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = [{"cli": e["cli"], "try": list(e.get("try") or [])}
                for e in entries if e.get("cli")]
        with open(AI_CLIS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    @staticmethod
    def _format_try_display(tries):
        return " || ".join(tries) if tries else ""

    @staticmethod
    def _parse_try_display(text):
        # " --continue || resume --last " → list of arg strings
        return [p.strip() for p in text.split("||") if p.strip()]

    @staticmethod
    def _ai_argv(cli, path, tries=None):
        # For each try string T: run `cli T`; if all fail, plain `cli`.
        c = shlex.quote(cli)
        d = shlex.quote(path)
        parts = [f"cd {d} || exit 1"]
        cmds = []
        for t in tries or []:
            t = t.strip()
            if not t:
                continue
            # quote each token so user can write: resume --last
            try:
                tokens = shlex.split(t)
            except ValueError:
                tokens = t.split()
            extra = " ".join(shlex.quote(tok) for tok in tokens)
            cmds.append(f"{c} {extra}")
        cmds.append(f"exec {c}")
        parts.append(" || ".join(cmds))
        return ["/bin/sh", "-c", "; ".join(parts)]

    def _fill_ai_combo(self, combo, entries, prefer=None):
        names = [e["cli"] for e in entries]
        combo.remove_all()
        for name in names:
            combo.append_text(name)
        prefer = prefer or (names[0] if names else "")
        if prefer in names:
            combo.set_active(names.index(prefer))
        elif prefer:
            combo.get_child().set_text(prefer)
            combo.set_active(-1)
        elif names:
            combo.set_active(0)

    def _on_manage_ai_clis(self, parent, combo=None):
        """Edit CLI names and their continue/resume try lists."""
        entries = self._load_ai_clis()
        # columns: cli name, try display ("a || b")
        store = Gtk.ListStore(str, str)
        for e in entries:
            store.append([e["cli"], self._format_try_display(e.get("try"))])

        dialog = Gtk.Dialog(title="Manage AI CLI list", transient_for=parent,
                            modal=True)
        dialog.add_buttons(
            "Reset defaults", Gtk.ResponseType.APPLY,
            "Cancel", Gtk.ResponseType.CANCEL,
            "Save", Gtk.ResponseType.OK)
        dialog.set_default_size(560, 360)
        dialog.set_default_response(Gtk.ResponseType.OK)
        root = dialog.get_content_area()
        root.set_spacing(10)
        for side in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{side}")(12)

        header = Gtk.Label(xalign=0)
        header.set_markup(
            "<b>AI command list</b>\n"
            "<span size='small' foreground='#7a7a88'>"
            "Continue tries: arguments after the CLI, tried left→right with "
            "<tt>||</tt>, then a plain start. "
            "Example: <tt>--continue</tt> or <tt>resume --last</tt>"
            "</span>")
        root.pack_start(header, False, False, 0)

        mid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_min_content_height(200)

        tree = Gtk.TreeView(model=store)
        tree.set_headers_visible(True)
        tree.set_reorderable(True)
        sel = tree.get_selection()
        sel.set_mode(Gtk.SelectionMode.SINGLE)

        def on_cli_edited(_cell, path, text):
            text = text.strip()
            if not text:
                return
            it = store.get_iter(path)
            for i, row in enumerate(store):
                if row[0] == text and str(i) != path:
                    return
            store[it][0] = text

        def on_try_edited(_cell, path, text):
            store[store.get_iter(path)][1] = text.strip()

        cell_cli = Gtk.CellRendererText(editable=True)
        cell_cli.set_property("ypad", 6)
        cell_cli.set_property("xpad", 8)
        cell_cli.connect("edited", on_cli_edited)
        col_cli = Gtk.TreeViewColumn("CLI", cell_cli, text=0)
        col_cli.set_min_width(120)
        col_cli.set_resizable(True)
        tree.append_column(col_cli)

        cell_try = Gtk.CellRendererText(editable=True)
        cell_try.set_property("ypad", 6)
        cell_try.set_property("xpad", 8)
        cell_try.connect("edited", on_try_edited)
        col_try = Gtk.TreeViewColumn("Continue tries ( || separated)",
                                    cell_try, text=1)
        col_try.set_expand(True)
        col_try.set_resizable(True)
        tree.append_column(col_try)

        scroll.add(tree)
        mid.pack_start(scroll, True, True, 0)

        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        btn_up = Gtk.Button.new_from_icon_name("go-up-symbolic",
                                               Gtk.IconSize.BUTTON)
        btn_down = Gtk.Button.new_from_icon_name("go-down-symbolic",
                                                 Gtk.IconSize.BUTTON)
        btn_del = Gtk.Button.new_from_icon_name("list-remove-symbolic",
                                                Gtk.IconSize.BUTTON)
        for b, tip in ((btn_up, "Move up"), (btn_down, "Move down"),
                       (btn_del, "Remove")):
            b.set_tooltip_text(tip)
            side.pack_start(b, False, False, 0)
        mid.pack_start(side, False, False, 0)
        root.pack_start(mid, True, True, 0)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        add_cli = Gtk.Entry()
        add_cli.set_placeholder_text("CLI, e.g. claude")
        add_cli.set_width_chars(12)
        add_try = Gtk.Entry()
        add_try.set_placeholder_text("Continue tries, e.g. --continue")
        add_try.set_hexpand(True)
        btn_add = Gtk.Button.new_from_icon_name("list-add-symbolic",
                                                Gtk.IconSize.BUTTON)
        btn_add.set_label("Add")
        btn_add.set_always_show_image(True)
        add_row.pack_start(add_cli, False, False, 0)
        add_row.pack_start(add_try, True, True, 0)
        add_row.pack_start(btn_add, False, False, 0)
        root.pack_start(add_row, False, False, 0)

        foot = Gtk.Label(
            label="Saved to ~/.config/tabit/ai_clis.json  ·  "
                  "Runs: cd <path> && (cli <try1> || cli <try2> || … || cli)",
            xalign=0)
        foot.get_style_context().add_class("session-sub")
        root.pack_start(foot, False, False, 0)

        def selected_iter():
            _model, it = sel.get_selected()
            return it

        def on_up(_b):
            it = selected_iter()
            if it is None:
                return
            path = store.get_path(it)
            if path[0] == 0:
                return
            store.swap(it, store.get_iter((path[0] - 1,)))

        def on_down(_b):
            it = selected_iter()
            if it is None:
                return
            path = store.get_path(it)
            if path[0] >= store.iter_n_children(None) - 1:
                return
            store.swap(it, store.get_iter((path[0] + 1,)))

        def on_del(_b):
            it = selected_iter()
            if it is not None:
                store.remove(it)

        def on_add(_b=None):
            name = add_cli.get_text().strip()
            if not name:
                return
            for row in store:
                if row[0] == name:
                    add_cli.set_text("")
                    add_try.set_text("")
                    return
            store.append([name, add_try.get_text().strip()])
            add_cli.set_text("")
            add_try.set_text("")
            n = store.iter_n_children(None)
            last = store.get_iter((n - 1,))
            sel.select_iter(last)
            tree.scroll_to_cell(store.get_path(last), None, False, 0, 0)

        def refill(defaults):
            store.clear()
            for e in defaults:
                store.append([e["cli"], self._format_try_display(e.get("try"))])

        def store_to_entries():
            out, seen = [], set()
            for row in store:
                name = row[0].strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                out.append({
                    "cli": name,
                    "try": self._parse_try_display(row[1] or ""),
                })
            return out or [{"cli": e["cli"], "try": list(e["try"])}
                           for e in DEFAULT_AI_CLIS]

        btn_up.connect("clicked", on_up)
        btn_down.connect("clicked", on_down)
        btn_del.connect("clicked", on_del)
        btn_add.connect("clicked", on_add)
        add_cli.connect("activate", on_add)
        add_try.connect("activate", on_add)

        dialog.show_all()
        while True:
            resp = dialog.run()
            if resp == Gtk.ResponseType.APPLY:
                refill(DEFAULT_AI_CLIS)
                continue
            if resp == Gtk.ResponseType.OK:
                ordered = store_to_entries()
                self._save_ai_clis(ordered)
                if combo is not None:
                    cur = (combo.get_active_text() or "").strip()
                    self._fill_ai_combo(combo, ordered, prefer=cur)
            break
        dialog.destroy()

    def _on_add_ai(self, _btn):
        last = self._load_ai_last()
        entries = self._load_ai_clis()
        dialog = Gtk.Dialog(title="New AI session", transient_for=self,
                            modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Open", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)

        cli = Gtk.ComboBoxText.new_with_entry()
        self._fill_ai_combo(cli, entries, prefer=last.get("cli"))
        manage = Gtk.Button(label="Edit list…")
        manage.connect("clicked",
                       lambda *_: self._on_manage_ai_clis(dialog, cli))
        cli_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        cli_box.pack_start(cli, True, True, 0)
        cli_box.pack_start(manage, False, False, 0)

        path_default = last.get("path") or GLib.get_home_dir()
        path = Gtk.Entry(text=path_default, width_chars=36)
        browse = Gtk.Button(label="Browse…")

        def on_browse(_b):
            chooser = Gtk.FileChooserDialog(
                title="Working directory", parent=dialog,
                action=Gtk.FileChooserAction.SELECT_FOLDER)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                                "Select", Gtk.ResponseType.OK)
            if os.path.isdir(path.get_text()):
                chooser.set_current_folder(path.get_text())
            if chooser.run() == Gtk.ResponseType.OK:
                path.set_text(chooser.get_filename())
            chooser.destroy()

        browse.connect("clicked", on_browse)
        path_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        path_box.pack_start(path, True, True, 0)
        path_box.pack_start(browse, False, False, 0)

        try_hint = Gtk.Label(xalign=0)
        try_hint.get_style_context().add_class("session-sub")

        def update_try_hint(*_a):
            tool = (cli.get_active_text() or "").strip()
            tries = None
            for e in self._load_ai_clis():
                if e["cli"] == tool:
                    tries = e.get("try") or []
                    break
            if tries is None:
                tries = list(DEFAULT_AI_TRY)
            if tries:
                chain = " → ".join(tries) + " → plain"
            else:
                chain = "plain start only"
            try_hint.set_text(f"Will try: {chain}")

        cli.connect("changed", update_try_hint)
        update_try_hint()

        grid.attach(Gtk.Label(label="CLI", xalign=0), 0, 0, 1, 1)
        grid.attach(cli_box, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Path", xalign=0), 0, 1, 1, 1)
        grid.attach(path_box, 1, 1, 1, 1)
        grid.attach(try_hint, 0, 2, 2, 1)
        dialog.get_content_area().add(grid)
        self._dialog_enter_is_ok(dialog)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            tool = (cli.get_active_text() or "").strip()
            cwd = (path.get_text() or "").strip() or GLib.get_home_dir()
            cwd = os.path.expanduser(cwd)
            if tool:
                tries = None
                for e in self._load_ai_clis():
                    if e["cli"] == tool:
                        tries = e.get("try") or []
                        break
                if tries is None:
                    tries = list(DEFAULT_AI_TRY)
                short = cwd if len(cwd) <= 28 else "…" + cwd[-27:]
                self._add_session(tool, self._ai_argv(tool, cwd, tries),
                                  ICON_AI, sub=short)
                self._save_ai_last(tool, cwd)
        dialog.destroy()

    # --- keyboard -----------------------------------------------------------

    @staticmethod
    def _parse_accel(accel):
        key, mods = Gtk.accelerator_parse(accel)
        if key == 0:
            return None
        return (key, Gdk.ModifierType(mods))

    @staticmethod
    def _accel_label(key, mods):
        # Human text like "Ctrl+Shift+T" (not GTK's <Primary><Shift>t).
        # Primary is GTK's portable name for Ctrl on Linux / Cmd on macOS.
        return Gtk.accelerator_get_label(key, mods) or "(none)"

    @classmethod
    def _accel_label_from_name(cls, accel):
        pair = cls._parse_accel(accel)
        return cls._accel_label(*pair) if pair else "(none)"

    @staticmethod
    def _norm_keyval(keyval):
        name = Gdk.keyval_name(keyval) or ""
        if name.startswith("KP_"):
            base = Gdk.keyval_from_name(name[3:])
            if base:
                keyval = base
        return Gdk.keyval_to_lower(keyval)

    @classmethod
    def _load_keys(cls):
        raw = dict(DEFAULT_KEYS)
        try:
            with open(KEYS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULT_KEYS and isinstance(v, str):
                        raw[k] = v
        except (OSError, ValueError):
            pass
        parsed = {}
        for action, accel in raw.items():
            pair = cls._parse_accel(accel)
            if pair:
                parsed[action] = pair
            else:
                parsed[action] = cls._parse_accel(DEFAULT_KEYS[action])
        return parsed

    def _save_keys(self, accel_map):
        """accel_map: action -> accelerator string"""
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(KEYS_FILE, "w") as f:
            json.dump(accel_map, f, indent=2)
        self._keys = {}
        for action, accel in accel_map.items():
            pair = self._parse_accel(accel)
            if pair:
                self._keys[action] = pair

    def _match_action(self, event):
        key = self._norm_keyval(event.keyval)
        mods = event.state & MOD_MASK
        for action, (want_key, want_mods) in self._keys.items():
            if key == self._norm_keyval(want_key) and mods == (want_mods & MOD_MASK):
                return action
        return None

    def _run_action(self, action, term=None):
        row = self.listbox.get_selected_row()
        if action == "new_shell":
            self._on_add_shell(None)
        elif action == "new_serial":
            self._on_add_serial(None)
        elif action == "new_ai":
            self._on_add_ai(None)
        elif action == "new_note":
            self._on_add_note(None)
        elif action == "save_note":
            if row is not None and getattr(row, "kind", None) == "note":
                self._save_note(row)
            else:
                return False
        elif action == "note_b64_enc":
            if row is not None and getattr(row, "kind", None) == "note":
                self._note_b64_encode(row)
            else:
                return False
        elif action == "note_b64_dec":
            if row is not None and getattr(row, "kind", None) == "note":
                self._note_b64_decode(row)
            else:
                return False
        elif action == "note_json_fmt":
            if row is not None and getattr(row, "kind", None) == "note":
                self._note_json_format(row)
            else:
                return False
        elif action == "note_preview":
            return self._note_toggle_preview(row)
        elif action == "close_session":
            if row is not None:
                self._close_session(row)
        elif action == "rename_session":
            self._rename_session()
        elif action == "move_tab_up":
            self._move_session(-1)
        elif action == "move_tab_down":
            self._move_session(1)
        elif action in ("prev_session", "next_session"):
            rows = self.listbox.get_children()
            if not rows:
                return True
            current = row
            i = rows.index(current) if current in rows else 0
            i = (i - 1 if action == "prev_session" else i + 1) % len(rows)
            self.listbox.select_row(rows[i])
        elif action == "copy":
            if row is not None and getattr(row, "kind", None) == "note":
                row.view.emit("copy-clipboard")
            else:
                t = term or (row.term if row is not None else None)
                if t:
                    t.copy_clipboard_format(Vte.Format.TEXT)
        elif action == "paste":
            if row is not None and getattr(row, "kind", None) == "note":
                row.view.emit("paste-clipboard")
            else:
                t = term or (row.term if row is not None else None)
                if t:
                    t.paste_clipboard()
        else:
            return False
        return True

    def _handle_shortcut(self, event, term=None):
        """Shared by window, terminal, and note editor."""
        if event.type != Gdk.EventType.KEY_PRESS:
            return False
        action = self._match_action(event)
        if not action:
            return False
        return self._run_action(action, term=term)

    def _on_window_key(self, _window, event):
        # When VTE has focus the term handler already runs; do not fire twice.
        if isinstance(self.get_focus(), Vte.Terminal):
            return False
        return self._handle_shortcut(event)

    def _on_term_key(self, term, event):
        return self._handle_shortcut(event, term=term)

    def _on_editor_key(self, _view, event):
        return self._handle_shortcut(event)

    @staticmethod
    def _load_settings():
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE) as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                data.update(raw)
        except (OSError, ValueError):
            pass
        return data

    @staticmethod
    def _save_settings(data):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cur = Tabit._load_settings()
        cur.update(data)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(cur, f, indent=2)

    def _apply_note_wrap_setting(self, wrap_on):
        mode = Gtk.WrapMode.WORD_CHAR if wrap_on else Gtk.WrapMode.NONE
        for row in self.listbox.get_children():
            if getattr(row, "kind", None) == "note" and row.view is not None:
                row.view.set_wrap_mode(mode)

    def _on_edit_settings(self, _btn):
        s = self._load_settings()
        dialog = Gtk.Dialog(title="Settings", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Notes</b>")
        wrap = Gtk.CheckButton(label="Word wrap notes (recommended)")
        wrap.set_active(bool(s.get("note_wrap", True)))
        wrap.set_tooltip_text(
            "When off, very long lines may lag. Default is on.")
        hint = Gtk.Label(
            label="Stored in ~/.config/tabit/settings.json",
            xalign=0)
        hint.get_style_context().add_class("session-sub")
        box.pack_start(head, False, False, 0)
        box.pack_start(wrap, False, False, 0)
        box.pack_start(hint, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self._save_settings({"note_wrap": wrap.get_active()})
            self._apply_note_wrap_setting(wrap.get_active())
        dialog.destroy()

    def _on_edit_keys(self, _btn):
        dialog = Gtk.Dialog(title="Keyboard shortcuts", transient_for=self,
                            modal=True)
        dialog.add_buttons("Reset defaults", Gtk.ResponseType.APPLY,
                           "Cancel", Gtk.ResponseType.CANCEL,
                           "Save", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=8, column_spacing=12, margin=12)
        # store GTK accel names; show human labels on buttons
        accels = {}
        for action, _label, default in KEY_ACTIONS:
            key, mods = self._keys.get(action, self._parse_accel(default))
            accels[action] = Gtk.accelerator_name(key, mods)

        buttons = {}
        for i, (action, label, _default) in enumerate(KEY_ACTIONS):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            btn = Gtk.Button(label=self._accel_label_from_name(accels[action]))
            btn.set_hexpand(True)
            buttons[action] = btn
            grid.attach(btn, 1, i, 1, 1)

            def capture(_b, act=action, b=btn):
                b.set_label("Press a key…")
                # grab keyboard on the dialog for one key
                def on_key(_w, event):
                    if event.type != Gdk.EventType.KEY_PRESS:
                        return True
                    name = (Gdk.keyval_name(event.keyval) or "").lower()
                    if name in ("escape",):
                        b.set_label(self._accel_label_from_name(accels[act]))
                        dialog.disconnect(handler_id)
                        return True
                    if name in ("control_l", "control_r", "shift_l", "shift_r",
                                "alt_l", "alt_r", "super_l", "super_r",
                                "meta_l", "meta_r"):
                        return True  # wait for the real key
                    mods = event.state & MOD_MASK
                    key = event.keyval
                    accels[act] = Gtk.accelerator_name(key, mods)
                    b.set_label(self._accel_label(key, mods))
                    dialog.disconnect(handler_id)
                    return True
                handler_id = dialog.connect("key-press-event", on_key)

            btn.connect("clicked", capture)

        hint = Gtk.Label(
            label="Click a shortcut, then press the new key combo.\n"
                  "Esc cancels capture. Stored in ~/.config/tabit/keys.json",
            xalign=0)
        hint.set_margin_top(8)
        box = dialog.get_content_area()
        box.add(grid)
        box.add(hint)
        dialog.show_all()

        while True:
            resp = dialog.run()
            if resp == Gtk.ResponseType.APPLY:
                for action, _label, default in KEY_ACTIONS:
                    accels[action] = default
                    buttons[action].set_label(self._accel_label_from_name(default))
                continue
            if resp == Gtk.ResponseType.OK:
                self._save_keys({a: accels[a] for a, _l, _d in KEY_ACTIONS})
            break
        dialog.destroy()

    # --- misc ---------------------------------------------------------------

    @staticmethod
    def _section(text):
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        label.get_style_context().add_class("section")
        return label


def _ensure_user_path():
    # Desktop launch gives a stripped PATH. +Command runs non-interactive sh
    # (no .bashrc), so tools in ~/.local/bin (e.g. screen.sh) are missing.
    local_bin = os.path.join(GLib.get_home_dir(), ".local", "bin")
    path = os.environ.get("PATH", "")
    if local_bin not in path.split(":"):
        os.environ["PATH"] = local_bin + (":" + path if path else "")


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    GLib.set_prgname("tabit")
    _ensure_user_path()

    # one instance is enough; the lock dies with the process
    lock = open(os.path.join(GLib.get_user_runtime_dir(), "tabit.lock"), "w")
    try:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("tabit is already running")

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    Tabit().show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
