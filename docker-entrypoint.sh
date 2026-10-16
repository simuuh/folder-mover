#!/bin/sh
# Starts as root only to make /config writable, then drops to PUID:PGID.
# (Docker creates a missing bind-mount folder like ./config as root:root.)
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  # Only /config and its top-level files (config.yaml, history.jsonl, .secret_key)
  find /config -maxdepth 1 -exec chown "$PUID:$PGID" {} +
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

# Started with `user:` (non-root) — we cannot fix permissions, but say clearly why it fails
if [ ! -w /config ]; then
  echo "ERROR: /config is not writable for uid $(id -u). Run: chown -R $(id -u):$(id -g) <your config folder>" >&2
  echo "       or remove 'user:' from compose and set PUID/PGID instead." >&2
fi
exec "$@"
