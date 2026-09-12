"""Rule-based subregion parcellation, in the Eckstein / CartiMorph scheme.

Chosen over anything of our own devising for one reason: it is the only
partition with published sex-specific reference values, and reference values
are what the doctors asked for.

Slice support drives which subdivisions are honest here. Each plate spans only
10-12.5 native sagittal slices on this data (measured, see ``probe.py``), and
that budget is spent differently by different cuts:

* An **angular** cut of a femoral condyle (trochlear / central / posterior)
  costs no slices at all - every sector still spans the condyle's full
  medio-lateral width. These are safe.
* A **medio-lateral** cut divides those 10-12 slices directly. The central-band
  thirds (ecMF/ccMF/icMF) leave ~4 slices each: reported, but flagged.

So subregions carry an explicit ``slice_support`` and the aggregator downgrades
confidence from it rather than from a hard-coded guess.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .frame import (LAT_FEM_CART, LAT_TIB_CART, MED_FEM_CART, MED_TIB_CART,
                    PAT_CART, KneeFrame)

# Femoral boundaries as fractions of each condyle's OWN angular span, measured
# about the fitted circle centre and weighted by surface area.
#
# Absolute degree thresholds were tried first and are wrong for this data: the
# cartilage spans roughly 30-200 deg on the medial condyle but 5-195 deg on the
# lateral one, so a fixed "trochlea below 25 deg" captured a sliver of nothing
# medially while "central 25-100 deg" landed on the anterior surface rather
# than the weight-bearing one. Against Framingham that showed up as ccMF/icMF
# reading 30% thin while ecMF read 19% thick - a displaced boundary, not noise.
# Fractions self-calibrate per condyle and per knee.
TROCHLEA_SPAN_FRACTION = 0.25
CENTRAL_SPAN_FRACTION = 0.65
# Fraction of tibial plateau area given to the central ROI.
TIBIAL_CENTRAL_AREA_FRACTION = 0.20
# Patellar facets split at the bone centroid, shifted slightly laterally.
PATELLA_FACET_SHIFT = 0.10

# subregion code -> (label it belongs to, MOAKS name, human name)
SUBREGION_NAMES: dict[str, tuple[str, str]] = {
    "MFT": ("MF (trochlea)", "股骨内侧滑车"),
    "ecMF": ("cMF", "股骨内侧髁中央-外侧带"),
    "ccMF": ("cMF", "股骨内侧髁中央-中央带"),
    "icMF": ("cMF", "股骨内侧髁中央-内侧带"),
    "pMF": ("pMF", "股骨内侧髁后部"),
    "LFT": ("LF (trochlea)", "股骨外侧滑车"),
    "ecLF": ("cLF", "股骨外侧髁中央-外侧带"),
    "ccLF": ("cLF", "股骨外侧髁中央-中央带"),
    "icLF": ("cLF", "股骨外侧髁中央-内侧带"),
    "pLF": ("pLF", "股骨外侧髁后部"),
    "cMT": ("cMT", "胫骨内侧平台中央"),
    "aMT": ("aMT", "胫骨内侧平台前部"),
    "eMT": ("eMT", "胫骨内侧平台外缘"),
    "pMT": ("pMT", "胫骨内侧平台后部"),
    "iMT": ("iMT", "胫骨内侧平台内缘"),
    "cLT": ("cLT", "胫骨外侧平台中央"),
    "aLT": ("aLT", "胫骨外侧平台前部"),
    "eLT": ("eLT", "胫骨外侧平台外缘"),
    "pLT": ("pLT", "胫骨外侧平台后部"),
    "iLT": ("iLT", "胫骨外侧平台内缘"),
    "MP": ("MP", "髌骨内侧面"),
    "LP": ("LP", "髌骨外侧面"),
}


@dataclass
class Parcellation:
    """Per-vertex subregion assignment for one cartilage plate."""
    codes: np.ndarray           # object array of subregion code per vertex
    slice_support: dict[str, int]   # code -> native slices the subregion spans
    notes: list[str]


def _fit_circle(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Kasa least-squares circle fit; returns (cx, cy, r).

    A femoral condyle is close to circular in sagittal projection, and fitting
    it means the angular boundaries do not move when the knee is imaged at a
    different flexion angle - which an anterior-posterior coordinate split
    would not survive.
    """
    a = np.stack([x, y, np.ones_like(x)], axis=1)
    b = x ** 2 + y ** 2
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = sol[0] / 2.0, sol[1] / 2.0
    r = float(np.sqrt(max(sol[2] + cx ** 2 + cy ** 2, 0.0)))
    return float(cx), float(cy), r


def _slice_support(codes: np.ndarray, slice_index: np.ndarray) -> dict[str, int]:
    """How many distinct native slices each subregion actually spans."""
    out: dict[str, int] = {}
    for code in set(codes.tolist()):
        if code:
            out[str(code)] = int(len(np.unique(slice_index[codes == code])))
    return out


