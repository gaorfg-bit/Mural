#!/bin/bash
# Mural - GNOME Force Recognition

APP="mural"
TGT="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICO_DIR="$HOME/.local/share/icons/hicolor/48x48/apps"

echo "Deep cleaning..."
rm -f "$APP_DIR/io.github.gaorfg-bit.Mural.desktop"
rm -f "$HOME/.local/share/applications/mural.desktop"

echo "Installing..."
mkdir -p "$TGT" "$BIN_DIR" "$APP_DIR" "$ICO_DIR"
cp -r mural LICENSE requirements.txt "$TGT/"

# Icon fix
cp "$TGT/mural/data/icons/io.github.gaorfg-bit.Mural.png" "$ICO_DIR/mural-app.png"

echo "Creating wrapper..."
cat <<EOF > "$BIN_DIR/$APP"
#!/bin/bash
export PYTHONPATH="$TGT"
exec python3 "$TGT/mural/main.py" "\$@"
EOF
chmod +x "$BIN_DIR/$APP"

echo "Creating desktop file (simple ID)..."
# Simplified ID to force GNOME to refresh
cat <<EOF > "$APP_DIR/mural.desktop"
[Desktop Entry]
Name=Mural
Comment=Wallpaper Manager
Exec=$BIN_DIR/$APP
Icon=mural-app
Type=Application
Terminal=false
Categories=Utility;Settings;
StartupNotify=true
Keywords=wallpaper;background;
EOF

echo "Forcing GNOME refresh..."
chmod +x "$APP_DIR/mural.desktop"
update-desktop-database "$APP_DIR"
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/"

echo "Done. Search for 'Mural' in your activities."