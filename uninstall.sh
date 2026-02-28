#!/bin/bash
# Mural - Uninstall
# Keeps config (~/.config/mural) and cache (~/.cache/mural) intact.

APP="mural"
echo "Removing $APP..."

# App files
rm -rf "$HOME/.local/share/$APP"
rm -f "$HOME/.local/bin/$APP"

# Desktop entries (all possible names)
rm -f "$HOME/.local/share/applications/mural.desktop"
rm -f "$HOME/.local/share/applications/io.github.gaorfg-bit.Mural.desktop"

# Icons (all possible locations)
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.gaorfg-bit.Mural.svg"
rm -f "$HOME/.local/share/icons/hicolor/48x48/apps/mural-app.png"

# Force GNOME to remove the icon from the app list immediately
update-desktop-database "$HOME/.local/share/applications"
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/" 2>/dev/null

echo "Done. Config and cache kept in:"
echo "  ~/.config/mural"
echo "  ~/.cache/mural"
