"""Outerbridge-style cartilage grading, derived from thickness.

What this is: an MRI-adapted, thickness-only reading of the Outerbridge
scale, computed in Python from the same per-vertex thickness map the report
already shows. What it is not: the arthroscopic scale, or a signal-based
reading, or anything validated against arthroscopy on this data.

    0    no focal thinning
    I    softening / signal change  -> NOT assessable from a segmentation;
         every report says so rather than silently skipping it
    II   focal deficit  <50% of baseline
    III  focal deficit >=50% of baseline
    IV   bare bone inside the covered plate (a denuded patch, from coverage.py)

Baseline is the plate's own median thickness over reliable vertices, not a
literature value. The acquisition underestimates thickness by 7-18% and that
bias is common to a plate and its lesions, so a patient-internal baseline
mostly cancels it. The price: a plate that is thin everywhere has little
*focal* deficit and will be under-called. That is written into the output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import connected_components, dijkstra

from .compute import MIN_RELIABLE_AREA_FRACTION
from .probe import RELIABLE_RES_MM

SCALE = "outerbridge_mri_thickness"
GRADE_RANK = {"0": 0, "II": 2, "III": 3, "IV": 4, "未评估": -1}


@dataclass
class GradingParams:
    """All from the reference YAML's ``outerbridge:`` block, hashed into the
    morphometry params so a threshold edit produces a new row."""
    deficit_ii: float = 0.25
    deficit_iii: float = 0.50
    min_lesion_area_mm2: float = 30.0
    min_lesion_slices: int = 2
    closing_radius_mm: float = 4.0
    min_denuded_area_mm2: float = 20.0
    min_baseline_mm: float = 0.8
    # A plate thins to nothing at its own margin. That taper is anatomy, not a
    # lesion, so vertices within this geodesic distance of the plate edge are
    # excluded from focal grading. Measured on real cases: without it, every
    # plate grew a ring of "grade II" along its rim.
    edge_margin_mm: float = 3.0
    # A denuded patch is grade IV only if cartilage surrounds it. Below this
    # enclosure it is reported as bare area in the coverage figures but not as
    # a lesion: on this data it is usually the cartilage label stopping short.
    min_enclosure: float = 0.8
    # Baseline for "focal": the area-weighted mean thickness of the cartilage
    # within this geodesic radius of each vertex (excluding unreliable
    # vertices). Anatomic gradients - trochlear ridge to sulcus, condyle to
    # posterior - span tens of millimetres and are absorbed; a lesion a few
    # millimetres across is not.
    local_baseline_radius_mm: float = 10.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def load_grading_params(refs: dict[str, Any] | None) -> GradingParams:
    block = (refs or {}).get("outerbridge") or {}
    p = GradingParams()
    for k in p.to_json():
        if k in block:
            setattr(p, k, type(getattr(p, k))(block[k]))
    return p


@dataclass
class Lesion:
    code: str
    grade: str
    area_mm2: float
    median_deficit_pct: float
    min_thickness_mm: float | None
    n_slices: int
    centroid_lps: list[float]
    confidence: str

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["area_mm2"] = round(d["area_mm2"], 1)
        d["median_deficit_pct"] = round(d["median_deficit_pct"], 0)
        if d["min_thickness_mm"] is not None:
            d["min_thickness_mm"] = round(d["min_thickness_mm"], 2)
        d["centroid_lps"] = [round(x, 1) for x in d["centroid_lps"]]
        return d


def _wmedian(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    cw = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cw, 0.5 * cw[-1])])


def _local_baseline(graph, th: np.ndarray, areas: np.ndarray, reliable: np.ndarray,
                    points_mm: np.ndarray, radius_mm: float, *, fallback: float) -> np.ndarray:
    """Area-weighted mean thickness of the reliable cartilage around each vertex.

    Diffusion on the surface graph: each step replaces a vertex's value with
    the average of its neighbours'. After k steps the kernel's radius is about
    sqrt(k) times the mean edge length, so k is chosen from the requested
    radius. Weights (areas, zero where unreliable) diffuse alongside so the
    result stays a proper weighted mean and holes do not drag it down.
    """
    n = len(th)
    if graph is None or n == 0 or radius_mm <= 0:
        return np.full(n, fallback)
    g = graph.tocsr()
    deg = np.asarray(g.getnnz(axis=1)).ravel()
    if not deg.any():
        return np.full(n, fallback)
    mean_edge = float(g.data.mean()) if g.nnz else 1.0
    steps = int(np.clip(round((radius_mm / max(mean_edge, 1e-6)) ** 2), 1, 2000))
    # unweighted neighbour average with a self-loop, row-normalised
    from scipy import sparse
    adj = (g > 0).astype(float) + sparse.identity(n, format="csr")
    inv = 1.0 / np.asarray(adj.sum(axis=1)).ravel()
    P = sparse.diags(inv) @ adj
    w = np.where(reliable, areas, 0.0)
    num = np.where(reliable, th * areas, 0.0)
    for _ in range(steps):
        num = P @ num
        w = P @ w
    out = np.where(w > 1e-9, num / np.maximum(w, 1e-9), fallback)
    return out


def _worse(a: str, b: str) -> str:
    return a if GRADE_RANK[a] >= GRADE_RANK[b] else b


def _lower_conf(a: str, b: str) -> str:
    rank = {"high": 2, "medium": 1, "low": 0}
    return a if rank.get(a, 0) <= rank.get(b, 0) else b


def grade_plate(*, th: np.ndarray, areas: np.ndarray, res_eff: np.ndarray,
                codes: np.ndarray, interface_verts_iso: np.ndarray,
                interface_faces: np.ndarray, slice_index: np.ndarray,
                points_lps: np.ndarray, denuded_patches: list[Any],
                subregion_confidence: dict[str, str],
                params: GradingParams,
                boundary: np.ndarray | None = None
                ) -> tuple[dict[str, dict[str, Any]], list[Lesion], dict[str, Any]]:
    """Grade one cartilage plate.

    All arrays are per *interface* vertex (the bone-cartilage surface).
    ``interface_faces`` are triangles re-indexed into that vertex set.
    ``denuded_patches`` are this plate's bare-bone patches from coverage.py.
    Returns (per-subregion grades, lesions, plate summary).
    """
    all_codes = sorted({c for c in codes.tolist() if c})
    summary: dict[str, Any] = {
        "scale": SCALE, "gradeIAssessable": False, "detectionFloorMm": 3.0,
        "baselineMm": None, "baselineStat": "median over reliable vertices",
        "reliableAreaFraction": None, "reason": None,
    }

    def not_assessed(reason: str):
        summary["reason"] = reason
        grades = {c: {"grade": "未评估", "confidence": "low", "lesionAreaMm2": 0.0,
                      "deficitPct": None, "baselineMm": None, "nSlices": 0, "reason": reason}
                  for c in all_codes}
        return grades, [], summary

    finite = np.isfinite(th)
    reliable = finite & (res_eff <= RELIABLE_RES_MM) & (areas > 0)
    total_area = float(areas[finite].sum())
    if total_area <= 0:
        return not_assessed("no measurable interface")
    reliable_fraction = float(areas[reliable].sum() / total_area)
    summary["reliableAreaFraction"] = round(reliable_fraction, 3)
    if reliable_fraction < MIN_RELIABLE_AREA_FRACTION:
        return not_assessed("reliable interface area below %.0f%%" % (100 * MIN_RELIABLE_AREA_FRACTION))

    baseline = _wmedian(th[reliable], areas[reliable])
    summary["baselineMm"] = round(baseline, 3)
    if baseline < params.min_baseline_mm:
        return not_assessed("plate baseline %.2f mm below %.2f mm" % (baseline, params.min_baseline_mm))

    n = len(th)
    graph = None
    if len(interface_faces):
        from .coverage import vertex_graph
        graph = vertex_graph(points_lps, interface_faces, 1.0)      # points already in mm

    # Subregion medians are reported for the table; grading uses the local
    # baseline below.
    baseline_of: dict[str, float] = {}
    for c in all_codes:
        m = reliable & (codes == c)
        baseline_of[c] = _wmedian(th[m], areas[m]) if areas[m].sum() >= 25.0 else baseline
    summary["baselineBySubregionMm"] = {c: round(v, 3) for c, v in baseline_of.items()}

    # "Focal" means thinner than the cartilage around it. The local baseline is
    # an area-weighted mean over a geodesic neighbourhood, computed by
    # repeated neighbour averaging on the surface graph (a diffusion), which
    # follows anatomic gradients that a subregion median cannot.
    # Two passes: a lesion must not be allowed to pull down its own baseline.
    # Pass one finds the candidates; pass two estimates the baseline from the
    # cartilage that is *not* a candidate.
    first = _local_baseline(graph, th, areas, reliable, points_lps,
                            params.local_baseline_radius_mm, fallback=baseline)
    first = np.where(first >= params.min_baseline_mm, first, baseline)
    suspect = reliable & (np.where(finite, 1.0 - th / first, 0.0) >= params.deficit_ii)
    per_vertex = _local_baseline(graph, th, areas, reliable & ~suspect, points_lps,
                                 params.local_baseline_radius_mm, fallback=baseline)
    per_vertex = np.where(per_vertex >= params.min_baseline_mm, per_vertex, baseline)

    deficit = np.where(finite, 1.0 - th / per_vertex, 0.0)

    # Margin exclusion: the plate's own edge tapers to zero thickness.
    interior = np.ones(n, bool)
    if boundary is not None and boundary.any() and graph is not None and params.edge_margin_mm > 0:
        d = dijkstra(graph, directed=False, indices=np.flatnonzero(boundary),
                     limit=params.edge_margin_mm, min_only=True)
        interior = ~np.isfinite(d)
    summary["edgeExcludedAreaFraction"] = round(
        float(areas[finite & ~interior].sum() / total_area), 3)

    candidate = reliable & interior & (deficit >= params.deficit_ii)

    lesions: list[Lesion] = []
    if candidate.any() and graph is not None:
        f = interface_faces
        keep = candidate[f[:, 0]] & candidate[f[:, 1]] & candidate[f[:, 2]]
        f = f[keep]
        if len(f):
            i = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
            j = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
            g = sparse.coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)).tocsr()
            _, labels = connected_components(g, directed=False)
            labels = np.where(candidate, labels, -1)
            for lab in np.unique(labels[labels >= 0]):
                m = labels == lab
                area = float(areas[m].sum())
                n_sl = int(len(np.unique(slice_index[m])))
                # A patch confined to one 3 mm slice cannot be told apart from
                # partial volume at a label edge; it is not reported at all.
                if area < params.min_lesion_area_mm2 or n_sl < params.min_lesion_slices:
                    continue
                med = _wmedian(deficit[m], areas[m])
                grade = "III" if med >= params.deficit_iii else "II"
                cs = [c for c in codes[m].tolist() if c]
                code = max(set(cs), key=cs.count) if cs else ""
                conf = "low" if _wmedian(res_eff[m], areas[m]) > RELIABLE_RES_MM else "medium"
                lesions.append(Lesion(
                    code=code, grade=grade, area_mm2=area, median_deficit_pct=100 * med,
                    min_thickness_mm=float(np.nanmin(th[m])), n_slices=n_sl,
                    centroid_lps=points_lps[m].mean(0).tolist(), confidence=conf))

    for p in denuded_patches:
        if getattr(p, "enclosure", 1.0) < params.min_enclosure:
            continue                      # bare rim, not a hole in the plate
        lesions.append(Lesion(
            code=getattr(p, "code", "") or "", grade="IV", area_mm2=float(p.area_mm2),
            median_deficit_pct=100.0, min_thickness_mm=0.0, n_slices=int(p.n_slices),
            centroid_lps=list(p.centroid_lps),
            confidence="low" if p.n_slices < params.min_lesion_slices else "medium"))

    grades: dict[str, dict[str, Any]] = {}
    for c in all_codes:
        mine = [l for l in lesions if l.code == c]
        grade = "0"
        for l in mine:
            grade = _worse(grade, l.grade)
        conf = subregion_confidence.get(c, "low")
        for l in mine:
            conf = _lower_conf(conf, l.confidence)
        worst = max(mine, key=lambda l: (GRADE_RANK[l.grade], l.area_mm2), default=None)
        grades[c] = {
            "grade": grade, "confidence": conf,
            "lesionAreaMm2": round(sum(l.area_mm2 for l in mine), 1),
            "deficitPct": round(worst.median_deficit_pct, 0) if worst else None,
            "baselineMm": round(baseline_of.get(c, baseline), 3),
            "nSlices": worst.n_slices if worst else 0,
            "reason": None,
        }
    return grades, lesions, summary
