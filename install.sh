#!/bin/sh
# Install tabit for the current user (Ubuntu/Debian).
set -e
cd "$(dirname "$0")"

if [ "$1" = "--uninstall" ]; then
    rm -f "$HOME/.local/bin/tabit" \
          "$HOME/.local/share/applications/tabit.desktop" \
          "$HOME/.local/share/icons/hicolor/scalable/apps/tabit.svg" \
          "$HOME/.config/autostart/tabit.desktop"
    rm -rf "$HOME/.local/share/tabit"
    echo "tabit removed"
    exit 0
fi

# Prefer sudo -A when SUDO_ASKPASS is set (GUI password from tabit update).
if [ -n "${SUDO_ASKPASS:-}" ]; then
    SUDO="sudo -A"
else
    SUDO="sudo"
fi

# Non-interactive apt (no more config prompts during GUI update).
export DEBIAN_FRONTEND=noninteractive

$SUDO apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 \
    gir1.2-gtksource-4 python3-markdown picocom screen

# WebKit for the note Markdown preview: 4.0 on older Ubuntu, 4.1 on 24.04+.
# Optional - if neither is available the app just runs without the preview.
$SUDO apt-get install -y gir1.2-webkit2-4.0 \
    || $SUDO apt-get install -y gir1.2-webkit2-4.1 \
    || echo "WebKit not found; note Markdown preview will be disabled"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" \
         "$HOME/.local/share/icons/hicolor/scalable/apps" \
         "$HOME/.local/share/tabit"
install -m 755 tabit.py "$HOME/.local/bin/tabit"
install -m 644 tabit.svg "$HOME/.local/share/icons/hicolor/scalable/apps/tabit.svg"
# agent status: pure detector + herdr manifests (no herdr binary)
install -m 644 agent_detect.py "$HOME/.local/share/tabit/agent_detect.py"
install -m 644 agent_status.py "$HOME/.local/share/tabit/agent_status.py"
install -m 644 tabit_chrome.toml "$HOME/.local/share/tabit/tabit_chrome.toml"
install -m 755 sudo_askpass.py "$HOME/.local/share/tabit/sudo_askpass.py"
rm -rf "$HOME/.local/share/tabit/agent-detection" "$HOME/.local/share/tabit/vendor"
cp -a agent-detection "$HOME/.local/share/tabit/agent-detection"
cp -a vendor "$HOME/.local/share/tabit/vendor"
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
