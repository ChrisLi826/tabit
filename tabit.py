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
import random
import re
import shlex
import signal
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkSource", "4")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkSource, Pango, Vte

# WebKit2 4.0 (libsoup2, older Ubuntu) or 4.1 (libsoup3, 24.04+); same API
# for what we use. Optional: without it the note Markdown preview is off.
WebKit2 = None
HAS_WEBKIT = False
for _wk in ("4.0", "4.1"):
    try:
        gi.require_version("WebKit2", _wk)
        from gi.repository import WebKit2
        HAS_WEBKIT = True
        break
    except (ValueError, ImportError):
        continue

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
ICON_NOTE = "text-x-generic-symbolic"
ICON_COMMAND = "system-run-symbolic"
ICON_TMUX = "view-grid-symbolic"
# per-type CSS class so each session icon gets its own color (see CSS)
ICON_CLASS = {
    "utilities-terminal-symbolic": "ic-shell",
    "network-wired-symbolic": "ic-serial",
    ICON_NOTE: "ic-note",
    ICON_COMMAND: "ic-command",
    ICON_TMUX: "ic-tmux",
}
# note performance: long lines / huge buffers can freeze GtkSourceView
NOTE_BIG_CHARS = 200_000   # total characters
NOTE_LONG_LINE = 8_000     # any single line
# a single very long line (or a huge file) freezes GtkSourceView on open;
# past these limits, offer to open it in a terminal editor instead
NOTE_MAX_OPEN_SIZE = 5_000_000   # bytes
NOTE_MAX_OPEN_LINE = 20_000      # any single line (chars)
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
# serial backends shown in the +Serial dialog (first = default).
# "screen" runs the bundled screen.sh script (written out at startup).
SERIAL_BACKENDS = ("screen", "kermit", "picocom", "ssh", "telnet")
SERIAL_NET_BACKENDS = ("ssh", "telnet")  # target a host, not a /dev device

