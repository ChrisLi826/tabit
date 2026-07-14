#!/usr/bin/env python3
"""tabit - terminal sessions as vertical tabs on the left.

Each tab is a real terminal (VTE, the same engine xfce4-terminal uses):
a local shell, a serial console (screen.sh / kermit / picocom), or any
command you give it. Click a tab to switch, press its x to close, use
the + buttons to add. When a session's process ends the tab stays
(greyed) so the scrollback is not lost; only the x really closes it.
The set of tabs is remembered and restored (fresh processes) on the
next start.

Keyboard shortcuts are user-editable (sidebar → Shortcuts…, stored in
~/.config/tabit/keys.json).
"""

import fcntl
import glob
import json
import os
import shlex
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango, Vte

SIDEBAR_WIDTH = 200
DEFAULT_BAUD = "115200"
# custom sidebar icon for +AI tabs (stored as icon_name in sessions.json)
ICON_AI = "tabit-ai"
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
AI_LAST_FILE = os.path.join(CONFIG_DIR, "ai_last.json")
AI_CLIS_FILE = os.path.join(CONFIG_DIR, "ai_clis.json")
TERM_FG = "#d5d5df"
TERM_BG = "#101016"

# (action_id, label, default GTK accelerator string)
KEY_ACTIONS = (
    ("new_shell", "New shell", "<Primary><Shift>t"),
    ("new_serial", "New serial", "<Primary><Shift>s"),
    ("new_ai", "New AI session", "<Primary><Shift>a"),
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
"""


class Tabit(Gtk.Window):
    def __init__(self):
        super().__init__(title="tabit")
        self.set_default_size(1100, 700)
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
        for text, handler in (("+ Serial", self._on_add_serial),
                              ("+ Shell", self._on_add_shell),
                              ("+ AI", self._on_add_ai),
                              ("+ Command", self._on_add_command)):
            btn = Gtk.Button(label=text)
            btn.connect("clicked", handler)
            adders.pack_start(btn, False, False, 0)
        keys_btn = Gtk.Button(label="Shortcuts…")
        keys_btn.connect("clicked", self._on_edit_keys)
        adders.pack_start(keys_btn, False, False, 0)
        sidebar.pack_start(adders, False, False, 0)

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(sidebar, False, False, 0)
        root.pack_start(self.stack, True, True, 0)
        self.add(root)

        for s in self._load_sessions():
            try:
                self._add_session(s["label"], s["argv"], s["icon"],
                                  s.get("sub"))
            except (KeyError, TypeError):
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

    def _add_session(self, label, argv, icon_name, sub=None):
        term = Vte.Terminal()
        term.set_scrollback_lines(10000)
        fg, bg = Gdk.RGBA(), Gdk.RGBA()
        fg.parse(TERM_FG)
        bg.parse(TERM_BG)
        term.set_colors(fg, bg, [])
        term.connect("key-press-event", self._on_term_key)

        # VTE scrolls itself; a ScrolledWindow around it draws a spurious
        # dashed bar at the bottom (horizontal scrollbar chrome).
        self._counter += 1
        self.stack.add_named(term, f"session-{self._counter}")

        row = Gtk.ListBoxRow()
        # EventBox with its own GdkWindow: clicks on Labels land here.
        # above_child=False → close button still receives its own clicks.
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
        row.set_tooltip_text(" ".join(argv))
        row.session_label = f"{label} {sub}" if sub else label
        row.title_text = label
        row.title_label = title
        row.sub_text = sub
        row.argv = argv
        row.icon_name = icon_name
        row.page = term  # stack child (was a ScrolledWindow wrapper)
        row.term = term
        row.subtitle = subtitle
        row.dot = dot
        row.dead = False
        # insert under the current tab (not always at the end)
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

        term.connect("child-exited", self._on_child_exited, row)
        term.connect("contents-changed", self._on_activity, row)
        term.spawn_async(Vte.PtyFlags.DEFAULT, GLib.get_home_dir(), argv,
                         None, GLib.SpawnFlags.SEARCH_PATH, None, None,
                         -1, None, None, None)

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
        was_selected = self.listbox.get_selected_row() is row
        rows = self.listbox.get_children()
        idx = rows.index(row)
        self.listbox.remove(row)
        self.stack.remove(row.page)
        row.page.destroy()  # destroys the pty, the child gets SIGHUP
        self._save_sessions()
        rows = self.listbox.get_children()
        if not rows:
            Gtk.main_quit()
        elif was_selected:
            # focus the next tab (same index after remove); if we closed
            # the last one, fall back to the new last
            self.listbox.select_row(rows[min(idx, len(rows) - 1)])

    def _on_row_selected(self, _listbox, row):
        if row is None:
            return
        row.dot.hide()
        self.stack.set_visible_child(row.page)
        self.set_title(f"{row.session_label} — tabit")
        if not row.term.has_focus():
            row.term.grab_focus()

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
        # one popover at a time
        old = getattr(self, "_rename_pop", None)
        if old is not None:
            old.popdown()

        pop = Gtk.Popover.new(row)
        pop.set_position(Gtk.PositionType.RIGHT)
        pop.set_modal(True)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin=8)
        entry = Gtk.Entry(text=row.title_text, width_chars=18)
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
                row.title_text = name
                row.title_label.set_text(name)
                row.session_label = (f"{name} {row.sub_text}"
                                     if row.sub_text else name)
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
                row.term.grab_focus()

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
        if action == "new_shell":
            self._on_add_shell(None)
        elif action == "new_serial":
            self._on_add_serial(None)
        elif action == "new_ai":
            self._on_add_ai(None)
        elif action == "close_session":
            row = self.listbox.get_selected_row()
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
            current = self.listbox.get_selected_row()
            i = rows.index(current) if current in rows else 0
            i = (i - 1 if action == "prev_session" else i + 1) % len(rows)
            self.listbox.select_row(rows[i])
        elif action == "copy":
            t = term or (self.listbox.get_selected_row() and
                         self.listbox.get_selected_row().term)
            if t:
                t.copy_clipboard_format(Vte.Format.TEXT)
        elif action == "paste":
            t = term or (self.listbox.get_selected_row() and
                         self.listbox.get_selected_row().term)
            if t:
                t.paste_clipboard()
        else:
            return False
        return True

    def _handle_shortcut(self, event, term=None):
        """Shared by window and terminal so bindings work while VTE has focus."""
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
