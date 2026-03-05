#!/bin/bash
# Mural - Uninstall
# Keeps config (~/.config/mural) and cache (~/.cache/mural) intact.

APP="mural"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/mural.desktop"
DESKTOP_FILE_LEGACY="$APP_DIR/io.github.gaorfgbit.Mural.desktop"
echo "Removing $APP..."

# App files
rm -rf "$HOME/.local/share/$APP"
rm -f "$HOME/.local/bin/$APP"
rm -f "$HOME/.local/bin/mural-launcher"

# Desktop entries (all possible names)
rm -f "$DESKTOP_FILE"
rm -f "$DESKTOP_FILE_LEGACY"

# Icons (all possible locations)
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.gaorfgbit.Mural.svg"
rm -f "$HOME/.local/share/icons/hicolor/256x256/apps/io.github.gaorfgbit.Mural.png"

# Force GNOME to remove the icon from the app list immediately
update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/" 2>/dev/null

echo "Done. Config and cache kept in:"
echo "  ~/.config/mural"
echo "  ~/.cache/mural"