def parcellate_femur(points_af: np.ndarray, slice_index: np.ndarray,
                     medial: bool) -> Parcellation:
    """Split a femoral condyle into trochlear / central / posterior, then the
    central band into medio-lateral thirds.

    ``points_af`` holds anatomic-frame coordinates in mm, columns
    (ML, AP, SI), with +ML medial for this knee.
    """
    ap, si, ml = points_af[:, 1], points_af[:, 2], points_af[:, 0]
    cx, cy, _ = _fit_circle(ap, si)

    # Angle measured from the anterior direction, sweeping down and back:
    # 0 deg is anterior (trochlea), 90 deg is inferior, 180 deg is posterior.
    theta = np.degrees(np.arctan2(-(si - cy), ap - cx))
    theta = np.where(theta < 0, theta + 360.0, theta)

    # Boundaries at fractions of this condyle's own span. Robust percentiles,
    # so a few stray vertices on the intercondylar notch cannot drag them.
    theta_lo, theta_hi = np.percentile(theta, [3.0, 97.0])
    span = theta_hi - theta_lo
    trochlea_max = theta_lo + TROCHLEA_SPAN_FRACTION * span
    central_max = theta_lo + CENTRAL_SPAN_FRACTION * span

    prefix = "M" if medial else "L"
    codes = np.empty(len(points_af), dtype=object)
    codes[:] = ""
    trochlea = theta < trochlea_max
    central = (theta >= trochlea_max) & (theta < central_max)
    posterior = theta >= central_max

    codes[trochlea] = f"{prefix}FT"
    codes[posterior] = f"p{prefix}F"

    notes: list[str] = []
    if central.any():
        # Medio-lateral thirds of the central band, by equal width. This is the
        # cut that spends slices, hence the support figure reported alongside.
        band = ml[central]
        band_lo, band_hi = np.percentile(band, [2.0, 98.0])
        edges = np.linspace(band_lo, band_hi, 4)
        third = np.clip(np.digitize(band, edges[1:-1]), 0, 2)
        # +ML is medial, so ascending third order runs lateral -> medial.
        sub = np.array([f"ec{prefix}F", f"cc{prefix}F", f"ic{prefix}F"])[third]
        codes[central] = sub
    else:
        notes.append(f"no central weight-bearing band found on {prefix}F")

    return Parcellation(codes, _slice_support(codes, slice_index), notes)


def parcellate_tibia(points_af: np.ndarray, slice_index: np.ndarray,
                     medial: bool) -> Parcellation:
    """Central ellipse plus four peripheral quadrants, OARSI/Eckstein style."""
    ml, ap = points_af[:, 0], points_af[:, 1]
    c_ml, c_ap = float(np.median(ml)), float(np.median(ap))
    u, v = ml - c_ml, ap - c_ap

    # Scale each axis by its own spread so the central ROI is an ellipse
    # matching the plateau's aspect ratio rather than a circle.
    s_u = float(np.std(u)) or 1.0
    s_v = float(np.std(v)) or 1.0
    r = np.sqrt((u / s_u) ** 2 + (v / s_v) ** 2)
    r_cut = float(np.quantile(r, TIBIAL_CENTRAL_AREA_FRACTION))

    prefix = "M" if medial else "L"
    codes = np.empty(len(points_af), dtype=object)
    central = r <= r_cut
    codes[central] = f"c{prefix}T"

    # Peripheral ring by quadrant. +AP is anterior; +ML is medial.
    ring = ~central
    ang = np.degrees(np.arctan2(u[ring] / s_u, v[ring] / s_v))   # 0 = anterior
    quad = np.empty(int(ring.sum()), dtype=object)
    quad[:] = f"a{prefix}T"
    quad[(ang >= 45.0) & (ang < 135.0)] = f"i{prefix}T"      # toward medial
    quad[np.abs(ang) >= 135.0] = f"p{prefix}T"               # posterior
    quad[(ang < -45.0) & (ang > -135.0)] = f"e{prefix}T"     # toward lateral
    codes[ring] = quad

    return Parcellation(codes, _slice_support(codes, slice_index), [])


def parcellate_patella(points_af: np.ndarray, slice_index: np.ndarray
                       ) -> Parcellation:
    """Medial and lateral patellar facets.

    Split at the cartilage's own medio-lateral median, nudged laterally: the
    lateral facet is normally the broader of the two, so an exact median would
    systematically clip it.
    """
    ml = points_af[:, 0]
    lo, hi = np.percentile(ml, [2.0, 98.0])
    cut = float(np.median(ml)) - PATELLA_FACET_SHIFT * (hi - lo)

    codes = np.empty(len(points_af), dtype=object)
    codes[ml >= cut] = "MP"          # +ML is medial
    codes[ml < cut] = "LP"
    return Parcellation(codes, _slice_support(codes, slice_index), [])


def parcellate(label: int, points_lps: np.ndarray, slice_index: np.ndarray,
               frame: KneeFrame) -> Parcellation:
    """Dispatch to the right rule for a cartilage label.

    ``points_lps`` are interface vertices in patient LPS mm; ``slice_index`` is
    the native sagittal slice each one falls on, used only to report support.
    """
    # Into the anatomic frame, with +ML meaning medial for THIS knee so that
    # left and right knees parcellate identically.
    axes = frame.axes.copy()
    axes[0] *= frame.medial_sign
    points_af = points_lps @ axes.T

    if label == MED_FEM_CART:
        return parcellate_femur(points_af, slice_index, medial=True)
    if label == LAT_FEM_CART:
        return parcellate_femur(points_af, slice_index, medial=False)
    if label == MED_TIB_CART:
        return parcellate_tibia(points_af, slice_index, medial=True)
    if label == LAT_TIB_CART:
        return parcellate_tibia(points_af, slice_index, medial=False)
    if label == PAT_CART:
        return parcellate_patella(points_af, slice_index)
    raise ValueError("label %d is not a cartilage plate" % label)
