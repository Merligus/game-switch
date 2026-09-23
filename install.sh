#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$apps" "$icons"
install -m644 "$here/icons/gameswitch.svg" "$icons/gameswitch.svg"
sed "s|^Exec=.*|Exec=$here/bin/gameswitch|" "$here/gameswitch.desktop" > "$apps/gameswitch.desktop"
chmod +x "$here/bin/gameswitch"
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v xdg-icon-resource      >/dev/null && xdg-icon-resource forceupdate --theme hicolor 2>/dev/null || true
echo "Installed $apps/gameswitch.desktop"
echo "Installed $icons/gameswitch.svg"
