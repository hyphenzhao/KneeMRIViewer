"""Series geometry: slice ordering, affine derivation, volume assembly.

The conventions here are *verified* against the 0826 ground-truth labels, not
assumed. See ``tests/test_orientation_roundtrip.py``.

Canonical array layout (what we send to the browser)
----------------------------------------------------
``vol[k, j, i]`` with

* ``i`` = DICOM column index, advancing along ``IOP[0:3]`` (the row cosine)
* ``j`` = DICOM row index, advancing along ``IOP[3:6]`` (the column cosine)
* ``k`` = slice index, sorted **ascending** by the projection of
  ``ImagePositionPatient`` onto the slice normal ``n = row_cos x col_cos``

Flattened C-order this is exactly Cornerstone3D's ``scalarData`` for
``dimensions = [Columns, Rows, Slices]``, i.e. index ``k*rows*cols + j*cols + i``.

Relationship to the ground-truth NIfTI labels
---------------------------------------------
For the 0826 set the label NIfTI array is indexed ``lab[j, i, k]`` (DICOM row,
DICOM column, ascending slice) in RAS. Therefore::

    canonical = nifti_array.transpose(2, 0, 1)     # -> (k, j, i)
    nifti_array = canonical.transpose(1, 2, 0)     # inverse

with **no axis flips**. This was confirmed by rebuilding the affine from the
DICOM headers and comparing against the NIfTI ``sform``: max abs difference
3e-6 mm across all 24 annotated cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np
import pydicom

from .probe import InstanceHeader

# Tolerated deviation between consecutive slice gaps before we flag the series.
SPACING_TOLERANCE_MM = 1e-2


@dataclass
class SeriesGeometry:
    dims: tuple[int, int, int]            # (cols, rows, slices) - Cornerstone order
    spacing: tuple[float, float, float]   # (col, row, slice) mm
    origin: tuple[float, float, float]    # LPS mm, centre of voxel (0,0,0)
    direction: tuple[float, ...]          # 9 floats, column-major [i_hat, j_hat, k_hat]
    row_cosine: tuple[float, float, float]
    col_cosine: tuple[float, float, float]
    normal: tuple[float, float, float]
    plane: str                            # sagittal | coronal | axial | oblique
    anisotropy: float
    warning: str | None = None

    @property
    def shape_kji(self) -> tuple[int, int, int]:
        c, r, s = self.dims
        return (s, r, c)

    def affine_ras(self) -> np.ndarray:
        """4x4 affine mapping NIfTI array indices (j, i, k) -> RAS mm.

        This is the affine that the 0826 ground-truth labels carry, reproduced
        exactly, so a labelmap we write out drops into Slicer / nnU-Net
        unchanged.
        """
        c_cos = np.array(self.col_cosine, float)
        r_cos = np.array(self.row_cosine, float)
        n = np.array(self.normal, float)
        col_sp, row_sp, sl_sp = self.spacing
        a = np.eye(4)
        a[:3, 0] = c_cos * row_sp       # axis0 of the NIfTI array = DICOM row index
        a[:3, 1] = r_cos * col_sp       # axis1 = DICOM column index
        a[:3, 2] = n * sl_sp            # axis2 = slice index
        a[:3, 3] = np.array(self.origin, float)
        a[0, :] *= -1.0                 # LPS -> RAS
        a[1, :] *= -1.0
        return a


def classify_plane(normal: Sequence[float]) -> str:
    n = np.abs(np.asarray(normal, float))
    dominant = int(np.argmax(n))
    if n[dominant] < 0.75:
        return "oblique"
    return ("sagittal", "coronal", "axial")[dominant]


def sort_headers(headers: Sequence[InstanceHeader], *, complete: bool = True
                 ) -> tuple[list[InstanceHeader], str | None]:
    """Order slices along the acquisition normal, ascending.

    Falls back to InstanceNumber, then to filename, when positions are absent
    or degenerate. Returns the ordered headers plus a warning string.

    ``complete=False`` means this is a tier-1 probe holding only a *sample* of
    the series (first / middle / last). Gaps between sampled slices are then
    ~14 slices wide, so the uniformity check must be skipped - otherwise every
    3 mm series is reported as "non-uniform slice spacing (42-45 mm)".
    """
    imgs = [h for h in headers if h.is_image]
    if not imgs:
        return [], "no image instances"

    warn: list[str] = []
    ref = imgs[0]
    if ref.iop is not None and all(h.ipp is not None for h in imgs):
        iop = np.asarray(ref.iop, float)
        normal = np.cross(iop[:3], iop[3:])
        proj = np.array([float(np.dot(np.asarray(h.ipp, float), normal)) for h in imgs])
        if np.ptp(proj) > 1e-6:
            order = np.argsort(proj, kind="stable")
            ordered = [imgs[i] for i in order]
            gaps = np.diff(np.sort(proj))
            if len(gaps) and complete:
                if np.any(gaps < 1e-6):
                    warn.append("duplicate slice positions")
                if float(np.ptp(gaps)) > SPACING_TOLERANCE_MM:
                    warn.append(
                        "non-uniform slice spacing "
                        "(%.3f-%.3f mm)" % (float(gaps.min()), float(gaps.max()))
                    )
            return ordered, "; ".join(warn) or None
        warn.append("all slices share one position")

    if all(h.instance_number is not None for h in imgs):
        warn.append("ordered by InstanceNumber (no usable ImagePositionPatient)")
        return sorted(imgs, key=lambda h: h.instance_number or 0), "; ".join(warn)

    warn.append("ordered by filename (no position or instance number)")
    return sorted(imgs, key=lambda h: h.path.name), "; ".join(warn)


def geometry_from_headers(ordered: Sequence[InstanceHeader]) -> SeriesGeometry:
    ref = ordered[0]
    n_slices = len(ordered)
    rows = int(ref.rows or 0)
    cols = int(ref.cols or 0)

    if ref.iop is not None:
        iop = np.asarray(ref.iop, float)
        r_cos, c_cos = iop[:3], iop[3:]
    else:  # unknown orientation: assume an axial patient frame
        r_cos, c_cos = np.array([1.0, 0, 0]), np.array([0.0, 1, 0])
    normal = np.cross(r_cos, c_cos)
    nn = float(np.linalg.norm(normal))
    normal = normal / nn if nn > 1e-9 else np.array([0.0, 0, 1])

    row_sp, col_sp = (ref.pixel_spacing or (1.0, 1.0))

    warn: list[str] = []
    slice_sp = None
    if n_slices > 1 and all(h.ipp is not None for h in ordered):
        proj = np.array([float(np.dot(np.asarray(h.ipp, float), normal)) for h in ordered])
        d = np.diff(proj)
        if len(d) and np.all(np.abs(d) > 1e-6):
            slice_sp = float(np.mean(d))
    if slice_sp is None:
        slice_sp = float(ref.spacing_between or ref.slice_thickness or 1.0)
        if n_slices > 1:
            warn.append("slice spacing taken from header, not positions")
    slice_sp = abs(slice_sp) or 1.0

    origin = tuple(float(x) for x in (ref.ipp or (0.0, 0.0, 0.0)))
    spacing = (float(col_sp), float(row_sp), float(slice_sp))
    aniso = max(spacing) / min(spacing) if min(spacing) > 0 else 1.0

    # Cornerstone/VTK direction matrix: columns are the unit vectors of i, j, k.
    direction = tuple(float(x) for x in (*r_cos, *c_cos, *normal))

    return SeriesGeometry(
        dims=(cols, rows, n_slices),
        spacing=spacing,
        origin=origin,  # type: ignore[arg-type]
        direction=direction,
        row_cosine=tuple(float(x) for x in r_cos),   # type: ignore[arg-type]
        col_cosine=tuple(float(x) for x in c_cos),   # type: ignore[arg-type]
        normal=tuple(float(x) for x in normal),      # type: ignore[arg-type]
        plane=classify_plane(normal),
        anisotropy=float(aniso),
        warning="; ".join(warn) or None,
    )


def read_volume(ordered: Sequence[InstanceHeader]) -> np.ndarray:
    """Assemble the canonical ``vol[k, j, i]`` array. Reads PixelData."""
    ref = ordered[0]
    dtype = np.dtype(ref.numpy_dtype)
    rows, cols = int(ref.rows or 0), int(ref.cols or 0)
    out = np.zeros((len(ordered), rows, cols), dtype=dtype)
    for k, h in enumerate(ordered):
        ds = pydicom.dcmread(h.path)
        arr = ds.pixel_array
        if arr.ndim == 3:  # multi-frame in a single file: take the matching frame
            arr = arr[0]
        if arr.shape != (rows, cols):
            raise ValueError(
                "slice %s has shape %s, expected %s" % (h.path.name, arr.shape, (rows, cols))
            )
        out[k] = arr.astype(dtype, copy=False)
    return out


def nifti_to_canonical(arr: np.ndarray) -> np.ndarray:
    """NIfTI array (j, i, k) -> canonical (k, j, i)."""
    return np.ascontiguousarray(arr.transpose(2, 0, 1))


def canonical_to_nifti(arr: np.ndarray) -> np.ndarray:
    """Canonical (k, j, i) -> NIfTI array (j, i, k)."""
    return np.ascontiguousarray(arr.transpose(1, 2, 0))
