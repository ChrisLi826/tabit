# tabdesk

Vertical window tabs on the left edge of your screen — like browser tabs,
but for your whole desktop. Plus pinned files you can open with one click.

```
┌──────────┬───────────────────────┐
│ PINNED   │                       │
│ 📄 notes │                       │
│ 📁 work  │                       │
│──────────│      your desktop     │
│ WINDOWS  │                       │
│ ▣ Firefox│                       │
│ ▣ Files  │                       │
│ ▣ Editor │                       │
└──────────┴───────────────────────┘
```

One small Python file. No pip packages, no compiling — everything comes
from the Ubuntu archive.

## Requirements

- An X11 session (Wayland is not supported)
- Any window manager that follows EWMH: XFCE, GNOME (X11), KDE, MATE, ...
- Tested on Ubuntu / Xubuntu

## Install

```sh
git clone https://github.com/ChrisLi826/tabdesk.git
cd tabdesk
./install.sh      # installs deps via apt, copies to ~/.local/bin, autostarts on login
tabdesk &
```

To remove: `./install.sh --uninstall`

## Usage

| Action | Result |
|---|---|
| Left-click a window tab | Focus it (click again to minimize) |
| Middle-click a window tab | Close that window |
| Drag a file onto the sidebar | Pin it |
| Left-click a pin | Open it with the default app |
| Right-click a pin | Unpin it |
| Right-click empty space | Quit tabdesk |

Pins are stored in `~/.config/tabdesk/pins.json` (plain JSON, edit freely).

## How it works

- Window list and switching: [libwnck](https://gitlab.gnome.org/GNOME/libwnck)
  over the EWMH X11 standard — the same library XFCE's own taskbar uses.
- The sidebar reserves the screen edge with `_NET_WM_STRUT_PARTIAL`,
  so maximized windows never cover it.
- Opening files: `Gio.AppInfo.launch_default_for_uri` (same as `xdg-open`).

## License

MIT
