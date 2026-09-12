#!/usr/bin/env bash
# Produce the air-gapped install bundle. Run this on a machine WITH internet
# (the development server), Ubuntu 22.04, stock python3.10. Output: one tarball to carry
# to the A100/H100 box.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-bundle}"
PYVER=310
PLATFORM=manylinux_2_17_x86_64

rm -rf "$OUT"
mkdir -p "$OUT"/{wheelhouse,web,labelsets,deploy}

echo "== 1/5 python wheels =="
python3 -m pip download -r server/requirements.in -d "$OUT/wheelhouse" \
  --only-binary=:all: --platform "$PLATFORM" \
  --python-version "$PYVER" --implementation cp --abi "cp$PYVER"
python3 -m pip download pip setuptools wheel -d "$OUT/wheelhouse" \
  --only-binary=:all: --platform "$PLATFORM" \
  --python-version "$PYVER" --implementation cp --abi "cp$PYVER"

# Assert the wheels that have no pure-python fallback actually came down as
# cp310 manylinux. VTK in particular stopped publishing cp310 after 9.3.
for pkg in vtk numpy scipy SimpleITK pylibjpeg_libjpeg; do
  if ! ls "$OUT/wheelhouse" | grep -qi "^${pkg}-.*cp${PYVER}.*\.whl$"; then
    if ! ls "$OUT/wheelhouse" | grep -qi "^${pkg}-.*none-any\.whl$"; then
      echo "FAIL: no cp${PYVER} wheel for ${pkg} - pin a version that publishes one" >&2
      exit 1
    fi
  fi
done

echo "== 2/5 virtualenv zipapp =="
# Ubuntu strips ensurepip out of the stdlib into the python3.10-venv deb, which
# an air-gapped box cannot install. The virtualenv zipapp needs neither.
curl -fL -o "$OUT/virtualenv.pyz" https://bootstrap.pypa.io/virtualenv.pyz

echo "== 3/5 frontend =="
# Built here, once. The target machine never sees npm.
( cd web && npm ci && npm run build )
cp -r web/dist/* "$OUT/web/"

echo "== 4/5 offline check =="
bash packaging/verify_no_cdn.sh "$OUT/web"

echo "== 5/5 assemble =="
cp -r server "$OUT/server"
rm -rf "$OUT/server/__pycache__" "$OUT/server/src/mriviewer/__pycache__"
cp -r server/labelsets/* "$OUT/labelsets/"
cp deploy/* "$OUT/deploy/"
cp deploy/install.sh "$OUT/install.sh"
chmod +x "$OUT/install.sh"
( cd "$OUT" && find . -type f -print0 | xargs -0 sha256sum > SHA256SUMS )

TAG=$(git describe --tags --always 2>/dev/null || date +%Y%m%d)
tar czf "mriviewer-${TAG}-offline.tgz" -C "$OUT" .
echo
echo "wrote mriviewer-${TAG}-offline.tgz  ($(du -h "mriviewer-${TAG}-offline.tgz" | cut -f1))"
echo "copy it to the target machine, untar, and run ./install.sh"
