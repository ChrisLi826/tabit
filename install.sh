#!/bin/sh
# Install tabdesk for the current user (Ubuntu/Debian).
set -e
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/tabdesk" "$HOME/.config/autostart/tabdesk.desktop"
    echo "tabdesk removed (pins kept in ~/.config/tabdesk)"
    exit 0
fi

sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 x11-utils

mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart"
install -m 755 tabdesk.py "$HOME/.local/bin/tabdesk"

cat > "$HOME/.config/autostart/tabdesk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tabdesk
Comment=Vertical window tabs on the left screen edge
Exec=$HOME/.local/bin/tabdesk
EOF

echo "tabdesk installed. Start it now with: ~/.local/bin/tabdesk &"
echo "It will also start automatically on next login."
