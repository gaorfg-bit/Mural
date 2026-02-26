#!/bin/bash
# Mural - Uninstallation Script

APP="mural"
echo "Removing $APP..."

rm -rf "$HOME/.local/share/$APP"
rm -f "$HOME/.local/bin/$APP"
rm -f "$HOME/.local/share/applications/io.github.gaorfg-bit.Mural.desktop"
rm -f "$HOME/.local/share/icons/hicolor/scalable/apps/io.github.gaorfg-bit.Mural.svg"

echo "Done."
