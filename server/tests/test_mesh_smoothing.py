"""Display meshes must not show the 3 mm slice staircase.

The anti-aliased level set used for measurement is a narrow-band method: it
cannot move a surface more than about half a voxel from the binary boundary,
so wherever the outline jumps between two 3 mm slices the zero crossing stays
pinned to the slice plane. The display pipeline therefore uses shape-based
interpolation instead. These tests pin three things:

1. a sphere voxelised on 3 mm slices comes back round,
2. the slice-facing surface-area fraction (the terracing metric) at least
   halves against the old construction on a phantom that is not slice-aligned,
3. the measurement pipeline still uses the old, phantom-validated level set -
   this change is display-only by design.
"""
from __future__ import annotations

import numpy as np
import pytest

from mriviewer.seg.mesh import (_extract_surface, _have_vtk, _level_set,
                                _level_set_shape_interp, slice_facing_fraction)

pytestmark = pytest.mark.skipif(not _have_vtk(), reason="VTK not available")

NATIVE_0826 = (3.0, 0.2917, 0.2917)      # (k, j, i)
ISO_MM = 0.5


def _voxelise(fn, spacing, half_extent_mm):
    """Boolean mask of ``fn(x, y, z) <= 0`` on an anisotropic grid, centred."""
    sk, sj, si = spacing
    nk, nj, ni = (int(2 * half_extent_mm / s) + 1 for s in (sk, sj, si))
    ck, cj, ci = (nk - 1) / 2 * sk, (nj - 1) / 2 * sj, (ni - 1) / 2 * si
    K = (np.arange(nk) * sk - ck)[:, None, None]
    J = (np.arange(nj) * sj - cj)[None, :, None]
    I = (np.arange(ni) * si - ci)[None, None, :]
    return np.ascontiguousarray(fn(K, J, I) <= 0), np.array([ck, cj, ci])


def _surface_mm(field, out_spacing, sub_shape_native, spacing):
    """Extract a surface and return vertices in mm relative to the sub-volume origin."""
    verts, faces = _extract_surface(field, out_spacing)
    assert verts is not None and len(verts) > 0
    scale = np.array([out_spacing[d] / spacing[d] for d in range(3)])
    verts_native = verts * scale
    verts_mm = verts_native * np.array(spacing)
    return verts_mm, faces


def test_sphere_on_3mm_slices_is_round() -> None:
    """The headline requirement: no terraces on a shape that has none."""
    R = 15.0
    mask, centre = _voxelise(lambda k, j, i: np.sqrt(k**2 + j**2 + i**2) - R,
                             NATIVE_0826, R + 5.0)
    field, out_sp = _level_set_shape_interp(mask, NATIVE_0826, ISO_MM)
    verts_mm, faces = _surface_mm(field, out_sp, mask.shape, NATIVE_0826)
    radial = np.linalg.norm(verts_mm - centre, axis=1)
    dev = np.abs(radial - R)
    # Measured on the shipped pipeline; regenerate deliberately, never to pass.
    assert np.percentile(dev, 95) < 0.30, "p95 radial deviation %.3f mm" % np.percentile(dev, 95)
    assert dev.max() < 0.75, "max radial deviation %.3f mm" % dev.max()


def test_shape_interp_reduces_slice_facing_area() -> None:
    """A tilted ellipsoid has almost no surface that really faces the slice axis,
    so any slice-facing area is terracing. The new field must at least halve it."""
    a, b, c = 25.0, 12.0, 8.0
    th = np.deg2rad(30.0)

    def f(k, j, i):
        # rotate 30 deg about the i axis so no ellipsoid axis aligns with slices
        kk = np.cos(th) * k - np.sin(th) * j
        jj = np.sin(th) * k + np.cos(th) * j
        return (kk / a) ** 2 + (jj / b) ** 2 + (i / c) ** 2 - 1.0

    mask, _ = _voxelise(f, NATIVE_0826, a + 4.0)
    old_field, sp = _level_set(mask, NATIVE_0826, ISO_MM)
    new_field, _ = _level_set_shape_interp(mask, NATIVE_0826, ISO_MM)
    old_v, old_f = _surface_mm(old_field, sp, mask.shape, NATIVE_0826)
    new_v, new_f = _surface_mm(new_field, sp, mask.shape, NATIVE_0826)
    old = slice_facing_fraction(old_v, old_f, (1.0, 0.0, 0.0))
    new = slice_facing_fraction(new_v, new_f, (1.0, 0.0, 0.0))
    assert old > 0.05, "phantom did not terrace under the old level set (%.3f)" % old
    assert new <= 0.5 * old, "slice-facing area %.1f%% -> %.1f%%" % (100 * old, 100 * new)


def test_measurement_pipeline_still_uses_antialias() -> None:
    """morph/geometry.py must be byte-identical to _level_set output.

    The measurement path is phantom-validated (tests/test_thickness_phantom.py)
    and its numbers are published with a stated bias. Silently swapping its
    level set would change every reported thickness.
    """
    from mriviewer.morph.geometry import iso_fields

    class Geom:
        spacing = (NATIVE_0826[2], NATIVE_0826[1], NATIVE_0826[0])   # (col, row, slice)

    mask, _ = _voxelise(lambda k, j, i: np.sqrt(k**2 + j**2 + i**2) - 10.0,
                        NATIVE_0826, 14.0)
    fields, grid = iso_fields({4: mask}, Geom(), [4], iso_mm=ISO_MM, margin=0)
    idx = np.argwhere(mask)
    lo = idx.min(0)
    hi = idx.max(0) + 1
    sub = np.ascontiguousarray(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]])
    direct, _ = _level_set(sub, NATIVE_0826, ISO_MM)
    assert fields[4].shape == direct.shape
    assert np.array_equal(fields[4], direct)


def test_slice_facing_fraction_on_a_cube() -> None:
    """Two of six faces of an axis-aligned cube face the slice axis: 1/3."""
    p = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                  [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], float)
    quads = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (2, 3, 7, 6), (1, 2, 6, 5), (0, 3, 7, 4)]
    tris = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    assert slice_facing_fraction(p, tris, (1, 0, 0)) == pytest.approx(1 / 3)
