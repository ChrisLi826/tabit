#!/bin/sh
# Install tabit for the current user (Ubuntu/Debian).
set -e
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/tabit" "$HOME/.config/autostart/tabit.desktop"
    echo "tabit removed (pins kept in ~/.config/tabit)"
    exit 0
fi

sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-wnck-3.0 x11-utils

mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart"
install -m 755 tabit.py "$HOME/.local/bin/tabit"

cat > "$HOME/.config/autostart/tabit.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tabit
Comment=Vertical window tabs on the left screen edge
Exec=$HOME/.local/bin/tabit
EOF

echo "tabit installed. Start it now with: ~/.local/bin/tabit &"
echo "It will also start automatically on next login."
