"""Content-addressed volume cache.

Everything the browser downloads is stored **already gzipped** and served with
``Content-Encoding: gzip``, so the server never re-compresses per request and
the browser decompresses natively (no WASM decoder in the offline bundle).

Cache keys hash the *inputs*, not the location, so the whole cache directory
can be rsynced to another machine and stays valid.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

# Bump when the conversion pipeline changes in a way that invalidates cached
# volumes (e.g. a different slice ordering rule).
CONVERTER_VERSION = "1"

GZIP_LEVEL = 5  # level 9 costs ~4x the CPU for ~2% on 16-bit medical data


@dataclass
class CacheEntry:
    key: str
    raw_path: Path      # <...>.raw.gz
    meta_path: Path     # <...>.json

    @property
    def exists(self) -> bool:
        return self.raw_path.exists() and self.meta_path.exists()

    def read_meta(self) -> dict[str, Any]:
        return json.loads(self.meta_path.read_text(encoding="utf-8"))

    def write(self, array: np.ndarray, meta: dict[str, Any]) -> None:
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        buf = np.ascontiguousarray(array).tobytes()
        meta = dict(meta)
        meta["byteLength"] = len(buf)
        meta["checksum"] = "sha256:" + hashlib.sha256(buf).hexdigest()
        tmp = self.raw_path.with_suffix(self.raw_path.suffix + ".tmp")
        with gzip.open(tmp, "wb", compresslevel=GZIP_LEVEL) as fh:
            fh.write(buf)
        os.replace(tmp, self.raw_path)
        meta["gzipBytes"] = self.raw_path.stat().st_size
        tmpm = self.meta_path.with_suffix(".json.tmp")
        tmpm.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmpm, self.meta_path)

    def read_array(self, shape: tuple[int, ...], dtype: str) -> np.ndarray:
        with gzip.open(self.raw_path, "rb") as fh:
            buf = fh.read()
        return np.frombuffer(buf, dtype=np.dtype(dtype)).reshape(shape)


class VolumeCache:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _entry(self, kind: str, key: str, variant: str) -> CacheEntry:
        sub = self.root / kind / key[:2]
        stem = key if variant == "native" else key + "@" + variant
        return CacheEntry(key, sub / (stem + ".raw.gz"), sub / (stem + ".json"))

    def volume(self, key: str, variant: str = "native") -> CacheEntry:
        return self._entry("vol", key, variant)

    def segmentation(self, key: str, variant: str = "native") -> CacheEntry:
        return self._entry("seg", key, variant)

    def mesh_path(self, seg_key: str, label_value: int) -> Path:
        return self.root / "mesh" / seg_key[:2] / seg_key / (str(label_value) + ".mesh.gz")

    def thumb_path(self, key: str) -> Path:
        return self.root / "thumb" / key[:2] / (key + ".jpg")


def hash_inputs(*parts: Any) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    h.update(CONVERTER_VERSION.encode())
    return h.hexdigest()


def hash_files(paths: Iterable[Path]) -> str:
    """Hash a file list by (name, size, mtime) - cheap and good enough to
    detect a changed or re-exported series without reading the pixels."""
    h = hashlib.sha1()
    for p in paths:
        try:
            st = p.stat()
            h.update(("%s|%d|%.3f" % (p.name, st.st_size, st.st_mtime)).encode("utf-8"))
        except OSError:
            h.update(p.name.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
