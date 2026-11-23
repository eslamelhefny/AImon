#!/usr/bin/env bash
# Install aimeter on a systemd Linux VPS. Run from the project folder: sudo bash deploy/install.sh
set -euo pipefail

DEST=/opt/aimeter
SRC="$(cd "$(dirname "$0")/.." && pwd)"

id aimeter >/dev/null 2>&1 || useradd --system --home "$DEST" --shell /usr/sbin/nologin aimeter
mkdir -p "$DEST/data"
cp "$SRC/aimeter.py" "$SRC/dashboard.html" "$SRC/requirements.txt" "$DEST/"
[ -f "$DEST/config.yaml" ] || cp "$SRC/config.example.yaml" "$DEST/config.yaml"
[ -f "$DEST/.env" ] || cp "$SRC/.env.example" "$DEST/.env"

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

chown -R aimeter:aimeter "$DEST"
chmod 600 "$DEST/.env"

cp "$SRC"/deploy/aimeter-*.service "$SRC"/deploy/aimeter-collect.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aimeter-collect.timer aimeter-web.service

echo
echo "Installed to $DEST"
echo "Next: edit $DEST/config.yaml and $DEST/.env, then:"
echo "  sudo -u aimeter $DEST/.venv/bin/python $DEST/aimeter.py test-alert"
echo "  sudo systemctl start aimeter-collect && sudo systemctl restart aimeter-web"
echo "  ssh -L 8787:127.0.0.1:8787 <vps>   then open http://localhost:8787"
