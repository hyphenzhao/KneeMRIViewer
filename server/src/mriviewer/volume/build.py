"""DICOM / NIfTI -> canonical cached volume, plus the browser wire metadata."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ..dicomio.probe import InstanceHeader
from ..dicomio.series import SeriesGeometry, read_volume
from .cache import CacheEntry, VolumeCache

DTYPE_TO_JS = {
    "uint8": "Uint8Array",
    "int8": "Int8Array",
    "uint16": "Uint16Array",
    "int16": "Int16Array",
    "float32": "Float32Array",
}


def window_from_percentiles(vol: np.ndarray) -> tuple[float, float]:
    """A robust default window when the header has none, or has a bad one."""
    sample = vol[:: max(1, vol.shape[0] // 8)].astype(np.float32).ravel()
    nz = sample[sample > 0]
    if nz.size < 100:
        nz = sample
    lo, hi = np.percentile(nz, [1.0, 99.0])
    width = float(max(hi - lo, 1.0))
    return float(lo + width / 2.0), width


def cornerstone_metadata(geom: SeriesGeometry, header: InstanceHeader,
                         wc: float, ww: float) -> dict[str, Any]:
    """The ``metadata`` block Cornerstone3D's createLocalVolume requires."""
    cols, rows, _ = geom.dims
    bits = int(header.bits_allocated or 16)
    return {
        "BitsAllocated": bits,
        "BitsStored": int(header.bits_allocated or bits),
        "HighBit": bits - 1,
        "SamplesPerPixel": int(header.samples_per_pixel or 1),
        "PixelRepresentation": int(header.pixel_representation or 0),
        "PhotometricInterpretation": header.photometric or "MONOCHROME2",
        "Modality": header.modality or "MR",
        "Rows": rows,
        "Columns": cols,
        "PixelSpacing": [geom.spacing[1], geom.spacing[0]],   # [row, column] per DICOM
        "ImageOrientationPatient": list(geom.row_cosine) + list(geom.col_cosine),
        "FrameOfReferenceUID": header.frame_of_reference_uid or "1.2.3.4",
        "SeriesInstanceUID": header.series_uid or "",
        "voiLut": [{"windowCenter": wc, "windowWidth": ww}],
        "VOILUTFunction": "LINEAR",
    }


def wire_meta(volume_id: str, geom: SeriesGeometry, arr: np.ndarray,
              header: InstanceHeader, wc: float, ww: float,
              variant: str = "native") -> dict[str, Any]:
    return {
        "volumeId": volume_id,
        "variant": variant,
        "dimensions": list(geom.dims),
        "spacing": list(geom.spacing),
        "origin": list(geom.origin),
        "direction": list(geom.direction),
        "dataType": DTYPE_TO_JS[str(arr.dtype)],
        "numpyDtype": str(arr.dtype),
        "rescale": {"slope": header.rescale_slope, "intercept": header.rescale_intercept},
        "range": {"min": int(arr.min()), "max": int(arr.max())},
        "plane": geom.plane,
        "anisotropy": round(geom.anisotropy, 3),
        "warning": geom.warning,
        "metadata": cornerstone_metadata(geom, header, wc, ww),
    }


def build_volume(ordered: Sequence[InstanceHeader], geom: SeriesGeometry,
                 cache: VolumeCache, key: str, volume_id: str) -> tuple[CacheEntry, dict[str, Any]]:
    arr = read_volume(ordered)
    ref = ordered[0]
    wc, ww = ref.window_center, ref.window_width
    if not wc or not ww or ww <= 1:
        wc, ww = window_from_percentiles(arr)
    meta = wire_meta(volume_id, geom, arr, ref, float(wc), float(ww))
    entry = cache.volume(key)
    entry.write(arr, meta)
    # write() adds byteLength/checksum/gzipBytes to its own copy, so read the
    # authoritative record back rather than returning the pre-write dict.
    return entry, entry.read_meta()


def downsample(arr: np.ndarray, factors: tuple[int, int, int]) -> np.ndarray:
    """Integer block-mean downsample of a (k, j, i) volume."""
    fk, fj, fi = factors
    k, j, i = arr.shape
    k2, j2, i2 = k // fk * fk, j // fj * fj, i // fi * fi
    a = arr[:k2, :j2, :i2].astype(np.float32)
    a = a.reshape(k2 // fk, fk, j2 // fj, fj, i2 // fi, fi).mean(axis=(1, 3, 5))
    return a.astype(arr.dtype)


def choose_downsample(dims: tuple[int, int, int], itemsize: int,
                      budget_bytes: int) -> tuple[int, int, int]:
    """Pick per-axis integer factors so the delivered volume fits the budget.

    Downsamples the two in-plane axes first: with 10:1 anisotropy the slice
    axis has no resolution to spare.
    """
    cols, rows, slices = dims
    fi = fj = fk = 1
    def nbytes() -> int:
        return (cols // fi) * (rows // fj) * (slices // fk) * itemsize
    for _ in range(6):
        if nbytes() <= budget_bytes:
            break
        if (cols // fi) >= (slices // fk) * 2 or (rows // fj) >= (slices // fk) * 2:
            fi *= 2
            fj *= 2
        else:
            fk *= 2
    return (fk, fj, fi)
