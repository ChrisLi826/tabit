#!/usr/bin/env python3
"""tabit - vertical window tabs and pinned files on the left screen edge.

Works on any X11 window manager that follows EWMH (XFCE, GNOME, KDE, MATE...).
Left-click a tab to focus or minimize it, middle-click to close it.
Drag a file onto the sidebar to pin it; right-click a pin to remove it.
Right-click empty space to quit.
"""

import fcntl
import json
import os
import signal
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Wnck", "3.0")
from gi.repository import Gdk, GdkX11, Gio, GLib, Gtk, Pango, Wnck

WIDTH = 220
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "tabit")
PINS_FILE = os.path.join(CONFIG_DIR, "pins.json")

CSS = b"""
window { background-color: #15151c; border-right: 1px solid #2c2c38; }
button { background: transparent; border: none; border-radius: 6px;
         border-left: 3px solid transparent;
         padding: 4px 8px 4px 5px; color: #d5d5df; outline-width: 0; }
button:hover { background: rgba(255,255,255,0.11); }
button:active { background: rgba(255,255,255,0.16); }
button.active { background: rgba(122,162,247,0.18);
                border-left-color: #7aa2f7;
                color: #ececf4; font-weight: 600; }
button.active:hover { background: rgba(122,162,247,0.30); }
button.minimized { opacity: 0.48; }
.section { color: #7a7a88; font-size: 8pt; font-weight: 600;
           letter-spacing: 1.5px; padding: 12px 8px 3px 8px; }
"""


