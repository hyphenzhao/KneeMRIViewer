#!/usr/bin/env bash
# Push the working tree to the dev server and restart the service.
#
# Use this instead of typing rsync by hand. An ad-hoc
#   rsync -a --delete <src>/ ~/mri-viewer/
# once deleted the server's venv and the built frontend, because neither lives
# in the source tree. The service survived only because the running process
# still held the deleted inodes; the next restart would have failed. Hence
# KEEP: anything that lives on the server but not in git.
set -euo pipefail

STAGE=${STAGE:-/media/haifeng/Elements/mriviewer-src}
DEST=${DEST:-$HOME/mri-viewer}
SERVICE=${SERVICE:-mriviewer}

# Never delete these from the destination - they are built or installed there.
KEEP=(--exclude venv --exclude venv.new --exclude venv.old --exclude ms-playwright
      --exclude node_modules --exclude web/dist
      --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache')

echo "== syncing source =="
rsync -a --delete "${KEEP[@]}" "$STAGE"/ "$DEST"/

# The frontend is built off-server and staged as web/dist; sync it separately so
# a source tree without a build does not wipe the deployed app.
if [ -d "$STAGE/web/dist" ]; then
  echo "== syncing built frontend =="
  rsync -a --delete "$STAGE/web/dist"/ "$DEST/web/dist"/
fi

echo "== reinstalling package =="
# Editable would be lighter, but the service imports from site-packages and a
# stale non-editable install silently shadows the source tree.
"$DEST/venv/bin/pip" -q install --no-deps "$DEST/server"

echo "== restarting =="
systemctl --user restart "$SERVICE"
for _ in $(seq 30); do
  sleep 1
  [ "$(systemctl --user is-active "$SERVICE")" = active ] || continue
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/v1/datasets || true)
  [ "$code" = 200 ] && { echo "up: /api/v1/datasets -> 200"; exit 0; }
done

echo "service did not come up:" >&2
journalctl --user -u "$SERVICE" -n 30 --no-pager >&2
exit 1
