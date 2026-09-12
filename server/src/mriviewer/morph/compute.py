"""The per-segmentation morphometry pipeline: masks in, metric tree out.

Deliberately free of database and HTTP concerns so it can be run over a folder
of NIfTI files during development and validated against the literature before
any of it is wired to an endpoint.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
from scipy.ndimage import map_coordinates

from .frame import KneeFrame, knee_frame, native_spacing_along
from .geometry import dense_surface, iso_fields, normals_to_lps, to_lps, vertex_areas
from .parcellation import SUBREGION_NAMES, anatomic_axes, parcellate
from .probe import PLATES, RELIABLE_RES_MM, TAU_BONE_MM
from .thickness import histogram, thickness_map, weighted_stats

ALGO_VERSION = "morph-0.3.0"

# A subregion needs at least this many native slices before its mean thickness
# is reported without a low-confidence flag. Three is the minimum that can
# show a trend rather than a single slice plus its neighbours.
MIN_SLICES_FOR_CONFIDENCE = 3
# ...and at least this much of its interface area sampled at <= RELIABLE_RES_MM.
MIN_RELIABLE_AREA_FRACTION = 0.35
# Ray-cast and nearest-neighbour thickness must agree this closely.
RAY_NN_RATIO_RANGE = (0.85, 1.15)


@dataclass
class PlateResult:
    label: int
    name: str
    volume_mm3: float
    interface_area_mm2: float
    thickness: dict[str, Any]
    histogram: dict[str, Any]
    subregions: dict[str, Any] = field(default_factory=dict)
    qc: dict[str, Any] = field(default_factory=dict)
    grading: dict[str, Any] = field(default_factory=dict)      # plate-level Outerbridge summary
    lesions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PlateArrays:
    """The per-vertex working set of one plate, for drawing from.

    Never serialised and never stored: tens of thousands of vertices per
    plate, kept only as long as it takes ``morph.figures`` to rasterise them
    in the same process that measured them. Every figure is therefore drawn
    from exactly the vertices the numbers came from.
    """
    label: int
    name: str
    axes: np.ndarray            # anatomic axes, +ML medial (parcellation.anatomic_axes)
    points_lps: np.ndarray      # (N, 3) interface vertices, patient LPS mm
    points_af: np.ndarray       # (N, 3) the same in (ML, AP, SI)
    th: np.ndarray              # thickness per vertex, NaN where the ray escaped
    areas: np.ndarray           # mm^2 per vertex
    res_eff: np.ndarray         # effective native resolution along the normal
    codes: np.ndarray           # subregion code per vertex
    boundary: np.ndarray        # plate-margin vertices
    frame2d: dict[str, Any]     # the parcellation's own 2D chart
    lesions: list[Any]          # outerbridge.Lesion, with their vertex points


# bone label -> the cartilage plates that sit on it
PLATES_OF_BONE = {1: [4, 5], 2: [6, 7], 3: [8]}


def compute_morphometry(canonical: np.ndarray, geom,
                        header_laterality: str | None = None,
                        *, strict: bool = True, grading: Any = None,
                        keep_arrays: bool = False) -> dict[str, Any]:
    """Full metric tree for one segmentation.

    Raises when the medial/lateral labels look swapped: a mirrored subregion
    report is worse than no report at all, because it is wrong in a way that
    reads as plausible.

    With ``keep_arrays`` the tree also carries ``"arrays"`` (label ->
    ``PlateArrays``), the per-vertex data the figures are drawn from. It is
    not JSON and the caller must pop it before storing anything.
    """
    masks = {int(v): (canonical == v) for v in np.unique(canonical) if v != 0}
    frame = knee_frame(masks, geom, header_laterality)
    if strict and not frame.medial_lateral_consistent:
        raise ValueError("; ".join(frame.notes) or "medial/lateral labels inconsistent")

    from .coverage import bone_coverage
    from .outerbridge import GradingParams

    grading = grading or GradingParams()
    voxel_mm3 = float(np.prod(geom.spacing))

    # Coverage once per bone, with all of that bone's plates together: dilating
    # one plate's covered region on its own would march across the trochlea
    # into the other plate.
    coverage: dict[int, Any] = {}
    for bone, carts in PLATES_OF_BONE.items():
        present = [c for c in carts if c in masks]
        if bone not in masks or not present:
            continue
        try:
            fields, grid = iso_fields(masks, geom, [bone] + present)
            cov = bone_coverage(fields, grid, geom, frame, bone, present,
                                closing_radius_mm=grading.closing_radius_mm,
                                min_denuded_area_mm2=grading.min_denuded_area_mm2,
                                min_denuded_slices=grading.min_lesion_slices)
        except Exception as exc:              # noqa: BLE001 - coverage is optional, grading IV is not
            cov = None
            frame.notes.append("coverage of bone %d failed: %s" % (bone, exc))
        if cov is not None:
            coverage[bone] = cov

    plates: list[PlateResult] = []
    arrays: dict[int, PlateArrays] = {}
    for cart, (bone, name) in PLATES.items():
        if cart not in masks or bone not in masks:
            continue
        cov = coverage.get(bone)
        patches = [p for p in (cov.denuded if cov else []) if p.cart_label == cart]
        result = _one_plate(masks, geom, frame, cart, bone, name, voxel_mm3,
                            grading=grading, denuded_patches=patches)
        if result is not None:
            plates.append(result[0])
            arrays[cart] = result[1]

    tree: dict[str, Any] = {
        "algoVersion": ALGO_VERSION,
        "frame": frame.to_json(),
        "plates": [asdict(p) for p in plates],
        "compartments": _compartments(plates),
        "coverage": {str(b): c.to_json() for b, c in coverage.items()},
        "grading": {"scale": "outerbridge_mri_thickness", "params": grading.to_json(),
                    "gradeIAssessable": False, "detectionFloorMm": float(geom.spacing[2])},
    }
    if keep_arrays:
        tree["arrays"] = arrays
    return tree


def _one_plate(masks, geom, frame: KneeFrame, cart: int, bone: int, name: str,
               voxel_mm3: float, *, grading: Any = None,
               denuded_patches: list[Any] | None = None
               ) -> tuple[PlateResult, PlateArrays] | None:
    from .outerbridge import GradingParams, grade_plate
    grading = grading or GradingParams()
    fields, grid = iso_fields(masks, geom, [cart, bone])
    surface = dense_surface(fields[cart])
    if surface is None:
        return None
    verts, faces, normals_kji = surface

    # The bone-cartilage interface: cartilage surface vertices sitting on bone.
    d_bone = map_coordinates(fields[bone], verts.T, order=1, mode="nearest")
    interface = d_bone > -(TAU_BONE_MM / grid.iso_mm)
    if not interface.any():
        return None

    result = thickness_map(fields[cart], verts, normals_kji, interface, grid.iso_mm)
    areas = vertex_areas(verts, faces)[interface] * (grid.iso_mm ** 2)
    normals_lps = normals_to_lps(normals_kji, geom)[interface]
    res_eff = native_spacing_along(frame, geom, normals_lps)
    points_lps = to_lps(verts[interface], grid, geom)

    # Which native slice each interface vertex belongs to, for slice support.
    slice_index = np.round(verts[interface, 0] * grid.scale[0]
                           + grid.lo[0]).astype(int)

    parc = parcellate(cart, points_lps, slice_index, frame)
    th = result.thickness_mm

    subregions: dict[str, Any] = {}
    for code in sorted({c for c in parc.codes.tolist() if c}):
        sel = parc.codes == code
        stats = weighted_stats(th[sel], areas[sel])
        support = parc.slice_support.get(code, 0)
        reliable_fraction = (float(areas[sel & (res_eff <= RELIABLE_RES_MM)].sum()
                                   / areas[sel].sum()) if areas[sel].sum() else 0.0)
        moaks, label_zh = SUBREGION_NAMES.get(code, (code, code))
        subregions[code] = {
            "moaks": moaks, "labelZh": label_zh,
            "thicknessMm": stats,
            "sliceSupport": support,
            "reliableAreaFraction": round(reliable_fraction, 3),
            "effectiveResolutionMm": round(
                float((res_eff[sel] * areas[sel]).sum() / areas[sel].sum()), 3),
            "confidence": _confidence(support, reliable_fraction,
                                      result.ray_vs_nn_median_ratio),
        }

    # Faces of the interface only, re-indexed into the interface vertex set,
    # so lesions are connected components on the bone-cartilage surface.
    remap = np.full(len(verts), -1, int)
    remap[np.flatnonzero(interface)] = np.arange(int(interface.sum()))
    fi = remap[faces]
    interface_faces = fi[(fi >= 0).all(axis=1)]

    # Plate margin: interface vertices that share a face with a non-interface
    # vertex. The plate tapers to nothing there by anatomy.
    fi_any = (fi >= 0).any(axis=1) & ~(fi >= 0).all(axis=1)
    boundary_full = np.zeros(len(verts), bool)
    boundary_full[faces[fi_any].ravel()] = True
    boundary = boundary_full[interface]

    grades, lesions, grading_summary = grade_plate(
        th=th, areas=areas, res_eff=res_eff, codes=parc.codes,
        interface_verts_iso=verts[interface], interface_faces=interface_faces,
        slice_index=slice_index, points_lps=points_lps,
        denuded_patches=list(denuded_patches or []),
        subregion_confidence={c: s["confidence"] for c, s in subregions.items()},
        params=grading, boundary=boundary)
    for code, g in grades.items():
        if code in subregions:
            subregions[code]["outerbridge"] = g

    reliable_all = float(areas[res_eff <= RELIABLE_RES_MM].sum() / areas.sum())
    axes = anatomic_axes(frame)
    plate_arrays = PlateArrays(
        label=cart, name=name, axes=axes, points_lps=points_lps,
        points_af=points_lps @ axes.T, th=th, areas=areas, res_eff=res_eff,
        codes=parc.codes, boundary=boundary, frame2d=parc.frame2d, lesions=lesions)
    return PlateResult(
        label=cart, name=name,
        volume_mm3=round(float(masks[cart].sum()) * voxel_mm3, 1),
        interface_area_mm2=round(float(areas.sum()), 1),
        thickness=weighted_stats(th, areas),
        histogram=histogram(th, areas),
        subregions=subregions,
        qc={
            "escapedFraction": round(float(result.escaped.mean()), 4),
            "rayVsNnMedianRatio": (None if not np.isfinite(result.ray_vs_nn_median_ratio)
                                   else round(result.ray_vs_nn_median_ratio, 3)),
            "reliableAreaFraction": round(reliable_all, 3),
            "effectiveResolutionMm": round(
                float((res_eff * areas).sum() / areas.sum()), 3),
            "isoMm": grid.iso_mm,
            "notes": parc.notes,
        },
        grading=grading_summary,
        lesions=[l.to_json() for l in lesions],
    ), plate_arrays


def _confidence(slice_support: int, reliable_fraction: float,
                ray_nn_ratio: float) -> str:
    """Confidence in a subregion's mean thickness.

    Note what this does *not* claim. Even 'high' means only that the subregion
    is well sampled by this acquisition; on 3 mm slices the measurement still
    carries a systematic underestimate of roughly 7-18% that no amount of
    sampling within the slice can remove (see tests/test_thickness_phantom.py).
    Confidence here is about precision, never about accuracy.
    """
    lo, hi = RAY_NN_RATIO_RANGE
    if np.isfinite(ray_nn_ratio) and not (lo <= ray_nn_ratio <= hi):
        return "low"
    if slice_support < MIN_SLICES_FOR_CONFIDENCE:
        return "low"
    if reliable_fraction < MIN_RELIABLE_AREA_FRACTION:
        return "low"
    if slice_support < 2 * MIN_SLICES_FOR_CONFIDENCE or reliable_fraction < 0.55:
        return "medium"
    return "high"


def _compartments(plates: list[PlateResult]) -> dict[str, Any]:
    """Compartment rollups, plus the medial-lateral asymmetry.

    Asymmetry is the headline number on anisotropic data: the slice-thickness
    bias is common to both compartments of the same knee, so it largely cancels
    in a ratio while it does not cancel in an absolute millimetre value.
    """
    by_label = {p.label: p for p in plates}

    def mean_of(label: int) -> float | None:
        p = by_label.get(label)
        return None if p is None else p.thickness.get("mean")

    out: dict[str, Any] = {}
    for name, femur, tibia in (("medial", 4, 6), ("lateral", 5, 7)):
        f, t = mean_of(femur), mean_of(tibia)
        out[name] = {
            "femoralThicknessMm": f, "tibialThicknessMm": t,
            "sumThicknessMm": None if f is None or t is None else round(f + t, 3),
            "volumeMm3": round(sum(by_label[l].volume_mm3 for l in (femur, tibia)
                                   if l in by_label), 1),
        }

    med, lat = out["medial"]["sumThicknessMm"], out["lateral"]["sumThicknessMm"]
    if med and lat:
        out["medialLateralRatio"] = round(med / lat, 3)
        out["asymmetryPct"] = round(100.0 * (med - lat) / (0.5 * (med + lat)), 1)
    return out