class Tabit(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()

        display = Gdk.Display.get_default()
        # a left strut can only reserve the left edge of the whole X screen,
        # so the sidebar lives on the leftmost monitor
        monitors = [display.get_monitor(i) for i in range(display.get_n_monitors())]
        monitor = min(monitors, key=lambda m: m.get_geometry().x)
        self.geo = monitor.get_geometry()
        self.scale = monitor.get_scale_factor()
        self.set_size_request(WIDTH, self.geo.height)

        self.pins = self._load_pins()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(6)
        self.pin_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.win_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.win_box)
        outer.pack_start(self.pin_box, False, False, 0)
        outer.pack_start(scroll, True, True, 0)
        self.add(outer)

        # accept files dragged from a file manager
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("button-press-event", self._on_bg_press)
        self.connect("map-event", self._on_map)
        self.connect("destroy", Gtk.main_quit)

        self._pending = False
        self.wnck = Wnck.Screen.get_default()
        self.wnck.force_update()
        for sig in ("window-opened", "window-closed",
                    "active-window-changed", "active-workspace-changed"):
            self.wnck.connect(sig, self._queue_refresh)
        self.wnck.connect("window-opened", lambda _s, w: self._hook(w))
        for w in self.wnck.get_windows():
            self._hook(w)

        self._refresh_pins()
        self._refresh_windows()

    # --- screen edge -----------------------------------------------------

    def _on_map(self, *_args):
        self.move(self.geo.x, self.geo.y)
        s = self.scale
        left = (self.geo.x + WIDTH) * s
        y0 = self.geo.y * s
        y1 = (self.geo.y + self.geo.height) * s - 1
        partial = [left, 0, 0, 0, y0, y1, 0, 0, 0, 0, 0, 0]
        xid = str(self.get_window().get_xid())
        # ponytail: Gdk.property_change is not introspectable in PyGObject,
        # so struts go through xprop; swap to python-xlib if this ever hurts
        for prop, vals in (("_NET_WM_STRUT_PARTIAL", partial),
                           ("_NET_WM_STRUT", partial[:4])):
            subprocess.run(
                ["xprop", "-id", xid, "-f", prop, "32c", "-set", prop,
                 ",".join(str(v) for v in vals)],
                check=False)

    # --- window tabs ------------------------------------------------------

    def _hook(self, win):
        # window-opened fires once per window, so this never double-connects
        for sig in ("name-changed", "icon-changed", "state-changed",
                    "workspace-changed"):
            win.connect(sig, self._queue_refresh)

    def _queue_refresh(self, *_args):
        if not self._pending:
            self._pending = True
            GLib.idle_add(self._refresh_windows)

    def _refresh_windows(self):
        self._pending = False
        for child in self.win_box.get_children():
            child.destroy()
        ws = self.wnck.get_active_workspace()
        active = self.wnck.get_active_window()
        self.win_box.pack_start(self._section("WINDOWS"), False, False, 0)
        for w in self.wnck.get_windows():
            if w.is_skip_tasklist() or (ws and not w.is_on_workspace(ws)):
                continue
            self.win_box.pack_start(self._tab_button(w, w is active),
                                    False, False, 0)
        self.win_box.show_all()
        return False

    def _tab_button(self, w, is_active):
        btn = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(Gtk.Image.new_from_pixbuf(w.get_mini_icon()),
                       False, False, 0)
        label = Gtk.Label(label=w.get_name())
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_xalign(0)
        box.pack_start(label, True, True, 0)
        btn.add(box)
        btn.set_tooltip_text(w.get_name())
        if is_active:
            btn.get_style_context().add_class("active")
        if w.is_minimized():
            btn.get_style_context().add_class("minimized")
        btn.connect("clicked", self._on_tab_click, w)
        btn.connect("button-press-event", self._on_tab_press, w)
        return btn

    def _on_tab_click(self, _btn, w):
        if w.is_active():
            w.minimize()
        else:
            w.activate(Gtk.get_current_event_time())

    def _on_tab_press(self, _btn, event, w):
        if event.button == 2:
            w.close(event.time)
            return True
        return False

    # --- pinned files -----------------------------------------------------

    def _load_pins(self):
        try:
            with open(PINS_FILE) as f:
                return [p for p in json.load(f) if isinstance(p, str)]
        except (OSError, ValueError):
            return []

    def _save_pins(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PINS_FILE, "w") as f:
            json.dump(self.pins, f, indent=2)

    def _refresh_pins(self):
        for child in self.pin_box.get_children():
            child.destroy()
        if self.pins:
            self.pin_box.pack_start(self._section("PINNED"), False, False, 0)
            for path in self.pins:
                self.pin_box.pack_start(self._pin_button(path), False, False, 0)
        self.pin_box.show_all()

    def _pin_button(self, path):
        gfile = Gio.File.new_for_path(path)
        try:
            info = gfile.query_info("standard::icon", 0, None)
            image = Gtk.Image.new_from_gicon(info.get_icon(),
                                             Gtk.IconSize.MENU)
        except GLib.Error:
            image = Gtk.Image.new_from_icon_name("text-x-generic",
                                                 Gtk.IconSize.MENU)
        btn = Gtk.Button()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.pack_start(image, False, False, 0)
        label = Gtk.Label(label=os.path.basename(path))
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_xalign(0)
        box.pack_start(label, True, True, 0)
        btn.add(box)
        btn.set_tooltip_text(path)
        btn.connect("clicked", self._on_pin_click, gfile)
        btn.connect("button-press-event", self._on_pin_press, path)
        return btn

    def _on_pin_click(self, _btn, gfile):
        try:
            Gio.AppInfo.launch_default_for_uri(gfile.get_uri(), None)
        except GLib.Error as e:
            print(f"tabit: cannot open {gfile.get_path()}: {e.message}",
                  file=sys.stderr)

    def _on_pin_press(self, _btn, event, path):
        if event.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="Unpin")
            item.connect("activate", self._on_unpin, path)
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _on_unpin(self, _item, path):
        self.pins.remove(path)
        self._save_pins()
        self._refresh_pins()

    def _on_drop(self, _widget, _ctx, _x, _y, data, _info, _time):
        for uri in data.get_uris():
            path = Gio.File.new_for_uri(uri).get_path()
            if path and path not in self.pins:
                self.pins.append(path)
        self._save_pins()
        self._refresh_pins()

    # --- misc ---------------------------------------------------------------

    @staticmethod
    def _section(text):
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        label.get_style_context().add_class("section")
        return label

    def _on_bg_press(self, _widget, event):
        if event.button == 3:
            menu = Gtk.Menu()
            item = Gtk.MenuItem(label="Quit tabit")
            item.connect("activate", lambda *_: Gtk.main_quit())
            menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    GLib.set_prgname("tabit")

    display = Gdk.Display.get_default()
    if display is None or not isinstance(display, GdkX11.X11Display):
        sys.exit("tabit needs an X11 session (Wayland is not supported)")

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
