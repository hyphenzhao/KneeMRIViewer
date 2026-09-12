"""Phantoms with known thickness: is the measurement right, and what does the
acquisition cost us?

The second question is the point. Run against isotropic data the pipeline is
unbiased to +/-0.003 mm, so the algorithm is not the problem. Run against the
0826 grid (3.0 x 0.2917 x 0.2917 mm) the same phantom comes back 7-18% thin,
*even over the surface we call reliable*. That is the honest cost of measuring
cartilage on 3 mm sagittal slices, and it is why absolute thickness here must
not be compared against literature reference values as though calibrated.

Crucially the anisotropy error is **not monotone** in true thickness (it is
-0.14 mm at 1.0 mm, -0.35 mm at 2.0 mm, +0.23 mm at 3.0 mm), so it cannot be
divided out by a correction factor. Anyone tempted to "calibrate it away"
should read that row of numbers first.
"""
from __future__ import annotations

import numpy as np
import pytest

from mriviewer.morph.geometry import dense_surface, vertex_areas
from mriviewer.morph.thickness import thickness_map
from mriviewer.seg.mesh import _have_vtk, _level_set

pytestmark = pytest.mark.skipif(not _have_vtk(), reason="VTK not available")

ISO_MM = 0.5
R_IN = 20.0                      # inner radius, roughly a femoral condyle
NATIVE_0826 = (3.0, 0.2917, 0.2917)      # (k, j, i) = (slice, row, column)
ISOTROPIC = (0.2917, 0.2917, 0.2917)


def _shell(thickness_mm: float, spacing: tuple[float, float, float]):
    """A spherical shell of exactly known thickness, voxelized on `spacing`."""
    sk, sj, si = spacing
    half = R_IN + thickness_mm + 4.0
    nk, nj, ni = (int(2 * half / s) + 1 for s in (sk, sj, si))
    ck, cj, ci = (nk - 1) / 2 * sk, (nj - 1) / 2 * sj, (ni - 1) / 2 * si
    r = np.sqrt(((np.arange(nk) * sk - ck) ** 2)[:, None, None]
                + ((np.arange(nj) * sj - cj) ** 2)[None, :, None]
                + ((np.arange(ni) * si - ci) ** 2)[None, None, :])
    mask = (r >= R_IN) & (r <= R_IN + thickness_mm)
    return np.ascontiguousarray(mask), np.array([ck, cj, ci])


def _measure(thickness_mm: float, spacing: tuple[float, float, float],
             reliable_only: bool = False) -> float:
    """Area-weighted mean measured thickness over the inner (interface) surface."""
    mask, centre = _shell(thickness_mm, spacing)
    phi, _ = _level_set(mask, spacing, ISO_MM)
    verts, faces, normals = dense_surface(phi)

    radius_mm = np.linalg.norm(verts - centre / ISO_MM, axis=1) * ISO_MM
    interface = radius_mm < R_IN + thickness_mm / 2

    measured = thickness_map(phi, verts, normals, interface, ISO_MM).thickness_mm
    weights = vertex_areas(verts, faces)[interface] * ISO_MM ** 2

    keep = np.isfinite(measured)
    if reliable_only:
        n = normals[interface]
        norm = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.divide(n, norm, out=np.zeros_like(n), where=norm > 1e-9)
        res_eff = np.sqrt(sum((n[:, d] * spacing[d]) ** 2 for d in range(3)))
        keep &= res_eff <= 0.6
    return float((measured[keep] * weights[keep]).sum() / weights[keep].sum())


@pytest.mark.parametrize("truth", [1.0, 1.5, 2.0, 3.0, 4.0])
def test_slab_thickness_is_exact(truth: float) -> None:
    """Flat slab, isotropic grid: the ray cast must be exact, not merely close."""
    n_k = int((20.0 + truth) / ISO_MM) + 20
    k = np.arange(n_k)[:, None, None] * ISO_MM
    phi = np.broadcast_to(np.minimum(k - 10.0, 10.0 + truth - k),
                          (n_k, 40, 40)).copy().astype(np.float32)
    verts, faces, normals = dense_surface(phi)
    interface = verts[:, 0] < (10.0 + truth / 2) / ISO_MM

    res = thickness_map(phi, verts, normals, interface, ISO_MM)
    assert res.escaped.mean() == 0.0
    assert np.nanmedian(res.thickness_mm) == pytest.approx(truth, abs=0.01)


@pytest.mark.parametrize("truth", [1.0, 2.0, 3.0])
def test_nested_shell_does_not_depend_on_vtk_normal_orientation(truth: float) -> None:
    """A nested shell is where trusting AutoOrientNormals silently returns NaN.

    vtkPolyDataNormals orients both the inner and outer sheet the same way, so
    a ray cast along a fixed sign of the normal never enters the material. The
    rays must be oriented from the level set locally instead.
    """
    measured = _measure(truth, ISOTROPIC)
    assert np.isfinite(measured), "rays escaped: normal orientation is being trusted"
    assert measured == pytest.approx(truth, abs=0.02)


@pytest.mark.parametrize("truth", [1.0, 1.5, 2.0, 2.5, 3.0])
def test_pipeline_is_unbiased_on_isotropic_data(truth: float) -> None:
    """Level set -> resample -> surface -> ray cast adds no bias of its own."""
    assert _measure(truth, ISOTROPIC) == pytest.approx(truth, abs=0.01)


# Measured on the 0826 acquisition grid: (true thickness, measured over the
# res_eff <= 0.6 mm surface). Regenerate deliberately, never to make a test pass.
ANISOTROPY_COST_MM = {1.0: -0.140, 1.5: -0.145, 2.0: -0.353, 2.5: -0.339, 3.0: +0.228}


@pytest.mark.parametrize("truth,expected_bias", sorted(ANISOTROPY_COST_MM.items()))
def test_anisotropy_cost_is_what_we_publish(truth: float, expected_bias: float) -> None:
    """Pin the number that the limitations section quotes.

    If this drifts, the disclosure shipped to clinicians is wrong and must be
    updated with it - that is the entire reason it is asserted here.
    """
    bias = _measure(truth, NATIVE_0826, reliable_only=True) - truth
    assert bias == pytest.approx(expected_bias, abs=0.05)


def test_anisotropy_bias_is_not_correctable_by_a_factor() -> None:
    """The bias is non-monotone, so no single scale factor can remove it."""
    biases = [ANISOTROPY_COST_MM[t] for t in sorted(ANISOTROPY_COST_MM)]
    assert min(biases) < 0 < max(biases), (
        "bias changed sign in the source data but not here; recheck before "
        "anyone concludes it can be calibrated out")
