# Using tabit

The buttons at the foot of the tab list, the keys, and how tabs behave.
Back to the [README](../README.md).

## Buttons and keys

| Action | Result |
|---|---|
| `+ Serial` | Pick device, baud (default 115200), and tool (a device a tab already has open says which tab): `screen` (bundled `screen.sh`) / `kermit` / `picocom`; or `ssh` / `telnet` to a host + port (for network console servers) |
| `+ Terminal` | New tab running your login shell |
| `+ AI` | Pick AI CLI and working directory. Two separate choices: **Create new session** (on by default; untick to continue the last one), and **Resume when tabit reopens** (what the tab does after a restart; the rename popover changes it later). A tab comes back to its own session, not just the newest one in the folder: claude and the xAI grok start with an id tabit picks (`--session-id`, used only when the CLI's `--help` lists it); codex and agy pick their own, and tabit reads it from the session file the running agent holds open, once the first message has been sent. An optional **Session ID** resumes that exact session (tried first, normal continue/resume stays as fallback). **Run inside tmux** keeps the agent alive across restarts and lists the ones still running, each marked with the tab holding it where there is one. **Edit list…** manages CLI names and per-CLI continue/resume tries (`~/.config/tabit/ai_clis.json`) |
| `+ Open` | Blank buffer to type in, or open a file to read. GtkSourceView editor + **Markdown Preview** (WebKit); an image, PDF or HTML page opens rendered, full width; bottom tools: Base64 / JSON Format; wrap in **Settings…**; huge-line guards. The preview toggle is remembered across restarts |
| `Settings…` | Theme, fonts, terminal line spacing, note wrap, and other prefs (`settings.json`) |
| `+ Command` | Run anything (e.g. `ssh root@192.168.1.1`) in a new tab |
| `+ tmux` | Attach to a running tmux session or create one; rename / kill sessions from the list. A session a tab already holds is marked with that tab's group and name |
| Click a tab | Switch to that session |
| Double-click a tab / right-click → Rename… / `F2` | Rename (popover bubble to the right of the tab) |
| `x` on a tab (shown on hover) | Close that session |
| `Ctrl+Shift+S` / `Ctrl+Shift+T` / `Ctrl+Shift+A` / `Ctrl+Shift+N` | New serial / terminal / AI / open |
| `Ctrl+Shift+D` / `Ctrl+Shift+M` | New command / tmux |
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

Remote (the page on your phone) has its own page: [remote.md](remote.md).

## Tabs

A blue dot on a tab means that session printed output while you were
looking elsewhere. When a session's process ends (device unplugged,
`exit`, picocom quit) the tab stays, greyed and marked `exited`, so
you keep the scrollback — press its `x` to really close it.

Serial tool defaults to `screen` — a bundled `screen.sh` wrapper
(multi-attach + logfile), written to `~/.config/tabit/screen.sh`. `picocom` quit is
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

Closing a tab ends the tmux session only when tabit made it for that tab
(an AI tab's `ai-…` session). A session of your own, opened with **+ tmux**,
is only detached: it keeps running, and you can attach to it again.

## Two panes

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

## Shortcuts

Keyboard shortcuts are editable via **Shortcuts…** in the sidebar
(or hand-edit `~/.config/tabit/keys.json`). Defaults match the table
above; **Reset defaults** in the dialog restores them.
