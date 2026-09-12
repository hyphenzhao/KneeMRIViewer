"""Cartilage thickness by surface-normal ray casting, with a cross-check.

This is the CartiMorph / Chondrometrics definition: from each point of the
bone-cartilage interface, travel along the surface normal and measure how far
it is to the far side of the cartilage. It is not the same as an inscribed
sphere, and on thin curved plates the difference is not small - which is why
the nearest-neighbour estimate is computed too, and their disagreement is
reported rather than hidden.

Every thickness also carries the *effective resolution* of the native data
along the direction it was measured in (see ``frame.native_spacing_along``).
On 3 mm sagittal knees that is ~0.3-0.5 mm over the central condyles, plateaux
and patella, and ~3 mm at the rims. Reporting the number without that companion
would be the dishonest version of this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates

# Ray marching. STEP is a quarter of the isotropic voxel; MAX is comfortably
# thicker than any knee cartilage (patellar cartilage peaks around 6 mm).
RAY_STEP_MM = 0.125
RAY_MAX_MM = 9.0


@dataclass
class ThicknessResult:
    thickness_mm: np.ndarray      # per interface vertex, NaN where the ray escaped
    nn_mm: np.ndarray             # nearest-neighbour cross-check, same vertices
    escaped: np.ndarray           # bool, ray found no far surface within RAY_MAX_MM
    ray_vs_nn_median_ratio: float


def thickness_map(phi_cart: np.ndarray, verts_iso: np.ndarray,
                  normals_iso: np.ndarray, on_interface: np.ndarray,
                  iso_mm: float) -> ThicknessResult:
    """Ray-cast thickness from interface vertices through the cartilage.

    ``phi_cart`` is the cartilage level set (>0 inside) on the isotropic grid;
    ``verts_iso`` / ``normals_iso`` are the **cartilage** surface in isotropic
    voxel units, (k, j, i), with normals pointing **out of the cartilage** as
    ``dense_surface`` produces them.

    The ray direction is **not** taken on faith from the normal's sign. VTK's
    ``AutoOrientNormals`` only guarantees a consistent outward direction for a
    single closed surface; a cartilage plate wrapped around a bone is a thin
    curved sheet whose level set has nested components, and there VTK orients
    both sheets the same way. So each ray is oriented locally by probing which
    side of the vertex the cartilage is actually on. A nested-shell phantom
    fails outright without this, and it fails silently as NaN, not as a wrong
    number.
    """
    src = verts_iso[on_interface]
    nrm = _orient_inward(phi_cart, src, normals_iso[on_interface])
    if not len(src):
        empty = np.zeros(0)
        return ThicknessResult(empty, empty, np.zeros(0, bool), float("nan"))

    # Vertices sit exactly on phi = 0, so sampling at s = 0 is a coin flip on
    # floating-point rounding. Start a fifth of a step in, along the ray.
    n_steps = int(RAY_MAX_MM / RAY_STEP_MM) + 1
    s_mm = np.arange(n_steps) * RAY_STEP_MM + 0.2 * RAY_STEP_MM
    s_vox = s_mm / iso_mm                                        # (S,)

    # (N, S, 3) sample points, evaluated in one interpolation call.
    pts = src[:, None, :] + nrm[:, None, :] * s_vox[None, :, None]
    flat = pts.reshape(-1, 3).T
    phi = map_coordinates(phi_cart, flat, order=1, mode="nearest").reshape(len(src), n_steps)

    # Walk outward to the first crossing from inside (phi>0) to outside (phi<=0).
    inside = phi > 0
    # Ignore rays that start outside the cartilage entirely - a normal pointing
    # the wrong way, or an interface vertex on a sliver.
    starts_inside = inside[:, 0]
    leaves = (~inside) & np.concatenate(
        [np.zeros((len(src), 1), bool), inside[:, :-1]], axis=1)
    has_exit = leaves.any(axis=1)
    first_exit = np.where(has_exit, leaves.argmax(axis=1), 0)

    # Sub-step refinement: linear zero crossing of phi between the bracketing samples.
    idx = np.arange(len(src))
    phi_in = phi[idx, np.maximum(first_exit - 1, 0)]
    phi_out = phi[idx, first_exit]
    denom = phi_in - phi_out
    frac = np.where(np.abs(denom) > 1e-9, phi_in / np.where(denom == 0, 1e-9, denom), 0.0)
    frac = np.clip(frac, 0.0, 1.0)
    steps = np.maximum(first_exit - 1, 0) + frac

    # Distance travelled, measured from the vertex (undo the start offset).
    thickness = steps * RAY_STEP_MM + 0.2 * RAY_STEP_MM
    escaped = ~(has_exit & starts_inside)
    thickness = np.where(escaped, np.nan, thickness)

    # Cross-check: distance from each interface vertex to the nearest point on
    # the cartilage's outer surface. Cheap, independent, and catches a normal
    # field that has gone wrong.
    nn = _nearest_outer_distance(phi_cart, verts_iso, on_interface, iso_mm)

    both = np.isfinite(thickness) & np.isfinite(nn) & (nn > 1e-6)
    ratio = float(np.median(thickness[both] / nn[both])) if both.any() else float("nan")

    return ThicknessResult(thickness_mm=thickness, nn_mm=nn, escaped=escaped,
                           ray_vs_nn_median_ratio=ratio)


def _orient_inward(phi: np.ndarray, verts: np.ndarray,
                    normals: np.ndarray) -> np.ndarray:
    """Flip each normal so it points into the material (where phi increases).

    Probes phi a half-voxel either side of the vertex. Vertices sit on phi = 0,
    so the difference of the two probes has a clean sign wherever the surface
    is locally two-sided - which is everywhere except a sliver one voxel thick.
    """
    eps = 0.5
    fwd = map_coordinates(phi, (verts + eps * normals).T, order=1, mode="nearest")
    bwd = map_coordinates(phi, (verts - eps * normals).T, order=1, mode="nearest")
    sign = np.where(fwd >= bwd, 1.0, -1.0)[:, None]
    return normals * sign


def _nearest_outer_distance(phi_cart: np.ndarray, verts_iso: np.ndarray,
                            on_interface: np.ndarray, iso_mm: float) -> np.ndarray:
    """Straight-line distance from interface vertices to the outer cartilage surface."""
    from scipy.spatial import cKDTree

    outer = verts_iso[~on_interface]
    if not len(outer):
        return np.full(int(on_interface.sum()), np.nan)
    tree = cKDTree(outer)
    d, _ = tree.query(verts_iso[on_interface], k=1)
    return d * iso_mm


def weighted_stats(values: np.ndarray, weights: np.ndarray,
                   quantiles: tuple[float, ...] = (0.10, 0.50, 0.90)) -> dict[str, Any]:
    """Area-weighted mean and quantiles, ignoring NaN.

    Area weighting is not a refinement: marching-cubes vertex density scales
    with curvature, so an unweighted mean over-counts precisely the curved rims
    that this acquisition resolves worst.
    """
    ok = np.isfinite(values) & (weights > 0)
    if not ok.any():
        return {"mean": None, "max": None, "n": 0, "areaMm2": 0.0,
                **{f"p{int(q*100)}": None for q in quantiles}}
    v, w = values[ok], weights[ok]
    order = np.argsort(v)
    v_s, w_s = v[order], w[order]
    cw = np.cumsum(w_s) / w_s.sum()
    out: dict[str, Any] = {
        "mean": round(float((v * w).sum() / w.sum()), 3),
        "max": round(float(v.max()), 3),
        "n": int(ok.sum()),
        "areaMm2": round(float(w.sum()), 1),
    }
    for q in quantiles:
        i = int(np.searchsorted(cw, q, side="left"))
        out[f"p{int(q*100)}"] = round(float(v_s[min(i, len(v_s) - 1)]), 3)
    return out


def histogram(values: np.ndarray, weights: np.ndarray,
              bin_mm: float = 0.1, max_mm: float = 6.0) -> dict[str, Any]:
    """Area-weighted thickness histogram, computed server-side.

    Shipped inside the metrics JSON so the dashboard can draw the distribution
    without downloading a mesh.
    """
    ok = np.isfinite(values) & (weights > 0)
    edges = np.arange(0.0, max_mm + bin_mm, bin_mm)
    if not ok.any():
        return {"binMm": bin_mm, "edges": edges.round(3).tolist(),
                "areaMm2": [0.0] * (len(edges) - 1)}
    hist, _ = np.histogram(values[ok], bins=edges, weights=weights[ok])
    return {"binMm": bin_mm, "edges": edges.round(3).tolist(),
            "areaMm2": hist.round(2).tolist()}
