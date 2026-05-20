#!/bin/bash
# Folder Mover - Installation Script
# Run as root: sudo bash install.sh

set -e

APP_DIR="/opt/folder-mover"
SERVICE_USER="user"   # adjust if your download user is different

echo "==> Installing Folder Mover to $APP_DIR"

# ── Copy files ─────────────────────────────────────────────────────────────────
mkdir -p "$APP_DIR"
cp -r app.py config.py scanner.py mover.py requirements.txt templates "$APP_DIR/"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ── Config ────────────────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/config.yaml" ]; then
  cp config.yaml "$APP_DIR/config.yaml"
  echo ""
  echo "  !! Edit $APP_DIR/config.yaml and set your paths + password !!"
  echo ""
fi

# ── Python venv ───────────────────────────────────────────────────────────────
echo "==> Creating Python virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ── Systemd ───────────────────────────────────────────────────────────────────
echo "==> Installing systemd service"
# Adjust User= in service file to match SERVICE_USER
sed "s/^User=.*/User=$SERVICE_USER/" folder-mover.service > /etc/systemd/system/folder-mover.service
sed -i "s/^Group=.*/Group=$SERVICE_USER/" /etc/systemd/system/folder-mover.service

systemctl daemon-reload
systemctl enable folder-mover
systemctl restart folder-mover

echo ""
echo "==> Done! Folder Mover läuft auf http://$(hostname -I | awk '{print $1}'):8080"
echo "    Logs: journalctl -u folder-mover -f"
echo "    Passwort ändern: nano $APP_DIR/config.yaml"
echo ""
