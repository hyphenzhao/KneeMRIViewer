#!/usr/bin/env bash
# Install on an air-gapped Ubuntu 22.04 machine. No apt, no npm, no network.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/mriviewer
STATE=/var/lib/mriviewer

echo "== verifying bundle =="
( cd "$HERE" && sha256sum -c SHA256SUMS --quiet ) || { echo "checksum mismatch"; exit 1; }

echo "== creating user and directories =="
id -u mriviewer >/dev/null 2>&1 || useradd --system --home "$PREFIX" --shell /usr/sbin/nologin mriviewer
install -d "$PREFIX" "$PREFIX/web" "$PREFIX/labelsets" /etc/mriviewer
install -d -o mriviewer -g mriviewer \
  "$STATE" "$STATE/cache" "$STATE/derived" "$STATE/predictions"

echo "== python environment (no apt needed) =="
# python3 -m venv would require the python3.10-venv deb; the zipapp does not.
python3 "$HERE/virtualenv.pyz" --no-download "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --no-index --find-links="$HERE/wheelhouse" \
  -r "$HERE/server/requirements.in"
"$PREFIX/venv/bin/pip" install --no-index --no-deps "$HERE/server"

echo "== application files =="
cp -r "$HERE/web/." "$PREFIX/web/"
cp -r "$HERE/labelsets/." "$PREFIX/labelsets/"
[ -f /etc/mriviewer/config.toml ] || cp "$HERE/deploy/config.example.toml" /etc/mriviewer/config.toml
cp "$HERE/deploy/mriviewer.service" /etc/systemd/system/

echo "== self-check =="
MRIVIEWER_CONFIG=/etc/mriviewer/config.toml "$PREFIX/venv/bin/mrictl" doctor

cat <<MSG

Installed. Next:
  1. edit /etc/mriviewer/config.toml so each dataset root points at the data
  2. sudo systemctl daemon-reload && sudo systemctl enable --now mriviewer
  3. sudo -u mriviewer MRIVIEWER_CONFIG=/etc/mriviewer/config.toml \
       $PREFIX/venv/bin/mrictl scan -d ds0826
  4. open http://<this-host>:8080/ from any machine on the LAN

Mount the data drive read-only and by label, so "never overwrite the source"
is a filesystem guarantee rather than a code convention. In /etc/fstab:
  LABEL=Elements /srv/mridata exfat ro,nofail,uid=mriviewer,iocharset=utf8 0 0
MSG
