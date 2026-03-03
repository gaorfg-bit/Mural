#!/bin/bash
# Mural - GNOME Force Recognition

DIR="$(cd "$(dirname "$0")" && pwd)"

APP="mural"
LAUNCHER="mural-launcher"
TGT="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_SCALABLE_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

echo "Deep cleaning..."
rm -f "$HOME/.local/share/applications/mural.desktop"
rm -f "$HOME/.local/share/applications/io.github.gaorfgbit.Mural.desktop"

echo "Installing..."
mkdir -p "$TGT" "$BIN_DIR" "$APP_DIR" "$ICON_SCALABLE_DIR"
cp -r "$DIR/mural" "$DIR/LICENSE" "$DIR/requirements.txt" "$TGT/"

# Icon install
cp "$TGT/mural/data/icons/io.github.gaorfgbit.Mural.svg" "$ICON_SCALABLE_DIR/io.github.gaorfgbit.Mural.svg"

echo "Creating wrapper..."
install -m 755 "$DIR/scripts/$LAUNCHER" "$BIN_DIR/$LAUNCHER"
ln -sf "$BIN_DIR/$LAUNCHER" "$BIN_DIR/$APP"
chmod +x "$BIN_DIR/$LAUNCHER"

echo "Creating desktop file (reverse-DNS ID)..."
cat <<EOF > "$APP_DIR/io.github.gaorfgbit.Mural.desktop"
[Desktop Entry]
Name=Mural
Comment=Wallpaper Manager
Exec=$LAUNCHER
Icon=io.github.gaorfgbit.Mural
Type=Application
Terminal=false
Categories=Utility;Settings;
StartupNotify=true
StartupWMClass=io.github.gaorfgbit.Mural
DBusActivatable=true
Keywords=wallpaper;background;
EOF

echo "Forcing GNOME refresh..."
chmod +x "$APP_DIR/io.github.gaorfgbit.Mural.desktop"
update-desktop-database "$APP_DIR"
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/"

echo "Done. Search for 'Mural' in your activities."