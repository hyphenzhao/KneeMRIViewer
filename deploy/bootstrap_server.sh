#!/usr/bin/env bash
# One-shot bring-up on the development server (Ubuntu 22.04, has internet).
#
# Run it straight off the drive:
#     bash /media/haifeng/Elements/mri-viewer/deploy/bootstrap_server.sh
#
# Idempotent and resumable: re-running skips whatever is already done. The
# drive is only ever read from; code goes to ~/mri-viewer and every byte the
# app writes goes to ~/mri-viewer-cache.
set -euo pipefail

DRIVE="${MRIV_DRIVE:-/media/haifeng/Elements}"
SRC="$DRIVE/mri-viewer"
APP="${MRIV_APP:-$HOME/mri-viewer}"
STATE="${MRIV_STATE:-$HOME/mri-viewer-cache}"
CONFIG="$HOME/.config/mriviewer/config.toml"
PORT="${MRIV_PORT:-8080}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$DRIVE" ] || die "drive not mounted at $DRIVE (set MRIV_DRIVE=)"
[ -d "$SRC" ]   || die "no mri-viewer tree at $SRC"

say "1/8  copying code to $APP"
mkdir -p "$APP"
# Never run the app off exFAT; copy to the local disk.
cp -r "$SRC/server" "$SRC/deploy" "$SRC/packaging" "$SRC/README.md" "$APP/" 2>/dev/null || true
mkdir -p "$APP/web"
cp -r "$SRC/web-dist/." "$APP/web/"
find "$APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

say "2/8  python environment"
command -v python3 >/dev/null || die "python3 not found"
PY=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "    python $PY"
# A venv is only usable if it has pip. `python3 -m venv` on stock Ubuntu leaves
# behind a directory with just the python symlinks when ensurepip is missing,
# so testing for bin/python would wrongly skip the repair on a re-run.
if [ ! -x "$APP/venv/bin/pip" ]; then
  rm -rf "$APP/venv"
  # Ubuntu moves ensurepip out of the stdlib into the python3.10-venv deb, so
  # `python3 -m venv` fails on a stock install and needs sudo to fix. The
  # virtualenv zipapp is self-contained and needs neither - and it is the same
  # mechanism the air-gapped installer uses, so this path gets exercised here.
  if ! python3 -m venv "$APP/venv" 2>/dev/null || [ ! -x "$APP/venv/bin/pip" ]; then
    echo "    ensurepip unavailable; using the virtualenv zipapp instead"
    rm -rf "$APP/venv"
    PYZ="$APP/virtualenv.pyz"
    # A zipapp is a shebang plus a zip archive. A truncated download still
    # looks like a file, and python then tries to run it as source and dies
    # with a baffling encoding error - so check the archive actually opens.
    pyz_ok() { [ -f "$1" ] && python3 -c "import zipfile,sys; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)" "$1" 2>/dev/null; }
    if ! pyz_ok "$PYZ"; then
      rm -f "$PYZ"
      if [ -f "$SRC/deploy/virtualenv.pyz" ]; then
        cp "$SRC/deploy/virtualenv.pyz" "$PYZ"
        echo "    using the copy shipped with the bundle"
      fi
    fi
    if ! pyz_ok "$PYZ"; then
      rm -f "$PYZ"
      echo "    downloading virtualenv.pyz"
      curl -fsSL --connect-timeout 20 --max-time 300 --retry 2 \
        -o "$PYZ" https://bootstrap.pypa.io/virtualenv.pyz || true
    fi
    pyz_ok "$PYZ" || die "no usable virtualenv.pyz (bundle copy missing and download failed/truncated)"
    python3 "$PYZ" "$APP/venv"
  fi
fi
[ -x "$APP/venv/bin/pip" ] || die "venv has no pip"
"$APP/venv/bin/pip" install --quiet --upgrade pip wheel

say "3/8  dependencies"
"$APP/venv/bin/pip" install --quiet -r "$APP/server/requirements.in"
"$APP/venv/bin/pip" install --quiet --no-deps -e "$APP/server"

