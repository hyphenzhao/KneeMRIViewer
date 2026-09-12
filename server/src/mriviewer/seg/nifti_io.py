"""NIfTI segmentation <-> canonical labelmap.

This is the load-bearing correctness module of the whole project: get it wrong
and the cartilage overlay is mirrored, which looks plausible enough to survive
casual inspection.

The transform is *derived per file* by comparing the NIfTI affine's world
direction vectors against the series geometry, then **stored** on the
segmentation row. Saving a correction applies the exact stored inverse and
reuses the original file's affine and header verbatim, so the output is
byte-compatible with what 3D Slicer / nnU-Net expect.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np

from ..dicomio.series import SeriesGeometry

# How far the reconstructed corner may sit from the series origin before we
# refuse the association (mm). One voxel of slack on the coarse axis.
ORIGIN_TOLERANCE_MM = 4.0
DIRECTION_TOLERANCE = 0.08


class SegmentationMismatch(Exception):
    """The NIfTI does not describe the same physical volume as the series."""


@dataclass
class IngestTransform:
    """How to get from the on-disk NIfTI array to the canonical ``(k, j, i)``.

    ``axis_for[o]`` is the NIfTI array axis that becomes canonical axis ``o``
    (0=k slices, 1=j rows, 2=i columns); ``flip[o]`` says whether it must be
    reversed.
    """
    axis_for: tuple[int, int, int]
    flip: tuple[bool, bool, bool]
    nifti_shape: tuple[int, int, int]
    nifti_dtype: str
    nifti_affine: list[list[float]]
    origin_residual_mm: float
    direction_residual: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(s: str) -> "IngestTransform":
        d = json.loads(s)
        d["axis_for"] = tuple(d["axis_for"])
        d["flip"] = tuple(bool(x) for x in d["flip"])
        d["nifti_shape"] = tuple(d["nifti_shape"])
        return IngestTransform(**d)

    def apply(self, arr: np.ndarray) -> np.ndarray:
        """NIfTI array -> canonical (k, j, i)."""
        out = np.transpose(arr, self.axis_for)
        slicer = tuple(slice(None, None, -1) if f else slice(None) for f in self.flip)
        return np.ascontiguousarray(out[slicer])

    def invert(self, canonical: np.ndarray) -> np.ndarray:
        """Canonical (k, j, i) -> NIfTI array order."""
        slicer = tuple(slice(None, None, -1) if f else slice(None) for f in self.flip)
        out = canonical[slicer]
        inverse = np.argsort(np.asarray(self.axis_for))
        return np.ascontiguousarray(np.transpose(out, tuple(int(x) for x in inverse)))


def _ras_to_lps(v: np.ndarray) -> np.ndarray:
    out = np.array(v, float)
    out[..., 0] *= -1.0
    out[..., 1] *= -1.0
    return out


def derive_transform(affine: np.ndarray, shape: tuple[int, int, int],
                     dtype: str, geom: SeriesGeometry) -> IngestTransform:
    """Match NIfTI array axes to canonical (k, j, i) using world directions."""
    affine = np.asarray(affine, float)
    # World direction (LPS) of each NIfTI array axis, unit length.
    nifti_dirs = []
    for a in range(3):
        v = affine[:3, a]
        n = np.linalg.norm(v)
        nifti_dirs.append(_ras_to_lps(v / n) if n > 1e-9 else np.zeros(3))
    nifti_dirs_arr = np.stack(nifti_dirs)

    # Canonical axis world directions, in output order (k, j, i).
    canon_dirs = np.stack([
        np.asarray(geom.normal, float),
        np.asarray(geom.col_cosine, float),
        np.asarray(geom.row_cosine, float),
    ])
    canon_len = (geom.dims[2], geom.dims[1], geom.dims[0])

    dots = canon_dirs @ nifti_dirs_arr.T          # (3 canonical, 3 nifti)
    axis_for: list[int] = []
    flip: list[bool] = []
    used: set[int] = set()
    worst = 0.0
    for o in range(3):
        order = np.argsort(-np.abs(dots[o]))
        pick = next((int(a) for a in order if int(a) not in used), None)
        if pick is None:
            raise SegmentationMismatch("cannot match NIfTI axes to series axes")
        used.add(pick)
        axis_for.append(pick)
        flip.append(bool(dots[o, pick] < 0))
        worst = max(worst, 1.0 - abs(float(dots[o, pick])))

    if worst > DIRECTION_TOLERANCE:
        raise SegmentationMismatch(
            "NIfTI axes are not aligned with the series (residual %.3f)" % worst)

    permuted = tuple(shape[a] for a in axis_for)
    if permuted != canon_len:
        raise SegmentationMismatch(
            "shape mismatch: NIfTI %s permutes to %s, series is %s"
            % (shape, permuted, canon_len))

    # Where does canonical voxel (0,0,0) actually sit? Compare with the series
    # origin - this is what catches a missed flip.
    idx = np.zeros(3)
    for o, (a, f) in enumerate(zip(axis_for, flip)):
        idx[a] = (shape[a] - 1) if f else 0
    corner_ras = affine[:3, :3] @ idx + affine[:3, 3]
    corner_lps = _ras_to_lps(corner_ras)
    residual = float(np.linalg.norm(corner_lps - np.asarray(geom.origin, float)))
    if residual > ORIGIN_TOLERANCE_MM:
        raise SegmentationMismatch(
            "origin mismatch: labelmap corner is %.1f mm from the series origin" % residual)

    return IngestTransform(
        axis_for=(axis_for[0], axis_for[1], axis_for[2]),
        flip=(flip[0], flip[1], flip[2]),
        nifti_shape=tuple(int(x) for x in shape),        # type: ignore[arg-type]
        nifti_dtype=dtype,
        nifti_affine=[[float(x) for x in row] for row in affine],
        origin_residual_mm=round(residual, 4),
        direction_residual=round(worst, 6),
    )


def load_segmentation(path: Path, geom: SeriesGeometry) -> tuple[np.ndarray, IngestTransform]:
    """Load a NIfTI label file and return it in canonical (k, j, i) uint8."""
    img = nib.load(str(path))
    arr = np.asanyarray(img.dataobj)
    if arr.ndim == 4 and arr.shape[3] == 1:
        arr = arr[..., 0]
    if arr.ndim != 3:
        raise SegmentationMismatch("expected a 3-D label volume, got shape %s" % (arr.shape,))
    tr = derive_transform(img.affine, arr.shape, str(arr.dtype), geom)
    canonical = tr.apply(arr)
    vmax = int(canonical.max()) if canonical.size else 0
    if vmax > 255:
        raise SegmentationMismatch("label values exceed 255 (max %d)" % vmax)
    if canonical.min() < 0:
        raise SegmentationMismatch("negative label values")
    return canonical.astype(np.uint8, copy=False), tr


def merge_binary_masks(paths: dict[int, Path], geom: SeriesGeometry
                       ) -> tuple[np.ndarray, IngestTransform, dict[int, int]]:
    """Merge per-structure binary masks into one multi-label canonical volume.

    Cornerstone's labelmap is a single scalar volume, so thirty separate binary
    files have to become thirty *values*, not thirty segmentations. Smaller
    structures win on overlap so a big one cannot swallow them.
    """
    canonical: np.ndarray | None = None
    tr: IngestTransform | None = None
    counts: dict[int, int] = {}
    sized: list[tuple[int, int, np.ndarray]] = []
    for value, p in sorted(paths.items()):
        img = nib.load(str(p))
        arr = np.asanyarray(img.dataobj)
        if arr.ndim == 4 and arr.shape[3] == 1:
            arr = arr[..., 0]
        if tr is None:
            tr = derive_transform(img.affine, arr.shape, str(arr.dtype), geom)
        mask = tr.apply(arr) > 0
        sized.append((int(mask.sum()), value, mask))
    if tr is None:
        raise SegmentationMismatch("no masks supplied")
    sized.sort(reverse=True)          # write big first, small last -> small wins
    canonical = np.zeros(geom.shape_kji, dtype=np.uint8)
    for n, value, mask in sized:
        canonical[mask] = value
        counts[value] = n
    return canonical, tr, counts


def save_correction(canonical: np.ndarray, tr: IngestTransform, base_path: Path,
                    out_path: Path) -> dict[str, Any]:
    """Write a corrected labelmap back out, reusing the original affine/header.

    Reconstructing the affine from our own origin/direction is exactly where
    sign errors hide, so we do not: we load the source file and reuse its
    affine and header objects untouched.
    """
    base = nib.load(str(base_path))
    arr = tr.invert(canonical)
    if arr.shape != tuple(tr.nifti_shape):
        raise SegmentationMismatch(
            "inverse transform gave shape %s, source was %s" % (arr.shape, tr.nifti_shape))
    hdr = base.header.copy()
    hdr.set_data_dtype(np.uint8)
    out = nib.Nifti1Image(arr.astype(np.uint8), base.affine, hdr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(out, str(out_path))
    return {
        "path": str(out_path),
        "shape": list(arr.shape),
        "affine": [[float(x) for x in row] for row in np.asarray(base.affine)],
        "bytes": out_path.stat().st_size,
    }
