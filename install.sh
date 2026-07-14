#!/bin/sh
# Install tabit for the current user (Ubuntu/Debian).
set -e
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/tabit" \
          "$HOME/.local/share/applications/tabit.desktop" \
          "$HOME/.config/autostart/tabit.desktop"
    echo "tabit removed"
    exit 0
fi

sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 \
    gir1.2-gtksource-4 gir1.2-webkit2-4.0 python3-markdown picocom

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m 755 tabit.py "$HOME/.local/bin/tabit"

# leftover from the pre-v2 window-tab version
rm -f "$HOME/.config/autostart/tabit.desktop"

cat > "$HOME/.local/share/applications/tabit.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tabit
Comment=Terminal sessions as vertical tabs
Exec=$HOME/.local/bin/tabit
Icon=utilities-terminal
Categories=System;TerminalEmulator;
EOF

echo "tabit installed. Start it with: ~/.local/bin/tabit &"
echo "(also available from the app menu)"
