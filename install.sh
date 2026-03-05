#!/bin/bash
# Mural - Installer
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
APP="mural"
LAUNCHER="mural-launcher"
TGT="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$APP_DIR/mural.desktop"
DESKTOP_FILE_LEGACY="$APP_DIR/io.github.gaorfgbit.Mural.desktop"
ICON_SCALABLE_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ICON_256_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "Deep cleaning..."
rm -f "$DESKTOP_FILE"
rm -f "$DESKTOP_FILE_LEGACY"
rm -rf "$TGT"

if [ -d "/usr/share/mural" ] && [ ! -f "/usr/share/mural/mural/main.py" ]; then
  echo "Warning: incomplete system installation detected in /usr/share/mural"
  echo "The user installation in $TGT will be preferred by the launcher."
fi

echo "Installing..."
mkdir -p "$TGT" "$BIN_DIR" "$APP_DIR" "$ICON_SCALABLE_DIR" "$ICON_256_DIR" "$SYSTEMD_DIR"
cp -r "$DIR/mural" "$DIR/LICENSE" "$DIR/requirements.txt" "$TGT/"
printf "%s\n" "installed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$TGT/.install_stamp"

echo "Installing icon..."
cp "$TGT/mural/data/icons/io.github.gaorfgbit.Mural.svg" "$ICON_SCALABLE_DIR/io.github.gaorfgbit.Mural.svg"
if [ -f "$TGT/mural/data/icons/io.github.gaorfg-bit.Mural.png" ]; then
  cp "$TGT/mural/data/icons/io.github.gaorfg-bit.Mural.png" "$ICON_256_DIR/io.github.gaorfgbit.Mural.png"
elif [ -f "$DIR/assets/logo.png" ]; then
  cp "$DIR/assets/logo.png" "$ICON_256_DIR/io.github.gaorfgbit.Mural.png"
fi

echo "Creating launcher..."
install -m 755 "$DIR/scripts/$LAUNCHER" "$BIN_DIR/$LAUNCHER"
ln -sf "$BIN_DIR/$LAUNCHER" "$BIN_DIR/$APP"

echo "Creating desktop entry..."
cat <<EOF > "$DESKTOP_FILE"
[Desktop Entry]
Name=Mural
Comment=Wallpaper Manager
Exec=$BIN_DIR/$LAUNCHER
TryExec=$BIN_DIR/$LAUNCHER
Icon=io.github.gaorfgbit.Mural
Type=Application
Terminal=false
Categories=Utility;Settings;
StartupNotify=true
StartupWMClass=io.github.gaorfgbit.Mural
Keywords=wallpaper;background;
EOF
chmod 644 "$DESKTOP_FILE"
ln -sf "mural.desktop" "$DESKTOP_FILE_LEGACY"

echo "Installing daemon..."
cat <<EOF > "$SYSTEMD_DIR/mural.service"
[Unit]
Description=Mural Wallpaper Daemon
Documentation=https://github.com/gaorfg-bit/mural
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/env python3 %h/.local/share/mural/mural/mural-daemon-service.py
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
update-desktop-database "$APP_DIR" 2>/dev/null || true
gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor/" 2>/dev/null || true

echo "Done. Search for 'Mural' in your activities."