say "4/8  unpacking 0826.zip"
if [ -d "$DRIVE/0826/dicom" ]; then
  echo "    already extracted ($(ls "$DRIVE/0826/dicom" | wc -l) cases)"
elif [ -f "$DRIVE/0826.zip" ]; then
  # Local unzip on the server is far faster than pushing 1.4 GB over SMB.
  command -v unzip >/dev/null || die "unzip not installed (apt install unzip)"
  unzip -q -n "$DRIVE/0826.zip" -d "$DRIVE/"
  echo "    extracted $(ls "$DRIVE/0826/dicom" | wc -l) cases"
else
  echo "    WARNING: no 0826.zip and no 0826/ - the flagship dataset will be missing"
fi

say "5/8  config"
mkdir -p "$(dirname "$CONFIG")" "$STATE"
if [ -f "$CONFIG" ]; then
  echo "    keeping existing $CONFIG"
else
  cat > "$CONFIG" <<TOML
bind = "0.0.0.0:$PORT"
state_dir = "$STATE"
labelsets_dir = "$APP/server/labelsets"
web_dir = "$APP/web"
scan_workers = 12
browser_volume_budget_mb = 150
predictions_roots = ["$STATE/predictions"]

[[datasets]]
key = "ds0826"
name = "0826 膝关节软骨标注集"
adapter = "ds0826"
root = "$DRIVE/0826"
label_set = "knee_cartilage_0826_v1"

[[datasets]]
key = "changzheng"
name = "长征医院膝关节 MR"
adapter = "changzheng"
root = "$DRIVE/Knee_MR_Anonymized_CHANGZHENG"
reports_csv = "reports_deidentified.csv"

[[datasets]]
key = "fspdw"
name = "fsPDW"
adapter = "fspdw"
root = "$DRIVE/fsPDW"

[[datasets]]
key = "copd"
name = "COPD 胸部 CT"
adapter = "copd_nifti"
root = "$DRIVE/COPD_prognosis/IMAGE"
label_set = "lung_lobes_v1"
delivery_profile = "downsample"

[[datasets]]
key = "bonescan"
name = "核医学骨扫描"
adapter = "bonescan"
root = "$DRIVE/Bone Scan"
viewer = "frames_2d"
# root/<collection>/<batch>/<patient>: patients live 3 levels down.
patient_depth = 3
TOML
  echo "    wrote $CONFIG"
fi
export MRIVIEWER_CONFIG="$CONFIG"

say "6/8  dependency self-check"
"$APP/venv/bin/mrictl" doctor

say "7/8  index + cache the annotated set"
"$APP/venv/bin/mrictl" init
"$APP/venv/bin/mrictl" scan -d ds0826
"$APP/venv/bin/mrictl" materialize -d ds0826
"$APP/venv/bin/mrictl" mesh
"$APP/venv/bin/mrictl" stats

say "8/8  starting the server"
cat > "$APP/run.sh" <<RUN
#!/usr/bin/env bash
export MRIVIEWER_CONFIG="$CONFIG"
exec "$APP/venv/bin/uvicorn" mriviewer.main:app --host 0.0.0.0 --port $PORT --workers 4
RUN
chmod +x "$APP/run.sh"

IP=$(hostname -I | awk '{print $1}')
cat <<MSG

Ready. Start it with:
    $APP/run.sh

Then from any machine on the LAN:
    http://$IP:$PORT/

The other four datasets are indexed on demand - they are big, so run them
when you have the time (the first is ~170k files over USB):
    export MRIVIEWER_CONFIG=$CONFIG
    $APP/venv/bin/mrictl scan -d changzheng --limit 50   # timed pilot first
    $APP/venv/bin/mrictl scan -d changzheng
    $APP/venv/bin/mrictl scan -d fspdw
    $APP/venv/bin/mrictl scan -d copd
    $APP/venv/bin/mrictl scan -d bonescan

Volumes are built lazily the first time a series is opened, so you do not have
to pre-materialize anything beyond ds0826.
MSG
