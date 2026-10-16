#!/bin/bash
# Folder Mover - Installation Script
# Run as root:  sudo bash install.sh
# Other user:   sudo SERVICE_USER=media bash install.sh

set -euo pipefail

APP_DIR="/opt/folder-mover"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-user}}"   # user that owns the download dir

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root: sudo bash install.sh" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "User '$SERVICE_USER' does not exist. Set SERVICE_USER=<name>." >&2
  exit 1
fi
# Primary group of the user (not necessarily named like the user, e.g. "users")
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
if [ "$(id -u "$SERVICE_USER")" -eq 0 ]; then
  echo "Warning: the service would run as root. Better: sudo SERVICE_USER=<user> bash install.sh" >&2
fi

cd "$(dirname "$0")"
echo "==> Installing Folder Mover to $APP_DIR (service user: $SERVICE_USER)"

# ── Copy files ─────────────────────────────────────────────────────────────────
mkdir -p "$APP_DIR"
cp -r app.py config.py scanner.py mover.py history.py i18n.py gunicorn.conf.py \
      requirements.txt config.yaml.example templates static translations "$APP_DIR/"

# ── Config ────────────────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/config.yaml" ]; then
  if [ -f config.yaml ]; then
    cp config.yaml "$APP_DIR/config.yaml"
  else
    cp config.yaml.example "$APP_DIR/config.yaml"
  fi
  chmod 600 "$APP_DIR/config.yaml"
  echo ""
  echo "  !! Edit $APP_DIR/config.yaml and set your paths + password !!"
  echo ""
fi
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"

# ── Python venv ───────────────────────────────────────────────────────────────
echo "==> Creating Python virtualenv"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ── Systemd ───────────────────────────────────────────────────────────────────
echo "==> Installing systemd service"
sed -e "s/^User=.*/User=$SERVICE_USER/" -e "s/^Group=.*/Group=$SERVICE_GROUP/" \
    folder-mover.service > /etc/systemd/system/folder-mover.service

# Start only once the configured folders (NAS mounts) are available
MOUNTS=$(cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -c '
from config import load_config
c = load_config()
print(" ".join(c[k].replace("%", "%%") for k in ("download_dir", "movies_dir", "series_dir") if " " not in c[k]))
' 2>/dev/null || true)
if [ -n "$MOUNTS" ]; then
  sed -i "s|^Wants=network-online.target$|&\nRequiresMountsFor=$MOUNTS|" /etc/systemd/system/folder-mover.service
fi

systemctl daemon-reload
systemctl enable folder-mover
systemctl restart folder-mover

PORT=$(grep -E '^port:' "$APP_DIR/config.yaml" | awk '{print $2}' || true)
echo ""
echo "==> Done! Folder Mover läuft auf http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
echo "    Logs:            journalctl -u folder-mover -f"
echo "    Passwort-Hash:   $APP_DIR/venv/bin/python $APP_DIR/app.py --hash-password"
echo "    Konfiguration:   nano $APP_DIR/config.yaml"
echo ""
