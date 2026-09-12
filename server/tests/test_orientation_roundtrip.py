"""The single highest-value test in the project.

Every orientation bug - a mirrored cartilage overlay, a reversed slice order, a
corrected mask that no longer lines up with the image it was drawn on - is
caught here.

Two halves:

* **Synthetic**: an asymmetric phantom pushed through every one of the 48
  axis-permutation/flip combinations a NIfTI affine can express, asserting
  byte-exact recovery.
* **Real**: all 24 annotated 0826 cases, asserting the derived transform equals
  the hand-verified ``transpose(2, 0, 1)`` and that a save/reload round-trips.

Run directly (``python tests/test_orientation_roundtrip.py``) or under pytest.
"""
from __future__ import annotations

import glob
import itertools
import os
import re
import sys
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mriviewer.dicomio.probe import iter_dicom_files, probe            # noqa: E402
from mriviewer.dicomio.series import (                                  # noqa: E402
    geometry_from_headers, nifti_to_canonical, sort_headers,
)
from mriviewer.seg.nifti_io import (                                    # noqa: E402
    derive_transform, load_segmentation, save_correction,
)

DEVDATA = os.environ.get(
    "MRIVIEWER_TESTDATA",
    str(Path(__file__).resolve().parents[3] / "devdata" / "0826"),
)


