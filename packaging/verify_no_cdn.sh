#!/usr/bin/env bash
# Build-time half of the offline guarantee.
#
# Note what this can and cannot prove. Minified bundles are full of absolute
# URLs that are never fetched - vtk.js and React embed licence headers and doc
# links as inert string literals. Flagging every http:// in the bundle produces
# nothing but false positives, so this script checks the things that actually
# cause a network request:
#
#   1. external src/href/preconnect in the HTML entry points
#   2. any reference to a known CDN host, anywhere
#   3. WASM files (this build is supposed to have none)
#   4. importScripts()/new Worker() pointing at an absolute URL
#
# The runtime half is the Content-Security-Policy header in mriviewer/main.py
# (`default-src 'self'`), which turns any reference this misses into a visible
# console error instead of a silent hang. Verify once by loading the app with
# DevTools open and confirming zero third-party requests.
set -uo pipefail
DIR="${1:-web/dist}"
fail=0

if [ ! -d "$DIR" ]; then
  echo "FAIL: $DIR does not exist - run 'npm run build' first"
  exit 1
fi

# 1. HTML entry points must not reference anything off-origin.
html_hits=$(grep -rEoh '(src|href)="https?://[^"]+"' "$DIR" --include='*.html' 2>/dev/null || true)
if [ -n "$html_hits" ]; then
  echo "FAIL: external resource referenced from HTML:"
  echo "$html_hits" | sed 's/^/  /'
  fail=1
fi
if grep -rqE '<link[^>]+rel="(preconnect|dns-prefetch)"' "$DIR" --include='*.html' 2>/dev/null; then
  echo "FAIL: preconnect/dns-prefetch link in HTML"
  fail=1
fi

# 2. Known CDN / font hosts anywhere in the output.
cdn=$(grep -rEoh 'fonts\.googleapis\.com|fonts\.gstatic\.com|unpkg\.com|cdn\.jsdelivr\.net|cdnjs\.cloudflare\.com|code\.jquery\.com|esm\.sh' "$DIR" 2>/dev/null | sort -u || true)
if [ -n "$cdn" ]; then
  echo "FAIL: CDN host referenced in bundle:"
  echo "$cdn" | sed 's/^/  /'
  fail=1
fi

# 3. No WASM: we do not ship dicom-image-loader or polyseg, so there should be none.
wasm=$(find "$DIR" -name '*.wasm' 2>/dev/null || true)
if [ -n "$wasm" ]; then
  echo "warn: .wasm files present (this build is expected to have none):"
  echo "$wasm" | sed 's/^/  /'
fi

# 4. Absolute URLs passed to a script/worker loader.
loader=$(grep -rEoh '(importScripts\(|new Worker\()["'"'"'`]https?://[^"'"'"'`]+' "$DIR" 2>/dev/null | sort -u || true)
if [ -n "$loader" ]; then
  echo "FAIL: worker/script loaded from an absolute URL:"
  echo "$loader" | sed 's/^/  /'
  fail=1
fi

# Informational: every distinct host mentioned, so a human can eyeball it once.
echo "hosts mentioned in bundle (string literals; none are fetched if the checks above pass):"
grep -rEoh 'https?://[^"'"'"'` )]+' "$DIR" 2>/dev/null \
  | sed -E 's#(https?://[^/]+).*#\1#' | sort -u | sed 's/^/  /'

if [ $fail -eq 0 ]; then
  echo "OK: $DIR loads nothing from an external host"
fi
exit $fail
