#!/usr/bin/env python3
"""tabit - terminal sessions as vertical tabs on the left.

Each tab is a real terminal (VTE, the same engine xfce4-terminal uses):
a local shell, a serial console (screen.sh / kermit / picocom), or any
command you give it. Click a tab to switch, press its x to close, use
the + buttons to add. When a session's process ends the tab stays
(greyed) so the scrollback is not lost; only the x really closes it.
The set of tabs is remembered and restored (fresh processes) on the
next start.

Shortcuts: Ctrl+Shift+T new shell, Ctrl+Shift+S new serial,
Ctrl+PageUp/PageDown previous/next session,
Ctrl+Shift+C / Ctrl+Shift+V copy / paste.
"""

import fcntl
import glob
import json
import os
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte

SIDEBAR_WIDTH = 200
DEFAULT_BAUD = "115200"
# serial backends shown in the +Serial dialog (first = default)
SERIAL_BACKENDS = ("screen.sh", "kermit", "picocom")
KERMRC = os.path.expanduser("~/senaoenv/kermrc")
SESSIONS_FILE = os.path.join(GLib.get_user_config_dir(), "tabit",
                             "sessions.json")
TERM_FG = "#d5d5df"
TERM_BG = "#101016"

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
.adder button { padding: 4px 8px; font-size: 9pt; color: #9a9aa8; }
.adder button:hover { color: #ececf4; background: rgba(255,255,255,0.11); }
.section { color: #7a7a88; font-size: 8pt; font-weight: 600;
           padding: 12px 8px 3px 8px; }
"""


class Tabit(Gtk.Window):
    def __init__(self):
        super().__init__(title="tabit")
        self.set_default_size(1100, 700)
        self.connect("destroy", Gtk.main_quit)
        self.connect("key-press-event", self._on_window_key)
        self._counter = 0

        self.stack = Gtk.Stack()
        self.listbox = Gtk.ListBox()
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
                              ("+ Command", self._on_add_command)):
            btn = Gtk.Button(label=text)
            btn.connect("clicked", handler)
            adders.pack_start(btn, False, False, 0)
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
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(Gtk.Image.new_from_icon_name(icon_name,
                                                    Gtk.IconSize.MENU),
                       False, False, 0)
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
        row.add(box)
        row.set_tooltip_text(" ".join(argv))
        row.session_label = f"{label} {sub}" if sub else label
        row.title_text = label
        row.sub_text = sub
        row.argv = argv
        row.icon_name = icon_name
        row.page = term  # stack child (was a ScrolledWindow wrapper)
        row.term = term
        row.subtitle = subtitle
        row.dot = dot
        row.dead = False
        self.listbox.add(row)
        self.listbox.show_all()
        self.stack.show_all()
        self.listbox.select_row(row)
        self._save_sessions()

        term.connect("child-exited", self._on_child_exited, row)
        term.connect("contents-changed", self._on_activity, row)
        term.spawn_async(Vte.PtyFlags.DEFAULT, GLib.get_home_dir(), argv,
                         None, GLib.SpawnFlags.SEARCH_PATH, None, None,
                         -1, None, None, None)

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
        self.listbox.remove(row)
        self.stack.remove(row.page)
        row.page.destroy()  # destroys the pty, the child gets SIGHUP
        self._save_sessions()
        rows = self.listbox.get_children()
        if not rows:
            Gtk.main_quit()
        elif was_selected:
            self.listbox.select_row(rows[-1])

    def _on_row_selected(self, _listbox, row):
        if row is None:
            return
        row.dot.hide()
        self.stack.set_visible_child(row.page)
        self.set_title(f"{row.session_label} — tabit")
        row.term.grab_focus()

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

    def _on_add_serial(self, _btn):
        dialog = Gtk.Dialog(title="New serial session", transient_for=self,
                            modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Open", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        combo = Gtk.ComboBoxText.new_with_entry()
        for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            combo.append_text(dev)
        combo.set_active(0)
        baud = Gtk.Entry(text=DEFAULT_BAUD)
        baud.set_activates_default(True)
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
        dialog.set_default_response(Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="e.g. ssh root@192.168.1.1",
                          margin=12, width_chars=40)
        entry.set_activates_default(True)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            cmd = entry.get_text().strip()
            if cmd:
                parts = cmd.split(maxsplit=1)
                self._add_session(parts[0], ["/bin/sh", "-c", cmd],
                                  "utilities-terminal-symbolic",
                                  sub=parts[1] if len(parts) > 1 else None)
        dialog.destroy()

    # --- keyboard -----------------------------------------------------------

    def _on_window_key(self, _window, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        name = (Gdk.keyval_name(event.keyval) or "").lower()
        if ctrl and shift and name == "t":
            self._on_add_shell(None)
            return True
        if ctrl and shift and name == "s":
            self._on_add_serial(None)
            return True
        if ctrl and name in ("page_up", "page_down"):
            rows = self.listbox.get_children()
            current = self.listbox.get_selected_row()
            i = rows.index(current) if current in rows else 0
            i = (i - 1 if name == "page_up" else i + 1) % len(rows)
            self.listbox.select_row(rows[i])
            return True
        return False

    def _on_term_key(self, term, event):
        mask = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if event.state & mask == mask:
            name = (Gdk.keyval_name(event.keyval) or "").lower()
            if name == "c":
                term.copy_clipboard_format(Vte.Format.TEXT)
                return True
            if name == "v":
                term.paste_clipboard()
                return True
        return False

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
