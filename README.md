# tabit

Terminal sessions as vertical tabs on the left — one window for all your
shells, serial consoles and remote logins.

```
┌───────────┬─────────────────────────────┐
│ SESSIONS  │                             │
│ ▌shell    │  $ make flash               │
│  ttyUSB0  │  ...                        │
│  ttyUSB1  │                             │
│  ssh ecw  │      (real terminal,        │
│           │       VTE engine)           │
│ + Shell   │                             │
│ + Serial  │                             │
│ + Command │                             │
└───────────┴─────────────────────────────┘
```

One small Python file. No pip packages, no compiling — everything comes
from the Ubuntu archive.

## Requirements

- Linux with GTK3 + VTE (X11 or Wayland)
- `picocom` for serial sessions
- Tested on Ubuntu / Xubuntu

## Install

```sh
git clone https://github.com/ChrisLi826/tabit.git
cd tabit
./install.sh      # installs deps via apt, copies to ~/.local/bin, adds app menu entry
~/.local/bin/tabit &
```

To remove: `./install.sh --uninstall`

## Usage

| Action | Result |
|---|---|
| `+ Serial` | Pick device, baud (default 115200), and tool: `screen.sh` / `kermit` / `picocom` |
| `+ Shell` | New tab running your login shell |
| `+ AI` | Pick AI CLI and working directory; tries `--continue` → `resume --last` → `--resume latest` → plain. **Edit list…** manages the CLI dropdown (`~/.config/tabit/ai_clis.json`) |
| `+ Command` | Run anything (e.g. `ssh root@192.168.1.1`) in a new tab |
| Click a tab | Switch to that session |
| Double-click a tab / right-click → Rename… / `F2` | Rename (popover bubble to the right of the tab) |
| `x` on a tab (shown on hover) | Close that session |
| `Ctrl+Shift+S` / `Ctrl+Shift+T` / `Ctrl+Shift+A` | New serial / shell / AI (inserted under the current tab) |
| `Ctrl+Shift+W` | Close current session |
| `Ctrl+PageUp` / `Ctrl+PageDown` | Previous / next session |
| `Ctrl+Shift+PageUp` / `Ctrl+Shift+PageDown` | Move current tab up / down |
| `Ctrl+Shift+C` / `Ctrl+Shift+V` | Copy / paste |
| `Shortcuts…` (sidebar) | Edit any of the shortcuts above |

A blue dot on a tab means that session printed output while you were
looking elsewhere. When a session's process ends (device unplugged,
`exit`, picocom quit) the tab stays, greyed and marked `exited`, so
you keep the scrollback — press its `x` to really close it.

Serial tool defaults to `screen.sh` (multi-attach + logfile). `kermit`
uses `~/senaoenv/kermrc` when present (`-c -E`). `picocom` quit is
`Ctrl-A Ctrl-X`. Closing the last tab quits tabit.

Tabs are remembered: the next start restores the same set of sessions
as fresh processes (serial consoles reconnect, shells start clean —
scrollback is not kept). Stored in `~/.config/tabit/sessions.json`.

Keyboard shortcuts are editable via **Shortcuts…** in the sidebar
(or hand-edit `~/.config/tabit/keys.json`). Defaults match the table
above; **Reset defaults** in the dialog restores them.

## Roadmap

- File browser pane + text editing tabs
- Saved session profiles (named serial/ssh setups)

## License

MIT
