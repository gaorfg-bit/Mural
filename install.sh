#!/bin/bash
# Mural - Installer
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="mural"
LAUNCHER="mural-launcher"
TGT="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_SCALABLE_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "Deep cleaning..."
rm -f "$HOME/.local/share/applications/mural.desktop"
rm -f "$HOME/.local/share/applications/io.github.gaorfgbit.Mural.desktop"
rm -rf "$TGT"

echo "Installing..."
mkdir -p "$TGT" "$BIN_DIR" "$APP_DIR" "$ICON_SCALABLE_DIR" "$SYSTEMD_DIR"
cp -r "$DIR/mural" "$DIR/LICENSE" "$DIR/requirements.txt" "$TGT/"

echo "Installing icon..."
cp "$TGT/mural/data/icons/io.github.gaorfgbit.Mural.svg" "$ICON_SCALABLE_DIR/io.github.gaorfgbit.Mural.svg"

echo "Creating launcher..."
install -m 755 "$DIR/scripts/$LAUNCHER" "$BIN_DIR/$LAUNCHER"
ln -sf "$BIN_DIR/$LAUNCHER" "$BIN_DIR/$APP"

echo "Creating desktop entry..."
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
chmod +x "$APP_DIR/io.github.gaorfgbit.Mural.desktop"

echo "Installing daemon..."
cat <<EOF > "$SYSTEMD_DIR/mural.service"
[Unit]
Description=Mural Wallpaper Daemon
Documentation=https://github.com/gaorfg-bit/mural
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $TGT/mural/mural-daemon-service.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical-session.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now mural.service

echo "Refreshing GNOME..."
update-desktop-database "$APP_DIR"
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/" 2>/dev/null || true

echo "Done. Search for 'Mural' in your activities."