# Bundled screen.sh: a `screen` wrapper for /dev/ttyUSB* with logfile,
# multi-attach and log retention. Kept verbatim so tabit needs nothing in
# ~/.local/bin; written to SCREEN_SH_PATH on startup and run from there.
SCREEN_SH = r'''#!/bin/bash
# screen.sh — wrapper around `screen` for /dev/ttyUSB* with sensible defaults
#
# Auto-names session as ap-ttyUSBN, opens a logfile so other shells (or AI
# agents) can read output via `tail -f`, and if the session already exists
# attaches with `-x` so multiple clients can share the same console.
#
# Usage:
#   screen.sh                 interactive picker (or use sole device)
#   screen.sh 0               /dev/ttyUSB0  @ 115200
#   screen.sh ttyUSB2         /dev/ttyUSB2  @ 115200
#   screen.sh /dev/ttyUSB1 9600
#   screen.sh -l              list devices and exit
#   screen.sh -f 0            force: kill any non-screen holder of ttyUSB0 first
#
# Env:
#   SCREEN_SH_LOG_DIR         logfile dir (default /tmp)
#   SCREEN_SH_BAUD            default baud (default 115200)
#   SCREEN_SH_LOG_KEEP        per-device log retention count (default 5; 0 = unlimited)

BAUD_DEFAULT="${SCREEN_SH_BAUD:-115200}"
LOG_DIR="${SCREEN_SH_LOG_DIR:-/tmp}"
LOG_KEEP="${SCREEN_SH_LOG_KEEP:-5}"

usage() {
    sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
}

list_devs() {
    ls -1 /dev/ttyUSB* 2>/dev/null
}

FORCE=0
case "$1" in
    -h|--help) usage; exit 0 ;;
    -l|--list) list_devs; exit 0 ;;
    -f|--force) FORCE=1; shift ;;
esac

# Resolve device argument
arg="$1"
if [ -z "$arg" ]; then
    mapfile -t devs < <(list_devs)
    if [ "${#devs[@]}" -eq 0 ]; then
        echo "screen.sh: no /dev/ttyUSB* found" >&2
        exit 1
    elif [ "${#devs[@]}" -eq 1 ]; then
        DEV="${devs[0]}"
    else
        echo "Available devices:"
        for i in "${!devs[@]}"; do
            holder=$(fuser "${devs[$i]}" 2>/dev/null | tr -d ' ' | head -c 20)
            [ -n "$holder" ] && holder=" (in use by PID $holder)"
            printf "  [%d] %s%s\n" "$i" "${devs[$i]}" "$holder"
        done
        read -rp "Pick: " idx
        DEV="${devs[$idx]}"
    fi
elif [[ "$arg" == /dev/* ]]; then
    DEV="$arg"
elif [[ "$arg" =~ ^[0-9]+$ ]]; then
    DEV="/dev/ttyUSB$arg"
elif [[ "$arg" == ttyUSB* ]]; then
    DEV="/dev/$arg"
else
    echo "screen.sh: don't recognize device '$arg'" >&2
    exit 1
fi

if [ ! -e "$DEV" ]; then
    echo "screen.sh: $DEV does not exist" >&2
    exit 1
fi

BAUD="${2:-$BAUD_DEFAULT}"
NAME="$(basename "$DEV")"
SESSION="ap-$NAME"
TS=$(date +%Y%m%d-%H%M%S)
LOG="$LOG_DIR/screen-$NAME-$TS.log"

# Detect anything already holding the device. If a screen daemon owns it,
# attach to that session regardless of its name; otherwise tell the user.
holder_pid=$(fuser "$DEV" 2>/dev/null | tr -d ' ' | head -c 20)
if [ -n "$holder_pid" ] && [ "$FORCE" = "0" ]; then
    # Match holder_pid against `screen -ls` (which prints "<pid>.<name>" lines)
    existing_session=$(screen -ls 2>/dev/null | awk -v pid="$holder_pid" '
        $1 ~ "^"pid"\\." { print $1; exit }')
    if [ -n "$existing_session" ]; then
        echo "screen.sh: $DEV already held by screen session '$existing_session' — multi-attaching with -x"
        exec screen -x "$existing_session"
    fi
    holder_cmd=$(ps -p "$holder_pid" -o cmd= 2>/dev/null | head -c 120)
    cat >&2 <<ERR
screen.sh: $DEV is held by PID $holder_pid (not a screen session):
    $holder_cmd

  - If it's picocom or another terminal: kill it first, or use socat mux.
  - To force kill the holder and start a new session: screen.sh -f $arg
ERR
    exit 1
fi

if [ "$FORCE" = "1" ] && [ -n "$holder_pid" ]; then
    echo "screen.sh: --force given, killing PID $holder_pid (was holding $DEV)"
    kill "$holder_pid" 2>/dev/null
    sleep 1
    fuser "$DEV" >/dev/null 2>&1 && {
        echo "screen.sh: $DEV still busy after kill, aborting" >&2
        exit 1
    }
fi

mkdir -p "$LOG_DIR"

# Retention: keep the newest (LOG_KEEP - 1) existing logs for this device;
# the about-to-be-created file becomes the LOG_KEEP-th.
if [ "$LOG_KEEP" -gt 0 ] 2>/dev/null; then
    pruned=$(ls -1t "$LOG_DIR/screen-${NAME}-"*.log 2>/dev/null | tail -n +"$LOG_KEEP")
    if [ -n "$pruned" ]; then
        echo "$pruned" | xargs -r rm -f
        n=$(echo "$pruned" | wc -l)
        echo "screen.sh: pruned $n old log(s) for $NAME (keeping newest $((LOG_KEEP - 1)) + this new one)"
    fi
fi

cat <<INFO
screen.sh: session=$SESSION  device=$DEV @ $BAUD
screen.sh: logfile=$LOG

  Detach:               Ctrl-a d
  Kill session:         Ctrl-a k    (or: screen -S $SESSION -X quit)
  From another shell — inject command:
      screen -S $SESSION -X stuff "<cmd>\$(printf '\\r')"
  From another shell — read output:
      tail -f $LOG

INFO

# Disable the alternate screen so screen's output stays in the terminal's
# real scrollback — then the mouse wheel scrolls back through history. Keep
# the user's own ~/.screenrc, then apply our overrides.
RC="$LOG_DIR/tabit-screenrc"
{
    [ -f "$HOME/.screenrc" ] && echo "source $HOME/.screenrc"
    echo "defscrollback 10000"
    echo "termcapinfo xterm* ti@:te@"
    echo "termcapinfo linux* ti@:te@"
} > "$RC" 2>/dev/null

exec screen -c "$RC" -S "$SESSION" -L -Logfile "$LOG" "$DEV" "$BAUD"
'''
# default AI CLI list for +AI (user-editable → ~/.config/tabit/ai_clis.json)
# Each entry: {"cli": name, "try": ["args after cli", ...]} then plain cli.
DEFAULT_AI_CLIS = [
    {"cli": "claude", "try": ["--continue"]},
    {"cli": "codex", "try": ["resume --last"]},
    {"cli": "grok", "try": ["--continue"]},
    {"cli": "agy", "try": ["-c", "--continue"]},
]
# used when user types a CLI not in the list
DEFAULT_AI_TRY = ["--continue", "resume --last", "--resume latest"]
KERMRC = os.path.expanduser("~/senaoenv/kermrc")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "tabit")
SCREEN_SH_PATH = os.path.join(CONFIG_DIR, "screen.sh")
SESSIONS_FILE = os.path.join(CONFIG_DIR, "sessions.json")
KEYS_FILE = os.path.join(CONFIG_DIR, "keys.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
AI_LAST_FILE = os.path.join(CONFIG_DIR, "ai_last.json")
AI_CLIS_FILE = os.path.join(CONFIG_DIR, "ai_clis.json")
COMMANDS_FILE = os.path.join(CONFIG_DIR, "commands.json")
# quick commands shown in the terminal bottom bar (user-editable; empty
# by default — add your own from the bar's edit button)
DEFAULT_COMMANDS = []
# tab-group colors (names match the .group-bar.grp-* CSS classes)
GROUP_COLORS = ["red", "orange", "yellow", "green", "cyan", "purple"]
TERM_FG = "#c0c5d0"  # soft, lower-contrast body text (was harsher #d5d5df)
TERM_BG = "#101016"
# 16-color ANSI palette for VTE (indexes 0–15). Matches the Tokyo Night
# accents used in the sidebar CSS. Blue (4 / 12) is a soft sky tone so
# `ls` dirs stay readable without a harsh pure blue on TERM_BG.
TERM_PALETTE = (
    "#15151c",  # 0  black
    "#f7768e",  # 1  red
    "#9ece6a",  # 2  green
    "#e0af68",  # 3  yellow
    "#89dceb",  # 4  blue  (ls directories) — sky, less saturated than #7aa2f7
    "#bb9af7",  # 5  magenta
    "#7dcfff",  # 6  cyan
    "#c0caf5",  # 7  white
    "#565f89",  # 8  bright black
    "#f7768e",  # 9  bright red
    "#9ece6a",  # 10 bright green
    "#e0af68",  # 11 bright yellow
    "#89dceb",  # 12 bright blue
    "#bb9af7",  # 13 bright magenta
    "#7dcfff",  # 14 bright cyan
    "#c0caf5",  # 15 bright white
)
DEFAULT_SETTINGS = {
    "note_wrap": True,
    "shell_inherit_cwd": False,  # new shell opens in the focused tab's path
    "ai_fresh_on_restore": False,  # restored AI tabs start fresh (no continue)
    "group_names": {},             # tab-group color -> display name
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
    ("note_find", "Note: Find text", "<Primary>f"),
    ("note_find_next", "Note: Find next", "F3"),
    ("note_find_prev", "Note: Find previous", "<Shift>F3"),
    ("term_find", "Terminal: Find text", "<Primary><Shift>f"),
    ("close_session", "Close session", "<Primary><Shift>w"),
    ("rename_session", "Rename session", "F2"),
    ("prev_session", "Previous session", "<Primary>Page_Up"),
    ("next_session", "Next session", "<Primary>Page_Down"),
    ("move_tab_up", "Move tab up", "<Primary><Shift>Page_Up"),
    ("move_tab_down", "Move tab down", "<Primary><Shift>Page_Down"),
    ("move_group_up", "Move group up", "<Primary><Alt><Shift>Page_Up"),
    ("move_group_down", "Move group down", "<Primary><Alt><Shift>Page_Down"),
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
.sidebar row.drop-into { box-shadow: inset 0 0 0 2px #7aa2f7; }
.sidebar row.drop-above { box-shadow: inset 0 3px 0 0 #7aa2f7; }
.sidebar row.drop-below { box-shadow: inset 0 -3px 0 0 #7aa2f7; }
/* Ctrl+clicked tabs pending a group action */
.sidebar row.marked { box-shadow: inset 0 0 0 2px #7dcfff; }
/* tab-group color stripe on the far left of a row */
.group-bar { background-color: transparent; border-radius: 2px; }
.group-bar.grp-red    { background-color: #f7768e; }
.group-bar.grp-orange { background-color: #ff9e64; }
.group-bar.grp-yellow { background-color: #e0af68; }
.group-bar.grp-green  { background-color: #9ece6a; }
.group-bar.grp-cyan   { background-color: #7dcfff; }
.group-bar.grp-purple { background-color: #bb9af7; }
/* group header row: a small color dot + the group name */
.group-header { padding: 5px 6px 1px 8px; }
.group-header label { font-size: 8pt; font-weight: 600; color: #b0b0bc; }
.group-dot { border-radius: 50%; }
.group-dot.grp-red    { background-color: #f7768e; }
.group-dot.grp-orange { background-color: #ff9e64; }
.group-dot.grp-yellow { background-color: #e0af68; }
.group-dot.grp-green  { background-color: #9ece6a; }
.group-dot.grp-cyan   { background-color: #7dcfff; }
.group-dot.grp-purple { background-color: #bb9af7; }
.sidebar row .close { opacity: 0; }
.sidebar row:hover .close, .sidebar row:selected .close { opacity: 1; }
.sidebar button { background: transparent; border: none; border-radius: 6px;
                  padding: 3px 6px; color: #8a8a98; }
.sidebar button:hover { color: #ececf4; background: rgba(255,255,255,0.11); }
/* per-type icon colors (symbolic icons follow the CSS color property) */
.ic-shell   { color: #9ece6a; }
.ic-serial  { color: #7dcfff; }
.ic-note    { color: #e0af68; }
.ic-command { color: #f7768e; }
.ic-tmux    { color: #bb9af7; }
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
.cmd-bar { background-color: #15151c; border-top: 1px solid #2c2c38;
           padding: 4px 6px; }
.cmd-bar button { padding: 2px 10px; font-size: 9pt; }
/* narrow the resize handle so it stops stealing clicks meant for the
   terminal; the sidebar's border-right is the visible line. themes pad
   the separator and add a grip image, which widens the grab zone, so
   zero those out too.
   ponytail: 2px hit zone, widen if it gets hard to grab on purpose */
paned > separator {
    min-width: 2px;
    padding: 0;
    margin: 0;
    background-image: none;
    background-color: transparent;
}
"""


class Tabit(Gtk.Window):
    def __init__(self):
        super().__init__(title="tabit")
        self.set_default_size(1100, 700)
        self._set_app_icon()
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
        self._marked = set()  # rows Ctrl+clicked for a group action
        gn = self._load_settings().get("group_names", {})
        self._group_names = dict(gn) if isinstance(gn, dict) else {}

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.set_size_request(120, -1)  # min width; actual set by paned
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
                ("+ Command", ICON_COMMAND, self._on_add_command),
                ("+ tmux", ICON_TMUX, self._on_add_tmux)):
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

        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._paned.pack1(sidebar, resize=False, shrink=False)
        self._paned.pack2(self.stack, resize=True, shrink=True)
        width = self._load_settings().get("sidebar_width", SIDEBAR_WIDTH)
        self._paned.set_position(width)
        self.add(self._paned)

        ai_fresh = self._load_settings().get("ai_fresh_on_restore", False)
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
                    argv = s["argv"]
                    if ai_fresh and s.get("icon") == ICON_AI:
                        argv = self._ai_argv_plain(argv)  # no continue/resume
                    self._add_session(s["label"], argv, s["icon"],
                                      s.get("sub"), s.get("cwd"),
                                      s.get("track_cwd", False))
                color = s.get("color")
                if color:  # restore the tab-group stripe on the new row
                    r = self.listbox.get_selected_row()
                    if r is not None:
                        self._apply_group(r, color)
            except (KeyError, TypeError, OSError):
                continue  # skip broken entries in a hand-edited file
        self._relayout()  # build group headers + cluster restored members
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

    def _on_term_spawned(self, _term, pid, _error, row):
        # keep the child pid so we can read its cwd on save
        row.pid = pid if pid and pid > 0 else None
        self._refresh_term_cwd(row)

    def _refresh_term_cwd(self, row):
        if row.dead or not getattr(row, "track_cwd", False):
            return
        cwd = self._term_cwd(row)
        if not cwd:
            return
        home = GLib.get_home_dir()
        if cwd == home:
            shown = "~"
        elif cwd.startswith(home + "/"):
            shown = "~" + cwd[len(home):]
        else:
            shown = cwd
        # long path: keep first level + "..." + last level
        parts = shown.split("/")
        if len(parts) > 3:
            shown = "/".join(parts[:2] + ["..."] + parts[-1:])
        if shown == row.sub_text:
            return
        row.sub_text = shown
        row.subtitle.set_text(shown)
        row.subtitle.set_no_show_all(False)
        row.subtitle.show()
        row.session_label = f"{row.title_text} {shown}"
        # persist the new cwd soon, so a restart (even an abrupt kill) keeps it
        self._save_sessions_soon()

    @staticmethod
    def _term_cwd(row):
        pid = getattr(row, "pid", None)
        if not pid:
            return None
        try:
            return os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            return None

    def _save_sessions(self):
        data = []
        for r in self._session_rows():  # skip group-header rows
            entry = {"label": r.title_text, "sub": r.sub_text,
                     "argv": r.argv, "icon": r.icon_name}
            if getattr(r, "track_cwd", False):
                entry["track_cwd"] = True
                entry["sub"] = ""  # sub is the live cwd; recompute on reload
            cwd = self._term_cwd(r)
            if cwd:
                entry["cwd"] = cwd
            if getattr(r, "group_color", None):
                entry["color"] = r.group_color
            data.append(entry)
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
        img = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        # per-type color for symbolic icons (recolored via CSS `color`)
        cls = ICON_CLASS.get(icon_name)
        if cls:
            img.get_style_context().add_class(cls)
        return img

    def _make_sidebar_row(self, label, sub, icon_name, tooltip):
        """Build the shared left-tab chrome; caller fills row.page / kind."""
        row = Gtk.ListBoxRow()
        hit = Gtk.EventBox()
        hit.set_visible_window(True)
        hit.set_above_child(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        group_bar = Gtk.Box()
        group_bar.set_size_request(4, -1)
        group_bar.get_style_context().add_class("group-bar")
        box.pack_start(group_bar, False, False, 0)
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
        self._enable_row_dnd(row, hit)
        row.set_tooltip_text(tooltip or "")
        row.session_label = f"{label} {sub}" if sub else label
        row.title_text = label
        row.title_label = title
        row.sub_text = sub
        row.icon_name = icon_name
        row.subtitle = subtitle
        row.dot = dot
        row.group_bar = group_bar
        row.group_color = None
        row.dead = False
        row.kind = "term"
        row.term = None
        row.view = None
        row.buffer = None
        row.file_path = None
        row.cmd_bar = None
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
        self.stack.show_all()
        self._relayout()  # keep group members clustered under their header
        self.listbox.select_row(row)
        self._save_sessions()

    def _add_session(self, label, argv, icon_name, sub=None, cwd=None,
                     track_cwd=False):
        term = Vte.Terminal()
        term.set_scrollback_lines(10000)
        fg, bg = Gdk.RGBA(), Gdk.RGBA()
        fg.parse(TERM_FG)
        bg.parse(TERM_BG)
        palette = []
        for hex_color in TERM_PALETTE:
            c = Gdk.RGBA()
            c.parse(hex_color)
            palette.append(c)
        term.set_colors(fg, bg, palette)
        term.connect("key-press-event", self._on_term_key)
        term.connect("button-press-event", self._on_term_button)
        term.drag_dest_set(Gtk.DestDefaults.ALL,
                            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                            Gdk.DragAction.COPY)
        term.connect("drag-data-received", self._on_term_drag_data_received)

        # VTE scrolls itself; do not wrap in ScrolledWindow.
        row = self._make_sidebar_row(label, sub, icon_name, " ".join(argv))
        row.argv = argv
        row.kind = "term"
        row.term = term
        row.pid = None
        row.track_cwd = track_cwd  # shell tabs show live cwd in the subtitle
        # terminal page = search bar (hidden) + VTE + quick-command bar
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(self._build_term_search_bar(row, term), False, False, 0)
        page.pack_start(term, True, True, 0)
        page.pack_start(self._build_cmd_bar(row), False, False, 0)
        self._place_tab_row(row, page)

        workdir = cwd if cwd and os.path.isdir(cwd) else GLib.get_home_dir()
        term.connect("child-exited", self._on_child_exited, row)
        term.connect("contents-changed", self._on_activity, row)
        term.spawn_async(Vte.PtyFlags.DEFAULT, workdir, argv,
                         None, GLib.SpawnFlags.SEARCH_PATH, None, None,
                         -1, None, self._on_term_spawned, row)

    # --- quick command bar ------------------------------------------------

    @staticmethod
    def _load_commands():
        try:
            with open(COMMANDS_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                out = [{"cmd": str(it["cmd"]),
                        "enter": bool(it.get("enter", True))}
                       for it in data
                       if isinstance(it, dict) and it.get("cmd")]
                return out
        except (OSError, ValueError):
            pass
        return [dict(c) for c in DEFAULT_COMMANDS]

    @staticmethod
    def _save_commands(cmds):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = [{"cmd": c["cmd"], "enter": bool(c.get("enter", True))}
                for c in cmds if c.get("cmd")]
        with open(COMMANDS_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def _build_cmd_bar(self, row):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("cmd-bar")
        row.cmd_bar = bar
        self._populate_cmd_bar(row)
        return bar

    def _populate_cmd_bar(self, row):
        bar = row.cmd_bar
        for c in bar.get_children():
            bar.remove(c)
        acc = self._action_accel_label("term_find")
        find = Gtk.Button(label=f"Find  ({acc})" if acc else "Find")
        find.set_tooltip_text("Search the terminal"
                              + (f" — {acc}" if acc else ""))
        find.connect("clicked", lambda _b, r=row: self._term_find_trigger(r))
        bar.pack_start(find, False, False, 0)
        for entry in self._load_commands():
            b = Gtk.Button(label=entry["cmd"])
            b.set_tooltip_text(("runs" if entry.get("enter") else "types")
                               + f" “{entry['cmd']}”")
            b.connect("clicked",
                      lambda _b, e=entry, r=row: self._send_cmd(r, e))
            bar.pack_start(b, False, False, 0)
        edit = Gtk.Button.new_from_icon_name("document-edit-symbolic",
                                             Gtk.IconSize.MENU)
        edit.set_relief(Gtk.ReliefStyle.NONE)
        edit.set_tooltip_text("Edit quick commands…")
        edit.connect("clicked", lambda *_: self._on_edit_commands())
        bar.pack_end(edit, False, False, 0)
        bar.show_all()

    def _refresh_cmd_bars(self):
        for r in self.listbox.get_children():
            if getattr(r, "cmd_bar", None) is not None:
                self._populate_cmd_bar(r)

    def _send_cmd(self, row, entry):
        term = getattr(row, "term", None)
        if term is None or row.dead:
            return
        # "\r" is what the Enter key sends on a pty, so the shell runs it
        text = entry["cmd"] + ("\r" if entry.get("enter") else "")
        term.feed_child(text.encode("utf-8"))
        term.grab_focus()

    def _on_edit_commands(self):
        dialog = Gtk.Dialog(title="Quick commands", transient_for=self,
                            modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin=12)
        dialog.get_content_area().add(box)

        listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(listbox, True, True, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                       False, False, 0)

        add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry(placeholder_text="command", width_chars=20)
        entry.set_hexpand(True)
        enter_chk = Gtk.CheckButton(label="send Enter")
        enter_chk.set_active(True)
        add_btn = Gtk.Button(label="Add")
        add_row.pack_start(entry, True, True, 0)
        add_row.pack_start(enter_chk, False, False, 0)
        add_row.pack_start(add_btn, False, False, 0)
        box.pack_start(add_row, False, False, 0)

        def refresh():
            for c in listbox.get_children():
                listbox.remove(c)
            cmds = self._load_commands()
            if not cmds:
                lbl = Gtk.Label(label="No quick commands yet.", xalign=0)
                lbl.get_style_context().add_class("session-sub")
                listbox.pack_start(lbl, False, False, 0)
            for i, e in enumerate(cmds):
                r = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                nl = Gtk.Label(label=e["cmd"], xalign=0)
                nl.set_hexpand(True)
                nl.set_ellipsize(Pango.EllipsizeMode.END)
                tag = Gtk.Label(label="↵ Enter" if e.get("enter") else "no Enter")
                tag.get_style_context().add_class("session-sub")
                dele = Gtk.Button.new_from_icon_name("user-trash-symbolic",
                                                     Gtk.IconSize.MENU)
                dele.set_relief(Gtk.ReliefStyle.NONE)
                dele.connect("clicked", lambda _b, idx=i: do_delete(idx))
                r.pack_start(nl, True, True, 0)
                r.pack_start(tag, False, False, 0)
                r.pack_start(dele, False, False, 0)
                listbox.pack_start(r, False, False, 0)
            listbox.show_all()

        def do_delete(idx):
            cmds = self._load_commands()
            if 0 <= idx < len(cmds):
                del cmds[idx]
                self._save_commands(cmds)
                refresh()
                self._refresh_cmd_bars()

        def do_add(*_a):
            cmd = entry.get_text().strip()
            if not cmd:
                return
            cmds = self._load_commands()
            cmds.append({"cmd": cmd, "enter": enter_chk.get_active()})
            self._save_commands(cmds)
            entry.set_text("")
            enter_chk.set_active(True)
            refresh()
            self._refresh_cmd_bars()

        add_btn.connect("clicked", do_add)
        entry.connect("activate", do_add)
        refresh()
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def _add_note_session(self, path=None, label=None, sub=None, content=None):
        """GtkSourceView note tab; path=None means untitled.
        content fills an untitled note (e.g. base64-decoded text)."""
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
        view.drag_dest_set(Gtk.DestDefaults.ALL,
                            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                            Gdk.DragAction.COPY)
        view.connect("drag-data-received", self._on_note_drag_data_received)
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
            if content is not None:
                buf.begin_not_undoable_action()
                buf.set_text(content)
                buf.end_not_undoable_action()
                buf.set_modified(True)  # decoded content is unsaved
            else:
                buf.set_modified(False)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)

        # editor on the left; when preview is on, the reader shows on the
        # right of a draggable split (Gtk.Paned). No webkit → just the editor.
        webview = None
        if HAS_WEBKIT:
            webview = WebKit2.WebView()
            wset = webview.get_settings()
            wset.set_enable_javascript(False)
            wset.set_allow_file_access_from_file_urls(True)
            content = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            content.pack1(scroll, resize=True, shrink=False)
            content.pack2(webview, resize=True, shrink=False)
            webview.set_no_show_all(True)  # hidden until preview toggled on
        else:
            content = scroll

        row = self._make_sidebar_row(label, sub, ICON_NOTE, tooltip)
        row.argv = [NOTE_SENTINEL, path or ""]
        row.kind = "note"
        row.term = None
        row.view = view
        row.buffer = buf
        row.file_path = path
        row.webview = webview
        row.content_paned = content if HAS_WEBKIT else None
        row.preview_on = False
        row._preview_src = None       # debounced live re-render
        row._preview_pos_set = False  # split divider placed once
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
        acc = self._action_accel_label("note_find")
        find_btn = Gtk.Button(label=f"Find  ({acc})" if acc else "Find")
        find_btn.set_tooltip_text("Search the note"
                                  + (f" — {acc}" if acc else ""))
        find_btn.connect("clicked",
                         lambda _b, r=row: self._note_find_trigger(r))
        tools.pack_start(find_btn, False, False, 0)
        tip = Gtk.Label(
            label="  (selection, or whole note if none)",
            xalign=0)
        tip.get_style_context().add_class("session-sub")
        tools.pack_start(tip, False, False, 0)

        search_bar = self._build_search_bar(row, view)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.pack_start(search_bar, False, False, 0)
        page.pack_start(content, True, True, 0)
        page.pack_start(tools, False, False, 0)
        self._place_tab_row(row, page)

        buf.connect("modified-changed",
                    lambda _b: self._refresh_note_title(row))
        buf.connect("changed", lambda _b: self._note_schedule_tune(row))
        buf.connect("changed", lambda _b: self._note_schedule_preview(row))
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

    def _note_render_preview(self, row):
        start, end = row.buffer.get_bounds()
        text = row.buffer.get_text(start, end, True)
        doc = self._md_to_html(text, row.file_path)
        base = "file:///"
        if row.file_path:
            base = "file://" + os.path.dirname(row.file_path) + "/"
        row.webview.load_html(doc, base)

    def _note_schedule_preview(self, row):
        # live re-render while the split reader is open (debounced)
        if not getattr(row, "preview_on", False):
            return
        if getattr(row, "_preview_src", None):
            GLib.source_remove(row._preview_src)

        def run():
            row._preview_src = None
            if row.preview_on:
                self._note_render_preview(row)
            return False

        row._preview_src = GLib.timeout_add(300, run)

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
            self._note_render_preview(row)
            row.webview.set_no_show_all(False)
            row.webview.show_all()
            # place the divider in the middle the first time only, so a
            # user's later drag is kept across toggles
            if not row._preview_pos_set:
                w = row.content_paned.get_allocated_width()
                if w > 0:
                    row.content_paned.set_position(w // 2)
                    row._preview_pos_set = True
        else:
            row.webview.hide()
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
        # a result with a very long line (e.g. base64 of a big file) would
        # freeze GtkSourceView; hand it to a terminal editor instead
        longest = max((len(ln) for ln in new_text.splitlines()),
                      default=len(new_text))
        if longest > NOTE_MAX_OPEN_LINE or len(new_text) > NOTE_MAX_OPEN_SIZE:
            self._note_result_to_editor(new_text, row)
            return
        buf = row.buffer
        buf.begin_user_action()
        buf.delete(start, end)
        buf.insert(start, new_text)
        buf.end_user_action()
        self._note_tune_perf(row)

    def _note_result_to_editor(self, text, row):
        """Oversized result: let the user choose where to save it, then open
        it in $EDITOR (terminal) — GtkSourceView can't show a huge line."""
        editor = os.environ.get("EDITOR") or "vi"
        chooser = Gtk.FileChooserDialog(
            title="Result is too large for the note editor — save to a file",
            parent=self, action=Gtk.FileChooserAction.SAVE)
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                            f"Save & open in {editor}", Gtk.ResponseType.OK)
        chooser.set_do_overwrite_confirmation(True)
        if row is not None and getattr(row, "file_path", None):
            chooser.set_current_folder(os.path.dirname(row.file_path))
            chooser.set_current_name(os.path.basename(row.file_path) + ".b64")
        else:
            chooser.set_current_name("output.b64")
        path = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK \
            else None
        chooser.destroy()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._note_msg(Gtk.MessageType.ERROR, "Could not save", str(e))
            return
        q = shlex.quote(path)
        self._add_session(os.path.basename(path),
                          ["/bin/sh", "-c", f"exec {editor} {q}"],
                          "utilities-terminal-symbolic", sub=editor)

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

    def _build_search_bar(self, row, view):
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin=4)
        search_box.get_style_context().add_class("search-bar")

        entry = Gtk.SearchEntry()
        entry.set_placeholder_text("Search note...")
        entry.set_width_chars(30)

        search_settings = GtkSource.SearchSettings.new()
        search_settings.set_wrap_around(True)

        search_context = GtkSource.SearchContext.new(view.get_buffer(), search_settings)
        search_context.set_highlight(True)

        row.search_settings = search_settings
        row.search_context = search_context
        row.search_entry = entry
        row.search_box = search_box

        btn_prev = Gtk.Button.new_from_icon_name("go-up-symbolic", Gtk.IconSize.BUTTON)
        btn_prev.set_tooltip_text("Previous occurrence")
        btn_next = Gtk.Button.new_from_icon_name("go-down-symbolic", Gtk.IconSize.BUTTON)
        btn_next.set_tooltip_text("Next occurrence")

        btn_close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        btn_close.set_relief(Gtk.ReliefStyle.NONE)
        btn_close.set_tooltip_text("Close search")

        chk_case = Gtk.CheckButton(label="Match Case")
        chk_word = Gtk.CheckButton(label="Whole Word")

        search_box.pack_start(entry, False, False, 0)
        search_box.pack_start(btn_prev, False, False, 0)
        search_box.pack_start(btn_next, False, False, 0)
        search_box.pack_start(chk_case, False, False, 0)
        search_box.pack_start(chk_word, False, False, 0)
        search_box.pack_start(btn_close, False, False, 0)

        chk_case.connect("toggled", lambda b: search_settings.set_case_sensitive(b.get_active()))
        chk_word.connect("toggled", lambda b: search_settings.set_at_word_boundaries(b.get_active()))

        lbl_status = Gtk.Label(label="")
        lbl_status.get_style_context().add_class("session-sub")
        search_box.pack_start(lbl_status, False, False, 0)
        row.search_status_lbl = lbl_status

        def on_search_changed(entry_widget):
            text = entry_widget.get_text()
            search_settings.set_search_text(text or None)
            if text:  # jump to the first match as you type
                self._search_find(row, forward=True)

        entry.connect("search-changed", on_search_changed)

        def go_next(*_):
            self._search_find(row, forward=True)

        def go_prev(*_):
            self._search_find(row, forward=False)

        entry.connect("activate", go_next)
        btn_next.connect("clicked", go_next)
        btn_prev.connect("clicked", go_prev)

        def on_close(*_):
            search_box.set_no_show_all(True)
            search_box.hide()
            search_settings.set_search_text(None)
            view.grab_focus()

        btn_close.connect("clicked", on_close)

        def on_entry_key(widget, event):
            if event.keyval == Gdk.KEY_Escape:
                on_close()
                return True
            elif event.keyval == Gdk.KEY_F3:
                shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
                self._search_find(row, forward=not shift)
                return True
            return False
        entry.connect("key-press-event", on_entry_key)

        def on_occurrences_changed(*_):
            cnt = search_context.get_occurrences_count()
            if not search_settings.get_search_text():
                lbl_status.set_text("")
            elif cnt == 0:
                lbl_status.set_text("No matches")
            else:
                lbl_status.set_text(f"{cnt} matches")
        search_context.connect("notify::occurrences-count", on_occurrences_changed)

        search_box.set_no_show_all(True)
        search_box.hide()
        return search_box

    # --- terminal (VTE) search -------------------------------------------

    def _build_term_search_bar(self, row, term):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                      margin=4)
        box.get_style_context().add_class("search-bar")
        entry = Gtk.SearchEntry()
        entry.set_placeholder_text("Search terminal...")
        entry.set_width_chars(30)
        btn_prev = Gtk.Button.new_from_icon_name("go-up-symbolic",
                                                 Gtk.IconSize.BUTTON)
        btn_prev.set_tooltip_text("Previous occurrence")
        btn_next = Gtk.Button.new_from_icon_name("go-down-symbolic",
                                                 Gtk.IconSize.BUTTON)
        btn_next.set_tooltip_text("Next occurrence")
        chk_case = Gtk.CheckButton(label="Match Case")
        chk_word = Gtk.CheckButton(label="Whole Word")
        btn_close = Gtk.Button.new_from_icon_name("window-close-symbolic",
                                                  Gtk.IconSize.BUTTON)
        btn_close.set_relief(Gtk.ReliefStyle.NONE)
        for w in (entry, btn_prev, btn_next, chk_case, chk_word, btn_close):
            box.pack_start(w, False, False, 0)
        row.term_search_box = box
        row.term_search_entry = entry

        def apply_regex():
            text = entry.get_text()
            if not text:
                term.search_set_regex(None, 0)
                return False
            # PCRE2: multiline + utf, caseless unless "Match Case"
            flags = 0x00000400 | 0x00080000
            if not chk_case.get_active():
                flags |= 0x00000008
            pat = re.escape(text)
            if chk_word.get_active():
                pat = r"\b" + pat + r"\b"
            try:
                rx = Vte.Regex.new_for_search(pat, -1, flags)
            except GLib.Error:
                return False
            term.search_set_regex(rx, 0)
            term.search_set_wrap_around(True)
            return True

        def go_next(*_):
            if apply_regex():
                term.search_find_next()

        def go_prev(*_):
            if apply_regex():
                term.search_find_previous()

        entry.connect("search-changed", go_next)  # jump as you type
        entry.connect("activate", go_next)
        btn_next.connect("clicked", go_next)
        btn_prev.connect("clicked", go_prev)
        chk_case.connect("toggled", go_next)
        chk_word.connect("toggled", go_next)
        row._term_find_next = go_next  # so F3 works from the terminal too
        row._term_find_prev = go_prev

        def on_close(*_):
            box.set_no_show_all(True)
            box.hide()
            term.search_set_regex(None, 0)
            term.grab_focus()

        btn_close.connect("clicked", on_close)

        def on_key(_w, event):
            if event.keyval == Gdk.KEY_Escape:
                on_close()
                return True
            if event.keyval == Gdk.KEY_F3:
                shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
                go_prev() if shift else go_next()
                return True
            return False

        entry.connect("key-press-event", on_key)
        box.set_no_show_all(True)
        box.hide()
        return box

    def _term_find_trigger(self, row):
        if not hasattr(row, "term_search_box"):
            return
        row.term_search_box.set_no_show_all(False)
        row.term_search_box.show_all()
        row.page.queue_resize()
        row.term_search_entry.grab_focus()
        row.term_search_entry.select_region(0, -1)

    def _note_find_trigger(self, row):
        if not hasattr(row, "search_box"):
            return
        row.search_box.set_no_show_all(False)
        row.search_box.show_all()
        row.page.queue_resize()
        buf = row.buffer
        bounds = buf.get_selection_bounds()
        if bounds:
            start, end = bounds
            text = buf.get_text(start, end, True)
            if "\n" not in text and len(text) < 100:
                row.search_entry.set_text(text)
        # re-sync the entry text into the search settings so reopening Find
        # keeps working (on_close cleared it); select all so typing replaces
        row.search_settings.set_search_text(row.search_entry.get_text() or None)
        row.search_entry.grab_focus()
        row.search_entry.select_region(0, -1)

    def _search_find(self, row, forward=True):
        if not hasattr(row, "search_context"):
            return
        buf = row.buffer
        insert_mark = buf.get_insert()
        start_iter = buf.get_iter_at_mark(insert_mark)

        if forward:
            res = row.search_context.forward(start_iter)
        else:
            res = row.search_context.backward(start_iter)

        if res and res[0]:
            match_start, match_end = res[1], res[2]
            sel_bounds = buf.get_selection_bounds()
            if sel_bounds and sel_bounds[0].equal(match_start) and sel_bounds[1].equal(match_end):
                if forward:
                    start_iter.forward_char()
                    res = row.search_context.forward(start_iter)
                else:
                    start_iter.backward_char()
                    res = row.search_context.backward(start_iter)
                if res and res[0]:
                    match_start, match_end = res[1], res[2]
                else:
                    return

            buf.select_range(match_start, match_end)
            row.view.scroll_to_iter(match_start, 0.0, False, 0.5, 0.5)

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

    def _set_app_icon(self):
        # running from the repo: load the svg next to this script; once
        # installed, tabit.py has no svg beside it so use the theme icon
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "tabit.svg")
        if os.path.isfile(here):
            try:
                self.set_icon_from_file(here)
                return
            except GLib.Error:
                pass
        self.set_icon_name("tabit")

    def _on_delete_event(self, *_a):
        for row in list(self.listbox.get_children()):
            if not self._confirm_close_row(row):
                return True  # abort window close
        self._save_sessions()  # capture each shell's current cwd before exit
        self._save_settings({"sidebar_width": self._paned.get_position()})
        return False

    def _move_session(self, delta):
        row = self.listbox.get_selected_row()
        if row is None or getattr(row, "kind", None) == "group_header":
            return
        rows = self._session_rows()  # ignore group-header rows
        if row not in rows:
            return
        blocks = self._blocks()
        g = row.group_color
        if g:
            gb = next(b for b in blocks if b[0] == "group" and b[1] == g)
            members = gb[2]
            gi = members.index(row)
            if (delta < 0 and gi == 0) or (delta > 0 and gi == len(members) - 1):
                self._apply_group(row, None)  # at an edge → leave the group
                self._relayout()
                self._save_sessions_soon()
            else:                             # reorder within the group
                members[gi], members[gi + delta] = \
                    members[gi + delta], members[gi]
                self._apply_block_order(blocks)
            return
        # ungrouped tab
        bi = next(i for i, b in enumerate(blocks)
                  if b[0] == "tab" and b[1] is row)
        j = bi + delta
        if j < 0 or j >= len(blocks):
            return
        neighbor = blocks[j]
        if neighbor[0] == "group":            # entering a group → join its edge
            self._apply_group(row, neighbor[1])
            blocks.pop(bi)
            members = neighbor[2]
            members.insert(0, row) if delta > 0 else members.append(row)
            self._apply_block_order(blocks)
        else:                                 # swap with the adjacent tab
            blocks[bi], blocks[j] = blocks[j], blocks[bi]
            self._apply_block_order(blocks)

    # --- drag-to-reorder --------------------------------------------------

    def _enable_row_dnd(self, row, hit):
        target = Gtk.TargetEntry.new("TABIT_ROW", Gtk.TargetFlags.SAME_APP, 0)
        hit.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target],
                            Gdk.DragAction.MOVE)
        hit.connect("drag-begin", self._on_row_drag_begin, row)
        hit.connect("drag-data-get", self._on_row_drag_get)
        # MOTION|DROP keep auto accept + finish; drop HIGHLIGHT so we can
        # draw our own themed drop frame instead of the default green box
        row.drag_dest_set(Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
                          [target], Gdk.DragAction.MOVE)
        row.connect("drag-data-received", self._on_row_drop)
        row.connect("drag-motion", self._on_row_drag_motion)
        row.connect("drag-leave", self._on_row_drag_leave)

    def _on_row_drag_begin(self, _hit, _ctx, row):
        self._drag_row = row

    _DROP_CLASSES = ("drop-into", "drop-above", "drop-below")

    @staticmethod
    def _row_drop_zone(row, y):
        # top/bottom quarters reorder; the middle half joins the group
        h = row.get_allocation().height or 1
        if y < h * 0.25:
            return "above"
        if y > h * 0.75:
            return "below"
        return "into"

    @staticmethod
    def _clear_drop_classes(row):
        ctx = row.get_style_context()
        for c in Tabit._DROP_CLASSES:
            ctx.remove_class(c)

    def _on_row_drag_motion(self, row, _ctx, _x, y, _time):
        self._clear_drop_classes(row)
        row.get_style_context().add_class("drop-" + self._row_drop_zone(row, y))
        return False  # let DestDefaults.MOTION set the drag status

    def _on_row_drag_leave(self, row, _ctx, _time):
        self._clear_drop_classes(row)

    @staticmethod
    def _on_row_drag_get(_hit, _ctx, data, _info, _time):
        data.set(data.get_target(), 8, b"1")  # payload unused; _drag_row holds it

    def _on_row_drop(self, row, _ctx, _x, y, _data, _info, _time):
        self._clear_drop_classes(row)
        dragged = getattr(self, "_drag_row", None)
        self._drag_row = None
        if dragged is None or dragged is row:
            return
        if getattr(dragged, "kind", None) == "group_header":
            self._drop_group(dragged.group_color, row, y)  # move whole group
            return
        zone = self._row_drop_zone(row, y)
        if zone == "into":                 # drop on the middle → join the group
            self._group_with(dragged, row)
            return
        before = (zone == "above")         # top/bottom edge → reorder
        rows = self._session_rows()
        rows.remove(dragged)
        idx = rows.index(row) + (0 if before else 1)
        rows.insert(idx, dragged)
        for i, r in enumerate(rows):
            r._order = i
        self._order_seq = len(rows)
        self._relayout()                   # re-cluster groups + headers
        self.listbox.select_row(dragged)
        self._save_sessions()

    def _session_rows(self):
        """Session rows (not group headers), in visual order."""
        return sorted((r for r in self.listbox.get_children()
                       if getattr(r, "kind", None) != "group_header"),
                      key=lambda r: r._order)

    def _blocks(self):
        """Sidebar as movable blocks: ('tab', row) or ('group', color, rows)."""
        blocks, seen = [], set()
        sessions = self._session_rows()
        for r in sessions:
            g = r.group_color
            if g:
                if g in seen:
                    continue
                blocks.append(("group", g,
                               [m for m in sessions if m.group_color == g]))
                seen.add(g)
            else:
                blocks.append(("tab", r))
        return blocks

    def _apply_block_order(self, blocks):
        o = 0
        for b in blocks:
            for r in ([b[1]] if b[0] == "tab" else b[2]):
                r._order = o
                o += 1
        self._relayout()
        self._save_sessions_soon()

    def _move_block(self, match, delta):
        blocks = self._blocks()
        bi = next((i for i, b in enumerate(blocks) if match(b)), None)
        if bi is None:
            return
        j = bi + delta
        if 0 <= j < len(blocks):
            blocks[bi], blocks[j] = blocks[j], blocks[bi]
            self._apply_block_order(blocks)

    def _move_group(self, color, delta):
        self._move_block(lambda b: b[0] == "group" and b[1] == color, delta)

    def _drop_group(self, color, target_row, y):
        """Move a whole group (dragged by its header) next to target_row."""
        tcolor = target_row.group_color
        if tcolor == color:
            return  # dropped on its own group
        blocks = self._blocks()
        gi = next((i for i, b in enumerate(blocks)
                   if b[0] == "group" and b[1] == color), None)
        if gi is None:
            return
        if tcolor:
            ti = next(i for i, b in enumerate(blocks)
                      if b[0] == "group" and b[1] == tcolor)
        else:
            ti = next(i for i, b in enumerate(blocks)
                      if b[0] == "tab" and b[1] is target_row)
        grp = blocks.pop(gi)
        if gi < ti:
            ti -= 1
        before = y < (target_row.get_allocation().height or 1) / 2
        blocks.insert(ti if before else ti + 1, grp)
        self._apply_block_order(blocks)

    def _new_group_color(self):
        used = {getattr(r, "group_color", None) for r in self._session_rows()}
        free = [c for c in GROUP_COLORS if c not in used]
        return random.choice(free or GROUP_COLORS)

    def _group_with(self, dragged, target):
        color = getattr(target, "group_color", None)
        if not color:                      # target ungrouped → start a new group
            color = self._new_group_color()
            self._apply_group(target, color)
        self._apply_group(dragged, color)
        self._relayout()
        self._save_sessions()

    def _make_group_header(self, color):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.kind = "group_header"
        row.group_color = color
        row.get_style_context().add_class("group-header")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dot = Gtk.Box()
        dot.set_size_request(9, 9)
        dot.set_valign(Gtk.Align.CENTER)
        dot.get_style_context().add_class("group-dot")
        dot.get_style_context().add_class("grp-" + color)
        box.pack_start(dot, False, False, 0)
        name = self._group_names.get(color) or color.capitalize()
        box.pack_start(Gtk.Label(label=name.upper(), xalign=0), True, True, 0)
        hit = Gtk.EventBox()
        hit.add(box)
        hit.connect("button-press-event", self._on_header_button, color)
        # drag the header to move the whole group (source only; not a drop dest)
        target = Gtk.TargetEntry.new("TABIT_ROW", Gtk.TargetFlags.SAME_APP, 0)
        hit.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target],
                            Gdk.DragAction.MOVE)
        hit.connect("drag-begin", self._on_row_drag_begin, row)
        hit.connect("drag-data-get", self._on_row_drag_get)
        row.add(hit)
        return row

    def _relayout(self):
        """Rebuild the sidebar: each group's members cluster under a color
        header, ungrouped tabs keep their place. No collapsing."""
        children = self.listbox.get_children()
        for h in children:
            if getattr(h, "kind", None) == "group_header":
                self.listbox.remove(h)
        sessions = self._session_rows()
        seq, emitted = [], set()
        for r in sessions:
            g = getattr(r, "group_color", None)
            if g:
                if g in emitted:
                    continue
                seq.append(self._make_group_header(g))
                seq.extend(m for m in sessions if m.group_color == g)
                emitted.add(g)
            else:
                seq.append(r)
        for i, r in enumerate(seq):
            r._order = i
            if getattr(r, "kind", None) == "group_header":
                self.listbox.add(r)
        self._order_seq = len(seq)
        self.listbox.invalidate_sort()
        self.listbox.show_all()
        # forget names of colors no longer in use; persist so a later reuse of
        # that color can't inherit the deleted group's name after a restart
        pruned = False
        for c in list(self._group_names):
            if c not in emitted:
                del self._group_names[c]
                pruned = True
        if pruned:
            self._save_group_names()

    def _save_group_names(self):
        self._save_settings({"group_names": self._group_names})

    def _on_header_button(self, _hit, event, color):
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        menu = Gtk.Menu()
        rn = Gtk.MenuItem(label="Rename group…")
        rn.connect("activate", lambda *_: self._rename_group(color))
        up = Gtk.MenuItem(label="Move group up")
        up.connect("activate", lambda *_: self._move_group(color, -1))
        dn = Gtk.MenuItem(label="Move group down")
        dn.connect("activate", lambda *_: self._move_group(color, 1))
        ug = Gtk.MenuItem(label="Ungroup")
        ug.connect("activate", lambda *_: self._ungroup(color))
        for it in (rn, up, dn, ug):
            menu.append(it)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _rename_group(self, color):
        dialog = Gtk.Dialog(title="Rename group", transient_for=self,
                            modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                           "Rename", Gtk.ResponseType.OK)
        entry = Gtk.Entry(text=self._group_names.get(color, ""),
                          margin=12, width_chars=24)
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().add(entry)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if name:
                self._group_names[color] = name
            else:
                self._group_names.pop(color, None)
            self._save_group_names()
            self._relayout()
        dialog.destroy()

    def _ungroup(self, color):
        for r in self._session_rows():
            if r.group_color == color:
                self._apply_group(r, None)
        self._group_names.pop(color, None)
        self._save_group_names()
        self._relayout()
        self._save_sessions()

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
        self._refresh_term_cwd(row)

    def _close_session(self, row):
        if row.get_parent() is None:
            return
        if not self._confirm_close_row(row):
            return
        was_selected = self.listbox.get_selected_row() is row
        rows = self._session_rows()
        idx = rows.index(row)
        # drop pending note timers so they don't fire on a destroyed widget
        for attr in ("_preview_src", "_tune_src"):
            src = getattr(row, attr, None)
            if src:
                GLib.source_remove(src)
                setattr(row, attr, None)
        row.preview_on = False
        self._marked.discard(row)
        self.listbox.remove(row)
        self.stack.remove(row.page)
        row.page.destroy()  # term: SIGHUP child; note: destroys view
        self._relayout()  # drop empty group headers, re-cluster
        self._save_sessions()
        rows = self._session_rows()
        if not rows:
            Gtk.main_quit()
        elif was_selected:
            # focus the next tab (same index after remove); if we closed
            # the last one, fall back to the new last
            self.listbox.select_row(rows[min(idx, len(rows) - 1)])

    def _focus_row_content(self, row):
        # defer to idle: a mouse click grabs focus to the row afterwards, so
        # grabbing now would not stick. Skip while a rename popover is open.
        def grab():
            if row.get_parent() is None:  # row closed meanwhile
                return False
            if getattr(self, "_rename_pop", None) is not None:
                return False  # renaming: keep focus in the rename entry
            if getattr(row, "kind", None) == "note":
                if row.view is not None and not row.view.has_focus():
                    row.view.grab_focus()
            elif row.term is not None and not row.term.has_focus():
                row.term.grab_focus()
            return False

        GLib.idle_add(grab)

    def _on_row_selected(self, _listbox, row):
        self._clear_marks()  # a plain tab switch drops any Ctrl+click marks
        if row is None:
            return
        row.dot.hide()
        self.stack.set_visible_child(row.page)
        self.set_title(f"{row.session_label} — tabit")
        self._focus_row_content(row)

    def _toggle_mark(self, row):
        ctx = row.get_style_context()
        if row in self._marked:
            self._marked.discard(row)
            ctx.remove_class("marked")
        else:
            self._marked.add(row)
            ctx.add_class("marked")

    def _clear_marks(self):
        for r in self._marked:
            r.get_style_context().remove_class("marked")
        self._marked.clear()

    def _group_marked(self):
        rows = [r for r in self._marked if r.get_parent() is not None]
        if not rows:
            return
        # reuse an existing group color among the marked rows, else a new one
        color = next((r.group_color for r in rows
                      if getattr(r, "group_color", None)), None) \
            or self._new_group_color()
        for r in rows:
            self._apply_group(r, color)
        self._clear_marks()
        self._relayout()
        self._save_sessions()

    def _on_tab_button(self, _hit, event, row):
        """EventBox on each tab: Ctrl+click mark, double-click rename,
        right-click menu."""
        if (event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS
                and (event.state & Gdk.ModifierType.CONTROL_MASK)):
            self._toggle_mark(row)  # multi-select for a group action
            return True
        if event.button == 1 and event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
            self.listbox.select_row(row)
            # defer so ListBox finishes its own click handling first
            GLib.idle_add(self._rename_session, row)
            return True
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            menu = Gtk.Menu()
            if self._marked:
                # act on the Ctrl+clicked selection, keep the marks intact
                if row not in self._marked:
                    self._toggle_mark(row)
                n = len(self._marked)
                grp = Gtk.MenuItem(label=f"Add {n} selected tabs to a group")
                grp.connect("activate", lambda *_: self._group_marked())
                menu.append(grp)
                clr = Gtk.MenuItem(label="Clear selection")
                clr.connect("activate", lambda *_: self._clear_marks())
                menu.append(clr)
            else:
                self.listbox.select_row(row)
                item = Gtk.MenuItem(label="Rename…")
                item.connect("activate", lambda *_: self._rename_session(row))
                menu.append(item)
                group_item = Gtk.MenuItem(label="Group color")
                submenu = Gtk.Menu()
                none_it = Gtk.MenuItem(label="None")
                none_it.connect("activate",
                                lambda *_: self._set_group(row, None))
                submenu.append(none_it)
                for color in GROUP_COLORS:
                    it = Gtk.MenuItem(label=color.capitalize())
                    it.connect("activate",
                               lambda _i, c=color: self._set_group(row, c))
                    submenu.append(it)
                group_item.set_submenu(submenu)
                menu.append(group_item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def _apply_group(self, row, color):
        """Set the row's group-color stripe (no persistence)."""
        ctx = row.group_bar.get_style_context()
        for c in GROUP_COLORS:
            ctx.remove_class("grp-" + c)
        row.group_color = color if color in GROUP_COLORS else None
        if row.group_color:
            ctx.add_class("grp-" + row.group_color)

    def _set_group(self, row, color):
        self._apply_group(row, color)
        self._relayout()
        self._save_sessions()

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

    def _focused_cwd(self):
        """Path of the currently selected tab: a terminal's live cwd, or a
        saved note's folder. None if unknown."""
        row = self.listbox.get_selected_row()
        if row is None:
            return None
        if getattr(row, "kind", None) == "term":
            return self._term_cwd(row)
        if getattr(row, "file_path", None):
            return os.path.dirname(row.file_path)
        return None

    def _on_add_shell(self, _btn):
        cwd = None
        if self._load_settings().get("shell_inherit_cwd", False):
            cwd = self._focused_cwd()
        self._add_session("shell", [os.environ.get("SHELL", "/bin/bash")],
                          "utilities-terminal-symbolic", cwd=cwd,
                          track_cwd=True)

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
            ok = chooser.run() == Gtk.ResponseType.OK
            fn = chooser.get_filename() if ok else None
            chooser.destroy()
            if fn and os.path.isfile(fn) and self._note_file_too_big(fn):
                self._open_big_file(fn)  # GtkSourceView would freeze
            elif fn is not None:
                self._add_note_session(path=fn)

    @staticmethod
    def _note_file_too_big(path):
        """True if the file is too big / long-lined for GtkSourceView."""
        try:
            if os.path.getsize(path) > NOTE_MAX_OPEN_SIZE:
                return True
            with open(path, "rb") as f:
                data = f.read()
            return max((len(ln) for ln in data.split(b"\n")),
                       default=0) > NOTE_MAX_OPEN_LINE
        except OSError:
            return False

    @staticmethod
    def _b64_decode_file(path):
        """If the file is valid base64, return the decoded bytes, else None."""
        try:
            if os.path.getsize(path) > 50_000_000:
                return None
            with open(path, "rb") as f:
                compact = b"".join(f.read().split())  # drop any whitespace
            if not compact or len(compact) % 4 != 0:
                return None
            return base64.b64decode(compact, validate=True)
        except (OSError, ValueError):
            return None

    def _open_big_file(self, path):
        """Large / long-line file: open it in a terminal editor (viewport
        based, handles it) instead of freezing GtkSourceView. If it is base64,
        also offer to decode it."""
        size_mb = os.path.getsize(path) / 1_000_000
        editor = os.environ.get("EDITOR") or "vi"
        decoded = self._b64_decode_file(path)
        dialog = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"“{os.path.basename(path)}” is large "
                 f"({size_mb:.1f} MB) or has very long lines.")
        dialog.format_secondary_text(
            f"The note editor would freeze on it. Open it in {editor} "
            "(terminal)" + (", or decode it?" if decoded is not None
                            else " instead?"))
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Open in editor anyway", Gtk.ResponseType.REJECT)
        if decoded is not None:
            dialog.add_button("Base64 decode", 2)
        dialog.add_button(f"Open in {editor}", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.OK:            # edit with $EDITOR
            q = shlex.quote(path)
            self._add_session(os.path.basename(path),
                              ["/bin/sh", "-c", f"exec {editor} {q}"],
                              "utilities-terminal-symbolic", sub=editor)
        elif resp == Gtk.ResponseType.REJECT:      # force into the note editor
            self._add_note_session(path=path)
        elif resp == 2 and decoded is not None:    # base64 decode
            self._open_decoded(decoded, os.path.basename(path))

    def _open_decoded(self, data, name):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self._note_msg(Gtk.MessageType.WARNING,
                           "Decoded content is binary",
                           "It is not UTF-8 text, so it can't open as a note.")
            return
        longest = max((len(ln) for ln in text.splitlines()), default=len(text))
        if longest > NOTE_MAX_OPEN_LINE or len(text) > NOTE_MAX_OPEN_SIZE:
            self._note_result_to_editor(text, None)  # still too big → save+vi
        else:
            self._add_note_session(content=text, label=f"{name} (decoded)",
                                   sub="decoded")

    @staticmethod
    def _serial_argv(backend, dev, rate, port=""):
        if backend in ("ssh", "telnet"):
            try:
                parts = shlex.split(dev)  # host, or "user@host [args]"
            except ValueError:
                parts = dev.split()
            if backend == "ssh":  # ssh takes the port with -p
                return ["ssh"] + (["-p", port] if port else []) + parts
            return ["telnet"] + parts + ([port] if port else [])  # telnet: host port
        if backend == "screen":
            return [SCREEN_SH_PATH, dev, rate]
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
        grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=12)
        # serial: a /dev dropdown; ssh/telnet: a plain host entry (no dropdown)
        combo = Gtk.ComboBoxText.new_with_entry()
        for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
            combo.append_text(dev)
        combo.set_active(0)
        host = Gtk.Entry()
        host.set_placeholder_text("user@host")
        target_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        target_box.pack_start(combo, True, True, 0)
        target_box.pack_start(host, True, True, 0)

        baud = Gtk.Entry(text=DEFAULT_BAUD)
        port = Gtk.Entry()
        port.set_placeholder_text("port (optional)")
        field2_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        field2_box.pack_start(baud, True, True, 0)
        field2_box.pack_start(port, True, True, 0)

        backend = Gtk.ComboBoxText()
        for name in SERIAL_BACKENDS:
            backend.append_text(name)
        backend.set_active(0)  # screen
        target_label = Gtk.Label(label="Device", xalign=0)
        field2_label = Gtk.Label(label="Baud", xalign=0)
        for w in (target_box, field2_box, backend):
            w.set_hexpand(True)  # fill the width so there's no right-side gap
        grid.attach(target_label, 0, 0, 1, 1)
        grid.attach(target_box, 1, 0, 1, 1)
        grid.attach(field2_label, 0, 1, 1, 1)
        grid.attach(field2_box, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Tool", xalign=0), 0, 2, 1, 1)
        grid.attach(backend, 1, 2, 1, 1)
        # buttons directly under the fields (not the far-right action area)
        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btns.set_halign(Gtk.Align.END)
        btns.set_margin_top(6)
        cancel_b = Gtk.Button(label="Cancel")
        open_b = Gtk.Button(label="Open")
        cancel_b.connect("clicked",
                         lambda *_: dialog.response(Gtk.ResponseType.CANCEL))
        open_b.connect("clicked",
                       lambda *_: dialog.response(Gtk.ResponseType.OK))
        btns.pack_start(cancel_b, False, False, 0)
        btns.pack_start(open_b, False, False, 0)
        grid.attach(btns, 0, 3, 2, 1)
        open_b.set_can_default(True)
        dialog.set_default(open_b)
        dialog.get_content_area().add(grid)

        def on_backend_changed(*_a):
            net = backend.get_active_text() in SERIAL_NET_BACKENDS
            target_label.set_text("Host" if net else "Device")
            field2_label.set_text("Port" if net else "Baud")
            combo.set_visible(not net)
            host.set_visible(net)
            baud.set_visible(not net)
            port.set_visible(net)

        backend.connect("changed", on_backend_changed)
        self._dialog_enter_is_ok(dialog)
        dialog.show_all()
        on_backend_changed()  # after show_all so hide() sticks
        if dialog.run() == Gtk.ResponseType.OK:
            tool = backend.get_active_text() or SERIAL_BACKENDS[0]
            net = tool in SERIAL_NET_BACKENDS
            if net:
                dev = (host.get_text() or "").strip()
                prt = (port.get_text() or "").strip()
                if dev:
                    label = (dev.split() or [dev])[0]
                    sub = f"{tool}:{prt}" if prt else tool
                    self._add_session(
                        label, self._serial_argv(tool, dev, "", prt),
                        "network-wired-symbolic", sub=sub)
            else:
                dev = (combo.get_active_text() or "").strip()
                rate = baud.get_text().strip() or DEFAULT_BAUD
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
                                  ICON_COMMAND,
                                  sub=parts[1] if len(parts) > 1 else None)
        dialog.destroy()

    @staticmethod
    def _tmux_sessions():
        try:
            out = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return []
        if out.returncode != 0:  # no server running / no sessions
            return []
        return [s for s in out.stdout.splitlines() if s]

    def _on_add_tmux(self, _btn):
        dialog = Gtk.Dialog(title="tmux sessions", transient_for=self,
                            modal=True)
        dialog.add_button("Close", Gtk.ResponseType.CANCEL)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin=12)
        dialog.get_content_area().add(box)

        # create-new row
        new_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry = Gtk.Entry(placeholder_text="new session name", width_chars=22)
        entry.set_hexpand(True)
        create = Gtk.Button(label="Create")
        new_row.pack_start(entry, True, True, 0)
        new_row.pack_start(create, False, False, 0)
        box.pack_start(new_row, False, False, 0)
        box.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                       False, False, 0)

        listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(listbox, True, True, 0)

        chosen = {}  # filled with label/argv when the user picks a session

        def open_session(label, argv):
            chosen["label"] = label
            chosen["argv"] = argv
            dialog.response(Gtk.ResponseType.OK)

        def create_new(*_a):
            name = entry.get_text().strip()
            # -A: attach if it already exists, otherwise create it
            argv = (["tmux", "new-session", "-A", "-s", name] if name
                    else ["tmux"])
            open_session(name or "tmux", argv)

        def do_rename(name):
            new = self._tmux_prompt_rename(dialog, name)
            if new and new != name:
                subprocess.run(["tmux", "rename-session", "-t", name, new],
                               capture_output=True)
                refresh()

        def do_kill(name):
            if self._tmux_confirm_kill(dialog, name):
                subprocess.run(["tmux", "kill-session", "-t", name],
                               capture_output=True)
                refresh()

        def refresh():
            for c in listbox.get_children():
                listbox.remove(c)
            sessions = self._tmux_sessions()
            if not sessions:
                lbl = Gtk.Label(label="No running tmux sessions.", xalign=0)
                lbl.get_style_context().add_class("session-sub")
                listbox.pack_start(lbl, False, False, 0)
            for name in sessions:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                nl = Gtk.Label(label=name, xalign=0)
                nl.set_hexpand(True)
                nl.set_ellipsize(Pango.EllipsizeMode.END)
                row.pack_start(nl, True, True, 0)
                att = Gtk.Button(label="Attach")
                ren = Gtk.Button.new_from_icon_name("document-edit-symbolic",
                                                    Gtk.IconSize.MENU)
                ren.set_tooltip_text("Rename")
                kill = Gtk.Button.new_from_icon_name("user-trash-symbolic",
                                                     Gtk.IconSize.MENU)
                kill.set_tooltip_text("Kill session")
                att.connect("clicked", lambda _b, n=name: open_session(
                    n, ["tmux", "new-session", "-A", "-s", n]))
                ren.connect("clicked", lambda _b, n=name: do_rename(n))
                kill.connect("clicked", lambda _b, n=name: do_kill(n))
                for b in (att, ren, kill):
                    row.pack_start(b, False, False, 0)
                listbox.pack_start(row, False, False, 0)
            listbox.show_all()

        create.connect("clicked", create_new)
        entry.connect("activate", create_new)
        refresh()
        dialog.show_all()
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.OK and chosen:
            self._add_session(chosen["label"], chosen["argv"], ICON_TMUX,
                              sub="tmux")

    @staticmethod
    def _tmux_prompt_rename(parent, old):
        d = Gtk.Dialog(title="Rename session", transient_for=parent,
                       modal=True)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                      "Rename", Gtk.ResponseType.OK)
        e = Gtk.Entry(text=old, margin=12, width_chars=24)
        e.set_activates_default(True)
        d.set_default_response(Gtk.ResponseType.OK)
        ok = d.get_widget_for_response(Gtk.ResponseType.OK)
        ok.set_can_default(True)
        d.set_default(ok)
        d.get_content_area().add(e)
        d.show_all()
        new = e.get_text().strip() if d.run() == Gtk.ResponseType.OK else None
        d.destroy()
        return new

    @staticmethod
    def _tmux_confirm_kill(parent, name):
        d = Gtk.MessageDialog(
            transient_for=parent, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Kill tmux session '{name}'?")
        d.format_secondary_text("Its running programs will be terminated.")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL,
                      "Kill", Gtk.ResponseType.OK)
        d.set_default_response(Gtk.ResponseType.CANCEL)
        resp = d.run()
        d.destroy()
        return resp == Gtk.ResponseType.OK

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

    @staticmethod
    def _ai_argv_plain(argv):
        """Strip the continue/resume tries from a stored AI argv, leaving just
        `cd <path>; exec <cli>` so a restored AI tab starts fresh."""
        marker = " || exit 1; "
        if len(argv) != 3 or argv[0] != "/bin/sh":
            return argv
        cmd = argv[2]
        i = cmd.find(marker)
        if i == -1:
            return argv
        cd_part = cmd[:i]                        # "cd <path>"
        exec_part = cmd[i + len(marker):].rsplit(" || ", 1)[-1]  # "exec <cli>"
        if not exec_part.startswith("exec "):
            return argv
        return ["/bin/sh", "-c", f"{cd_part}{marker}{exec_part}"]

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
        if self._load_settings().get("shell_inherit_cwd", False):
            cwd = self._focused_cwd()
            if cwd:
                path_default = cwd  # inherit the focused tab's path
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

        resume_chk = Gtk.CheckButton(
            label="Continue / resume previous session")
        resume_chk.set_active(False)

        try_hint = Gtk.Label(xalign=0)
        try_hint.get_style_context().add_class("session-sub")

        def update_try_hint(*_a):
            if not resume_chk.get_active():
                try_hint.set_text("Will start fresh (no continue/resume)")
                return
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
        resume_chk.connect("toggled", update_try_hint)
        update_try_hint()

        grid.attach(Gtk.Label(label="CLI", xalign=0), 0, 0, 1, 1)
        grid.attach(cli_box, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Path", xalign=0), 0, 1, 1, 1)
        grid.attach(path_box, 1, 1, 1, 1)
        grid.attach(resume_chk, 1, 2, 1, 1)
        grid.attach(try_hint, 0, 3, 2, 1)
        dialog.get_content_area().add(grid)
        self._dialog_enter_is_ok(dialog)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            tool = (cli.get_active_text() or "").strip()
            cwd = (path.get_text() or "").strip() or GLib.get_home_dir()
            cwd = os.path.expanduser(cwd)
            if tool:
                tries = []
                if resume_chk.get_active():
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
        elif action == "note_find":
            if row is not None and getattr(row, "kind", None) == "note":
                self._note_find_trigger(row)
            else:
                return False
        elif action == "term_find":
            if row is not None and getattr(row, "kind", None) == "term":
                self._term_find_trigger(row)
            else:
                return False
        elif action == "note_find_next":
            if (row is not None and getattr(row, "kind", None) == "note"
                    and getattr(row, "search_box", None)
                    and row.search_box.get_visible()):
                self._search_find(row, forward=True)
            elif (row is not None and getattr(row, "kind", None) == "term"
                    and getattr(row, "term_search_box", None)
                    and row.term_search_box.get_visible()):
                row._term_find_next()
            else:
                return False
        elif action == "note_find_prev":
            if (row is not None and getattr(row, "kind", None) == "note"
                    and getattr(row, "search_box", None)
                    and row.search_box.get_visible()):
                self._search_find(row, forward=False)
            elif (row is not None and getattr(row, "kind", None) == "term"
                    and getattr(row, "term_search_box", None)
                    and row.term_search_box.get_visible()):
                row._term_find_prev()
            else:
                return False
        elif action == "close_session":
            if row is not None:
                self._close_session(row)
        elif action == "rename_session":
            self._rename_session()
        elif action == "move_tab_up":
            self._move_session(-1)
        elif action == "move_tab_down":
            self._move_session(1)
        elif action in ("move_group_up", "move_group_down"):
            sel = self.listbox.get_selected_row()
            if sel is not None and getattr(sel, "group_color", None):
                self._move_group(sel.group_color,
                                 -1 if action == "move_group_up" else 1)
            else:
                return False
        elif action in ("prev_session", "next_session"):
            rows = self._session_rows()  # skip group headers
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

    def _on_editor_key(self, _view, event):
        return self._handle_shortcut(event)

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

    def _on_term_button(self, term, event):
        """Right-click menu for terminals: copy / paste / select all."""
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        menu = Gtk.Menu()
        copy = Gtk.MenuItem(label="Copy")
        copy.set_sensitive(term.get_has_selection())
        copy.connect("activate",
                     lambda *_: term.copy_clipboard_format(Vte.Format.TEXT))
        paste = Gtk.MenuItem(label="Paste")
        paste.connect("activate", lambda *_: term.paste_clipboard())
        select_all = Gtk.MenuItem(label="Select All")
        select_all.connect("activate", lambda *_: term.select_all())
        for item in (copy, paste, select_all):
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _on_term_drag_data_received(self, widget, context, x, y, selection_data, info, time):
        uris = selection_data.get_uris()
        if uris:
            paths = []
            for uri in uris:
                path = GLib.filename_from_uri(uri)[0]
                paths.append(shlex.quote(path))
            text = " ".join(paths)
            widget.feed_child(text.encode("utf-8"))
            widget.grab_focus()
        context.finish(True, False, time)

    def _on_note_drag_data_received(self, widget, context, x, y, selection_data, info, time):
        uris = selection_data.get_uris()
        if uris:
            for uri in uris:
                path = GLib.filename_from_uri(uri)[0]
                if os.path.isfile(path):
                    self._add_note_session(path=path)
        context.finish(True, False, time)



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

        term_head = Gtk.Label(xalign=0)
        term_head.set_markup("<b>Terminals</b>")
        inherit = Gtk.CheckButton(
            label="New shell / AI opens in the current tab's path")
        inherit.set_active(bool(s.get("shell_inherit_cwd", False)))
        inherit.set_tooltip_text(
            "+ Shell (Ctrl+Shift+T) and + AI start in the focused tab's "
            "working directory instead of home. Default is off.")

        ai_head = Gtk.Label(xalign=0)
        ai_head.set_markup("<b>AI</b>")
        ai_fresh = Gtk.CheckButton(
            label="Start AI tabs fresh after reopening (no continue/resume)")
        ai_fresh.set_active(bool(s.get("ai_fresh_on_restore", False)))
        ai_fresh.set_tooltip_text(
            "When tabit reopens, restored AI tabs launch the CLI without "
            "--continue / resume. Default is off (they continue).")

        hint = Gtk.Label(
            label="Stored in ~/.config/tabit/settings.json",
            xalign=0)
        hint.get_style_context().add_class("session-sub")
        box.pack_start(head, False, False, 0)
        box.pack_start(wrap, False, False, 0)
        box.pack_start(term_head, False, False, 0)
        box.pack_start(inherit, False, False, 0)
        box.pack_start(ai_head, False, False, 0)
        box.pack_start(ai_fresh, False, False, 0)
        box.pack_start(hint, False, False, 0)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            self._save_settings({"note_wrap": wrap.get_active(),
                                 "shell_inherit_cwd": inherit.get_active(),
                                 "ai_fresh_on_restore": ai_fresh.get_active()})
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
        half = (len(KEY_ACTIONS) + 1) // 2  # split into two columns
        for i, (action, label, _default) in enumerate(KEY_ACTIONS):
            col, r = (0, i) if i < half else (2, i - half)
            lbl = Gtk.Label(label=label, xalign=0)
            if col == 2:
                lbl.set_margin_start(24)  # gap between the two columns
            grid.attach(lbl, col, r, 1, 1)
            btn = Gtk.Button(label=self._accel_label_from_name(accels[action]))
            btn.set_hexpand(True)
            buttons[action] = btn
            grid.attach(btn, col + 1, r, 1, 1)

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


def _ensure_screen_sh():
    # write the bundled screen.sh out so the "screen" serial backend works
    # without anything in ~/.local/bin; rewrite only when it changed
    try:
        if os.path.isfile(SCREEN_SH_PATH):
            with open(SCREEN_SH_PATH) as f:
                if f.read() == SCREEN_SH:
                    return
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SCREEN_SH_PATH, "w") as f:
            f.write(SCREEN_SH)
        os.chmod(SCREEN_SH_PATH, 0o755)
    except OSError:
        pass  # serial "screen" will fail visibly if this could not be written


def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    GLib.set_prgname("tabit")
    _ensure_user_path()
    _ensure_screen_sh()

    # one instance is enough; the lock dies with the process
    lock = open(os.path.join(GLib.get_user_runtime_dir(), "tabit.lock"), "w")
    try:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("tabit is already running")

    # ask for the dark theme variant; the WM reads this to draw a dark
    # title bar (via the _GTK_THEME_VARIANT hint) instead of the pale one
    settings = Gtk.Settings.get_default()
    if settings is not None:
        settings.set_property("gtk-application-prefer-dark-theme", True)

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    Tabit().show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
