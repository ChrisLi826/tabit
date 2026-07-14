#!/bin/sh
# Install tabit for the current user (Ubuntu/Debian).
set -e
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/tabit" \
          "$HOME/.local/share/applications/tabit.desktop" \
          "$HOME/.local/share/icons/hicolor/scalable/apps/tabit.svg" \
          "$HOME/.config/autostart/tabit.desktop"
    echo "tabit removed"
    exit 0
fi

sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 \
    gir1.2-gtksource-4 python3-markdown picocom

# WebKit for the note Markdown preview: 4.0 on older Ubuntu, 4.1 on 24.04+.
# Optional - if neither is available the app just runs without the preview.
sudo apt-get install -y gir1.2-webkit2-4.0 \
    || sudo apt-get install -y gir1.2-webkit2-4.1 \
    || echo "WebKit not found; note Markdown preview will be disabled"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/scalable/apps"
install -m 755 tabit.py "$HOME/.local/bin/tabit"
install -m 644 tabit.svg "$HOME/.local/share/icons/hicolor/scalable/apps/tabit.svg"
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# leftover from the pre-v2 window-tab version
rm -f "$HOME/.config/autostart/tabit.desktop"

cat > "$HOME/.local/share/applications/tabit.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=tabit
Comment=Terminal sessions as vertical tabs
Exec=$HOME/.local/bin/tabit
Icon=tabit
Categories=System;TerminalEmulator;
EOF

echo "tabit installed. Start it with: ~/.local/bin/tabit &"
echo "(also available from the app menu)"
