#!/usr/bin/env python3
"""SUDO_ASKPASS helper for tabit update — GTK password dialog.

Prints the password to stdout only (sudo -A contract). Exit 1 on cancel.
Invoked as a separate process so it can run its own Gtk main loop while
install.sh runs on a background thread inside tabit.
"""

from __future__ import annotations

import os
import sys

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib


def main() -> int:
    # sudo may pass a prompt as argv[1]
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Password:"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

    dialog = Gtk.Dialog(title="tabit — Authentication required")
    dialog.set_modal(True)
    dialog.set_default_size(420, 0)
    dialog.set_resizable(False)
    try:
        dialog.set_keep_above(True)
    except Exception:
        pass
    dialog.add_buttons(
        Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
        Gtk.STOCK_OK, Gtk.ResponseType.OK,
    )
    dialog.set_default_response(Gtk.ResponseType.OK)

    box = dialog.get_content_area()
    box.set_spacing(10)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(14)

    head = Gtk.Label(xalign=0)
    head.set_markup(
        "<b>Administrator privileges are required</b>\n"
        "System packages (GTK/VTE/…) may need updating via apt."
    )
    head.set_line_wrap(True)
    box.pack_start(head, False, False, 0)

    who = Gtk.Label(
        label=f"Password for {user}:",
        xalign=0,
    )
    box.pack_start(who, False, False, 0)

    entry = Gtk.Entry()
    entry.set_visibility(False)
    entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    entry.set_activates_default(True)
    entry.set_hexpand(True)
    box.pack_start(entry, False, False, 0)

    # Optional: show sudo's raw prompt (often useful if re-prompt after fail)
    if prompt and prompt not in ("Password:", "password:"):
        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            f"<span size='small' color='#888'>{GLib.markup_escape_text(prompt)}</span>"
        )
        hint.set_line_wrap(True)
        box.pack_start(hint, False, False, 0)

    dialog.show_all()
    entry.grab_focus()
    resp = dialog.run()
    password = entry.get_text() if resp == Gtk.ResponseType.OK else None
    dialog.destroy()

    # Drain pending events so the window actually closes before sudo continues
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    if password is None:
        return 1
    # Password on stdout only — no trailing junk
    sys.stdout.write(password)
    if not password.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(1)
