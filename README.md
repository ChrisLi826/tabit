# tabit

[![Release](https://img.shields.io/github/v/release/ChrisLi826/tabit?display_name=tag&sort=semver)](https://github.com/ChrisLi826/tabit/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**tabit** is a Linux GTK terminal where every session — a shell, a serial
console, an AI CLI, or a note — is a **color-coded tab down the left edge**
of one window.

<p align="center">
  <img src="assets/hero.png" alt="tabit — left session sidebar with color groups, AI status icons, serial and shell tabs" width="900">
</p>

- **Serial consoles** — USB-serial, `screen` multi-attach, kermit, picocom;
  group devices by project color and collapse what you are not using
- **Multi-AI status** — Claude / Codex / Grok (and more): ▶ working ·
  || idle · ? needs input · ✔ done; off-viewport peeks and
  collapsed-group summaries
- **Native GTK + VTE** — no Electron, no pip; install from the Ubuntu archive
  with one script

## Install

```sh
git clone https://github.com/ChrisLi826/tabit.git
cd tabit
./install.sh      # apt deps + ~/.local/bin/tabit + app menu entry
~/.local/bin/tabit &
```

To remove: `./install.sh --uninstall`

In-app **Check for updates** reinstalls the latest release (git pull when a
checkout exists; otherwise downloads the release tarball). May ask for your
sudo password for apt.

## Requirements

- Linux with GTK3 + VTE + GtkSourceView 4 (X11 or Wayland)
- WebKit2 + `python3-markdown` for note Markdown preview
- libnotify (`gir1.2-notify-0.7`) for AI status notifications
- `picocom` for serial sessions
- Tested on Ubuntu / Xubuntu

## Usage

| Action | Result |
|---|---|
| `+ Serial` | Pick device, baud (default 115200), and tool: `screen` (bundled `screen.sh`) / `kermit` / `picocom`; or `ssh` / `telnet` to a host + port (for network console servers) |
| `+ Terminal` | New tab running your login shell |
| `+ AI` | Pick AI CLI and working directory; an optional **Session ID** resumes that exact session (tried first, normal continue/resume stays as fallback). **Run inside tmux** keeps the agent alive across restarts and lists the ones still running, each marked with the tab holding it where there is one. **Edit list…** manages CLI names and per-CLI continue/resume tries (`~/.config/tabit/ai_clis.json`) |
| `+ Open` | Blank buffer to type in, or open a file to read. GtkSourceView editor + **Markdown Preview** (WebKit); an image, PDF or HTML page opens rendered, full width; bottom tools: Base64 / JSON Format; wrap in **Settings…**; huge-line guards. The preview toggle is remembered across restarts |
| `Settings…` | Theme, fonts, terminal line spacing, note wrap, and other prefs (`settings.json`) |
| `+ Command` | Run anything (e.g. `ssh root@192.168.1.1`) in a new tab |
| `+ tmux` | Attach to a running tmux session or create one; rename / kill sessions from the list. A session a tab already holds is marked with that tab's group and name |
| Click a tab | Switch to that session |
| Double-click a tab / right-click → Rename… / `F2` | Rename (popover bubble to the right of the tab) |
| `x` on a tab (shown on hover) | Close that session |
| `Ctrl+Shift+S` / `Ctrl+Shift+T` / `Ctrl+Shift+A` / `Ctrl+Shift+N` | New serial / terminal / AI / open |
| `Ctrl+Shift+E` / `Ctrl+Shift+D` / `Ctrl+Shift+M` | New connect / command / tmux |
| `Ctrl+S` | Save note (when a note tab is selected) |
| `Ctrl+Alt+B` / `Ctrl+Alt+Shift+B` | Note Base64 encode / decode |
| `Ctrl+Alt+J` | Note JSON format (also validates) |
| `Ctrl+Alt+M` | Note Markdown preview toggle |
| `Ctrl+Shift+W` | Close current session |
| `Ctrl+PageUp` / `Ctrl+PageDown` | Previous / next session |
| `Ctrl+Shift+PageUp` / `Ctrl+Shift+PageDown` | Move current tab up / down |
| `Ctrl+Alt+R` | Toggle the right content pane (two sessions side by side) |
| `Ctrl+Alt+P` | Pin the selected session to the right pane |
| `Ctrl+Tab` | Move focus between the left and right pane; with no right pane open, jump back to the last tab you stayed on (press again to come back). Tabs you only passed through do not count: a tab is "stayed on" once it has been selected for 0.8s **and** the modifier is released, so holding Ctrl through a run of `Ctrl+PageUp`/`PageDown` is one move however long it pauses |
| `Ctrl+Alt+W` | Swap what the left and right panes show |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste |
| `Ctrl`+click a URL in a terminal | Open it in the browser. Hover underlines the match; right-click a link adds **Open Link** / **Copy Link**. Works on plain `http(s)://` / `ftp://` text and on OSC 8 hyperlinks (tmux hides OSC 8 unless `allow-passthrough` is on; plain URLs still match) |
| `Ctrl`+click a file path in a terminal | Open it. A text file (decided by reading it, not by its suffix — `Makefile` counts) opens as a note tab; a `path:line` lands on that line; an `.html` page, an image (`.png` `.jpg` `.gif` `.webp` `.bmp` `.ico` `.svg`) or a `.pdf` opens rendered and full width, and so does a `.md`, which is written to be read even though it is also text. `Ctrl+Alt+M` swaps to the source where there is any, and right-click → **Open in Browser** hands a page over. A path an app wrapped onto a second line is put back together, as long as the result is a real file. A text file too big or too long-lined for the editor asks what to do — `$EDITOR` in a tab, or Base64 decode — rather than going to the desktop, which freezes on it just the same. Anything else goes to the desktop's handler. Absolute or `~/` paths only — a relative one would resolve against tabit's directory, not the terminal's. Paths with spaces or non-ASCII characters match up to the first such character, deliberately: a wider pattern underlines half of every JSON dump and serial log. Right-click adds **Open Path** / **Copy Path**, which is what a `.bin` usually wants |
| `Shortcuts…` (sidebar) | Edit any of the shortcuts above. Each sidebar button marks its own key the way a menu does — `+ (S)erial`, `+ t(M)ux`, and the letter on the end where the word has none — and follows an edit here. The modifiers are the same on all of them, so only the letter is shown |

When an AI tab stops working — it finished, or it wants input — a popup
inside tabit says so, **at the foot of the tab list**, shaped like the tab
row it is about: the group's colour bar, the same status glyph, the group
and tab names, and the time. Click anywhere on it to switch to that tab.
When tabit is **not** the window in front, the same news also goes to the
desktop as a system notification, so it reaches you either way — the popup
is what is still waiting when you come back. Nothing pops for the tab you
are already looking at, and nothing pops for a status that changes back
within a few seconds, which is a detection wobble rather than news.

Opening a tab any other way takes its popup down too, and one tab never
has two: a second one replaces the first. **Nothing is ever thrown away
to make room.** Three of them show at a time, newest at the bottom; the
rest wait behind `3 more · 1 need input ▴`, which opens the whole list
oldest-first and folds it back again. **Clear all** takes down the
waiting ones as well. The AI counts in the row just below are never
covered, whatever the stack is doing.

One that goes away on its own only counts down while it is on screen and
tabit is in front, and never under the pointer — a popup that expired
where nobody could read it was never shown. The wheel over a popup
reaches the terminal underneath, since the stack hangs past a narrow tab
list.

It stays until you dismiss it, because urgency is the only say an app has
over the desktop half: GNOME gives every other notification four seconds
and ignores the timeout an app asks for. **Settings… → AI** turns it off
or drops it to one that goes away on its own. The desktop half needs
`gir1.2-notify-0.7`, which the installer pulls in; the in-app half works
without it.

A blue dot on a tab means that session printed output while you were
looking elsewhere. When a session's process ends (device unplugged,
`exit`, picocom quit) the tab stays, greyed and marked `exited`, so
you keep the scrollback — press its `x` to really close it.

The foot of the sidebar counts **every** AI tab by status — `?` blocked,
`✓` done, play working, pause idle — next to a clock. The off-viewport
peeks only cover tabs scrolled out of sight, so with a long list an agent
waiting on you could still be missed; this row cannot miss one. Click a
count to jump to that agent, and click again to cycle through the rest.

Serial tool defaults to `screen` — a bundled `screen.sh` wrapper
(multi-attach + logfile), written to `~/.config/tabit/screen.sh`. `kermit`
uses `~/senaoenv/kermrc` when present (`-c -E`). `picocom` quit is
`Ctrl-A Ctrl-X`. Closing the last tab quits tabit.

Tabs are remembered: the next start restores the same set of sessions
as fresh processes (serial consoles reconnect, shells start clean —
scrollback is not kept). Stored in `~/.config/tabit/sessions.json`.

An AI tab can instead **run inside tmux** (tick it in **+ AI**): the agent
keeps running when tabit closes, and reopening the tab reattaches to the
turn it was in the middle of, rather than replaying the conversation with
`--continue`. **+ AI** lists the AI sessions still running — CLI, folder,
and whether a tab is attached — so a detached agent can be picked back up
or killed. tabit sets `status off`, window-title passthrough and prefix
`C-a` on those sessions, so the AI status icons keep working and `Ctrl+B`
still reaches the agent.

Two sessions can share the window: `Ctrl+Alt+R` opens a **right content
pane**, `Ctrl+Alt+P` pins the selected tab there. Drag the divider to
resize; the split and the pinned session come back on the next start. Each
pane has a small header with an `L` / `R` badge and an `x` that closes only
that pane — closing the right pane keeps the left one full width, closing
the left one promotes the right session. The tab list can sit **left,
right, or between the two panes** (**Settings… → Tab list position**).

With three columns on screen, the middle one can be nudged sideways: drag
its header — the tab list's **SESSIONS** bar when the list is centered, or
the `L` / `R` bar when the list is parked on an edge. The middle column
keeps its width and the two outer ones trade. Every drag stops where a
neighbour hits its minimum, so the left and right window edges stay put.

Keyboard shortcuts are editable via **Shortcuts…** in the sidebar
(or hand-edit `~/.config/tabit/keys.json`). Defaults match the table
above; **Reset defaults** in the dialog restores them.

## Where tabit fits

If you already use **tmux** or **Tilix**, keep them for session persistence
inside a host and for many-way tiling. tabit is a **session dock**: many
independent VTE terminals (and notes) as one tab list, with any two of them
side by side when you need it — especially when you juggle **serial
boards**, **SSH consoles**, and **several AI CLIs** and want at-a-glance
status without tiling windows by hand.

## Roadmap

- File browser pane + text editing tabs
- Saved session profiles (named serial/ssh setups)

## Releasing (cuisine codenames)

Each release gets a fresh **Taiwan-origin cuisine** English codename. There is
**no public menu of upcoming names** in the repo (that would spoil the
surprise). RD picks the name when cutting the release.

Public label:

```text
v1.7.12 · Oyster Omelette
```

That same string appears in:

- GitHub Release **title** (and preferably the notes heading)
- In-app **Update** dialog (`New version: …`)
- **Settings** version line (About)

### Naming rules

| Rule | Detail |
|---|---|
| Origin | Must be **Taiwan-origin cuisine** (not limited to street snacks) |
| Major / minor | 1st and 2nd version digits → more **internationally known** names |
| Patch | 3rd digit → more **obscure** names |
| Language | **English** display name in the public label; optional Chinese only in private notes if needed — never a browsable public catalog |
| Reuse | Do not reuse a name already in shipped history — except a **hotfix**, which is the same release with bugs taken out and keeps its name (`v1.8.3` → `v1.8.3.1`) |

### Files

| File | Role |
|---|---|
| `APP_VERSION` / `APP_CODENAME` in `tabit.py` | What the running build shows |
| `release-codenames.json` | **Shipped history only** (no future candidates) |
| `scripts/release-codename.py` | `list` / `title` / `record` / `check` helpers |

### Checklist when cutting a release

1. Pick a fresh English codename per the rules above (`python3 scripts/release-codename.py list` shows names already used).
2. Bump `APP_VERSION` and set `APP_CODENAME` in `tabit.py` to that English name.
3. Record history (no candidate pool):  
   `python3 scripts/release-codename.py record vX.Y.Z --name "English Name"`
4. Commit, tag `vX.Y.Z`, and create the GitHub release **with the codename in the title**:

```sh
TITLE=$(python3 scripts/release-codename.py title)
gh release create vX.Y.Z -t "$TITLE" -F .release/vX.Y.Z-notes.md
```

5. Confirm four places spell the **same** `vX.Y.Z · Name` string:
   - `APP_CODENAME` in `tabit.py`
   - shipped entry in `release-codenames.json`
   - Settings → Version (About)
   - Check for Updates / Update dialog (and GitHub release title)

## License

MIT