def _phantom(shape=(7, 9, 11)) -> np.ndarray:
    """Every axis and every direction distinguishable, no symmetry anywhere."""
    k, j, i = shape
    a = np.zeros(shape, dtype=np.uint8)
    a[0:3, 0:1, 0:1] = 1        # a 3-long spur along k from the origin corner
    a[0:1, 0:4, 0:1] = 2        # a 4-long spur along j
    a[0:1, 0:1, 0:5] = 3        # a 5-long spur along i
    a[k - 1, j - 1, i - 1] = 4  # unique marker at the far corner
    a[k // 2, j // 2, i // 2] = 5
    return a


class _Geom:
    """Minimal stand-in for SeriesGeometry (only the fields derive_transform uses)."""

    def __init__(self, dims, spacing, origin, row_cosine, col_cosine, normal):
        self.dims = dims
        self.spacing = spacing
        self.origin = origin
        self.row_cosine = row_cosine
        self.col_cosine = col_cosine
        self.normal = normal

    @property
    def shape_kji(self):
        c, r, s = self.dims
        return (s, r, c)


def test_synthetic_all_permutations() -> None:
    canonical = _phantom()
    ks, js, isz = canonical.shape
    spacing = (0.3, 0.4, 3.0)                       # (col, row, slice) mm
    origin = (12.5, -30.25, 7.75)
    row_cos, col_cos, normal = (1.0, 0, 0), (0.0, 1, 0), (0.0, 0, 1)
    geom = _Geom((isz, js, ks), spacing, origin, row_cos, col_cos, normal)

    canon_dirs_lps = {0: np.array(normal), 1: np.array(col_cos), 2: np.array(row_cos)}
    canon_step = {0: spacing[2], 1: spacing[1], 2: spacing[0]}

    tested = 0
    for perm in itertools.permutations(range(3)):
        for flips in itertools.product([False, True], repeat=3):
            # Build the NIfTI array and the affine that describes it.
            inv = np.argsort(np.asarray(perm))
            slicer = tuple(slice(None, None, -1) if flips[o] else slice(None)
                           for o in range(3))
            # undo the flips in canonical space, then permute: the exact
            # inverse of IngestTransform.apply()
            arr_nifti = np.transpose(canonical[slicer], tuple(int(x) for x in inv))

            affine = np.eye(4)
            for o in range(3):
                a = perm[o]
                d = canon_dirs_lps[o] * canon_step[o] * (-1.0 if flips[o] else 1.0)
                affine[:3, a] = np.array([-d[0], -d[1], d[2]])   # LPS -> RAS
            idx0 = np.zeros(3)
            for o in range(3):
                if flips[o]:
                    idx0[perm[o]] = arr_nifti.shape[perm[o]] - 1
            o_lps = np.asarray(origin, float)
            o_ras = np.array([-o_lps[0], -o_lps[1], o_lps[2]])
            affine[:3, 3] = o_ras - affine[:3, :3] @ idx0

            tr = derive_transform(affine, arr_nifti.shape, "uint8", geom)
            assert tr.axis_for == perm, (perm, flips, tr.axis_for)
            assert tr.flip == flips, (perm, flips, tr.flip)
            back = tr.apply(arr_nifti)
            assert np.array_equal(back, canonical), (perm, flips)
            assert np.array_equal(tr.invert(back), arr_nifti), (perm, flips)
            tested += 1
    assert tested == 48
    print("  synthetic: %d permutation/flip combinations round-trip exactly" % tested)


def _real_cases() -> list[str]:
    return sorted(
        {re.match(r"(\d+)_", os.path.basename(p)).group(1)
         for p in glob.glob(os.path.join(DEVDATA, "segmentation label", "*.nii"))},
        key=int,
    )


def test_real_0826_cases() -> None:
    cases = _real_cases()
    if not cases:
        print("  real: no dev data at %s - skipped" % DEVDATA)
        return
    for case in cases:
        sd = glob.glob(os.path.join(DEVDATA, "dicom", case, "*"))[0]
        hs = [h for h in (probe(f) for f in iter_dicom_files(sd)) if h]
        ordered, _ = sort_headers(hs)
        geom = geometry_from_headers(ordered)
        lab_path = Path(DEVDATA) / "segmentation label" / (case + "_all_labels-label-label.nii")

        canonical, tr = load_segmentation(lab_path, geom)

        # 1. matches the hand-verified transpose, with no flips
        raw = np.asanyarray(nib.load(str(lab_path)).dataobj)
        assert tr.axis_for == (2, 0, 1), (case, tr.axis_for)
        assert tr.flip == (False, False, False), (case, tr.flip)
        assert np.array_equal(canonical, nifti_to_canonical(raw).astype(np.uint8)), case

        # 2. geometry agrees to sub-micron
        assert tr.origin_residual_mm < 1e-3, (case, tr.origin_residual_mm)
        assert canonical.shape == geom.shape_kji, case

        # 3. edit -> save -> reload survives byte-exactly, affine untouched
        edited = canonical.copy()
        edited[edited == 4] = 0                       # erase medial femoral cartilage
        edited[2, 100:110, 100:110] = 7               # paint a stroke
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "corrected.nii.gz"
            info = save_correction(edited, tr, lab_path, out)
            reloaded, tr2 = load_segmentation(out, geom)
            assert np.array_equal(reloaded, edited), case
            assert np.allclose(np.asarray(info["affine"]),
                               np.asarray(nib.load(str(lab_path)).affine)), case
    print("  real: %d annotated 0826 cases round-trip exactly" % len(cases))


def test_rejects_mismatched_volume() -> None:
    """A labelmap from a different series must be refused, not silently shown."""
    cases = _real_cases()
    if len(cases) < 2:
        return
    a, b = cases[0], cases[-1]
    sd = glob.glob(os.path.join(DEVDATA, "dicom", a, "*"))[0]
    hs = [h for h in (probe(f) for f in iter_dicom_files(sd)) if h]
    ordered, _ = sort_headers(hs)
    geom = geometry_from_headers(ordered)
    wrong = Path(DEVDATA) / "segmentation label" / (b + "_all_labels-label-label.nii")
    from mriviewer.seg.nifti_io import SegmentationMismatch
    try:
        load_segmentation(wrong, geom)
    except SegmentationMismatch as exc:
        print("  mismatch guard: case %s label refused for case %s (%s)"
              % (b, a, str(exc)[:60]))
        return
    raise AssertionError("a labelmap from case %s was accepted for case %s" % (b, a))


if __name__ == "__main__":
    print("orientation round-trip")
    test_synthetic_all_permutations()
    test_real_0826_cases()
    test_rejects_mismatched_volume()
    print("ALL PASS")
