#!/bin/bash
# Mural - GNOME Force Recognition

DIR="$(cd "$(dirname "$0")" && pwd)"

APP="mural"
LAUNCHER="mural-launcher"
TGT="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICO_DIR="$HOME/.local/share/icons/hicolor/48x48/apps"

echo "Deep cleaning..."
rm -f "$APP_DIR/io.github.gaorfg-bit.Mural.desktop"
rm -f "$HOME/.local/share/applications/mural.desktop"

echo "Installing..."
mkdir -p "$TGT" "$BIN_DIR" "$APP_DIR" "$ICO_DIR"
cp -r "$DIR/mural" "$DIR/LICENSE" "$DIR/requirements.txt" "$TGT/"

# Icon fix
cp "$TGT/mural/data/icons/io.github.gaorfg-bit.Mural.png" "$ICO_DIR/mural-app.png"

echo "Creating wrapper..."
install -m 755 "$DIR/scripts/$LAUNCHER" "$BIN_DIR/$LAUNCHER"
ln -sf "$BIN_DIR/$LAUNCHER" "$BIN_DIR/$APP"
chmod +x "$BIN_DIR/$LAUNCHER"

echo "Creating desktop file (simple ID)..."
cat <<EOF > "$APP_DIR/mural.desktop"
[Desktop Entry]
Name=Mural
Comment=Wallpaper Manager
Exec=$LAUNCHER
Icon=mural-app
Type=Application
Terminal=false
Categories=Utility;Settings;
StartupNotify=true
StartupWMClass=io.github.gaorfg_bit.Mural
Keywords=wallpaper;background;
EOF

echo "Forcing GNOME refresh..."
chmod +x "$APP_DIR/mural.desktop"
update-desktop-database "$APP_DIR"
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/"

echo "Done. Search for 'Mural' in your activities."