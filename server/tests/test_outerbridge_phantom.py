"""Thickness-derived Outerbridge grading on phantoms with known lesions.

`grade_plate` is exercised on a synthetic flat plate where every vertex's
thickness is chosen, so the expected grade is arithmetic, not opinion. The
coverage step (grade IV) is exercised on a spherical bone wearing a cartilage
shell with a hole of known area.
"""
from __future__ import annotations

import numpy as np
import pytest

from mriviewer.morph.outerbridge import GradingParams, grade_plate, load_grading_params

# ------------------------------------------------------------ flat plate

def _plate(nx: int = 60, ny: int = 40, pitch_mm: float = 0.5):
    """A regular triangulated grid: vertices, faces, per-vertex area, slice index."""
    xs, ys = np.meshgrid(np.arange(nx) * pitch_mm, np.arange(ny) * pitch_mm, indexing="ij")
    verts = np.stack([xs.ravel(), ys.ravel(), np.zeros(nx * ny)], axis=1)
    idx = np.arange(nx * ny).reshape(nx, ny)
    a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    faces = np.concatenate([np.stack([a, b, c], -1).reshape(-1, 3),
                            np.stack([a, c, d], -1).reshape(-1, 3)])
    areas = np.full(nx * ny, pitch_mm * pitch_mm)
    # 3 mm slices along x: six 0.5 mm columns per native slice
    slice_index = (xs.ravel() // 3.0).astype(int)
    return verts, faces, areas, slice_index


def _grade(thickness: np.ndarray, params: GradingParams = GradingParams(), **kw):
    verts, faces, areas, slices = _plate()
    n = len(verts)
    codes = np.array(["ccMF"] * n, dtype=object)
    res_eff = np.full(n, 0.3)
    return grade_plate(
        th=thickness, areas=areas, res_eff=res_eff, codes=codes,
        interface_verts_iso=verts, interface_faces=faces, slice_index=slices,
        points_lps=verts, denuded_patches=kw.get("patches", []),
        subregion_confidence={"ccMF": "high"}, params=params)


def _with_cap(baseline: float, deficit: float, area_mm2: float) -> np.ndarray:
    verts, *_ = _plate()
    n = len(verts)
    th = np.full(n, baseline)
    centre = verts[:, :2].mean(0)
    r = np.sqrt(area_mm2 / np.pi)
    inside = np.linalg.norm(verts[:, :2] - centre, axis=1) <= r
    th[inside] = baseline * (1.0 - deficit)
    return th


def test_no_defect_grades_zero() -> None:
    grades, lesions, summary = _grade(np.full(2400, 2.0))
    assert grades["ccMF"]["grade"] == "0"
    assert lesions == []
    assert summary["baselineMm"] == pytest.approx(2.0, abs=1e-6)
    assert summary["gradeIAssessable"] is False


def test_sixty_percent_cap_is_grade_iii() -> None:
    grades, lesions, _ = _grade(_with_cap(2.0, 0.60, 40.0))
    assert grades["ccMF"]["grade"] == "III"
    assert len(lesions) == 1
    assert lesions[0].area_mm2 == pytest.approx(40.0, rel=0.2)
    assert lesions[0].median_deficit_pct == pytest.approx(60.0, abs=1.0)


def test_thirty_percent_cap_is_grade_ii() -> None:
    grades, lesions, _ = _grade(_with_cap(2.0, 0.30, 40.0))
    assert grades["ccMF"]["grade"] == "II"
    assert lesions[0].grade == "II"


def test_tiny_defect_is_ignored() -> None:
    grades, lesions, _ = _grade(_with_cap(2.0, 0.60, 12.0))
    assert grades["ccMF"]["grade"] == "0"
    assert lesions == []


def test_diffuse_thinning_is_not_a_focal_lesion() -> None:
    """The documented limitation: a plate thin everywhere has no focal deficit."""
    grades, lesions, summary = _grade(np.full(2400, 1.0))
    assert grades["ccMF"]["grade"] == "0"
    assert summary["baselineMm"] == pytest.approx(1.0)


def test_unreliable_plate_is_not_assessed() -> None:
    verts, faces, areas, slices = _plate()
    n = len(verts)
    grades, lesions, summary = grade_plate(
        th=np.full(n, 2.0), areas=areas, res_eff=np.full(n, 3.0), codes=np.array(["ccMF"] * n, dtype=object),
        interface_verts_iso=verts, interface_faces=faces, slice_index=slices, points_lps=verts,
        denuded_patches=[], subregion_confidence={"ccMF": "high"}, params=GradingParams())
    assert grades["ccMF"]["grade"] == "未评估"
    assert "reliable" in summary["reason"]


def test_denuded_patch_is_grade_iv() -> None:
    class Patch:
        area_mm2 = 30.0
        centroid_lps = [1.0, 2.0, 3.0]
        cart_label = 4
        code = "ccMF"
        n_slices = 3
        enclosure = 0.95
    grades, lesions, _ = _grade(np.full(2400, 2.0), patches=[Patch()])
    assert grades["ccMF"]["grade"] == "IV"
    assert lesions[0].grade == "IV" and lesions[0].min_thickness_mm == 0.0


def test_bare_rim_is_not_grade_iv() -> None:
    """Cartilage label stopping short of the bone label leaves a bare strip
    with little cartilage around it. That is a segmentation edge, not a hole."""
    class Patch:
        area_mm2 = 30.0
        centroid_lps = [1.0, 2.0, 3.0]
        cart_label = 4
        code = "ccMF"
        n_slices = 3
        enclosure = 0.4
    grades, lesions, _ = _grade(np.full(2400, 2.0), patches=[Patch()])
    assert grades["ccMF"]["grade"] == "0"
    assert lesions == []


def test_single_slice_lesion_is_not_reported() -> None:
    """One 3 mm slice cannot be told from partial volume at a label edge."""
    verts, faces, areas, slices = _plate()
    n = len(verts)
    th = np.full(n, 2.0)
    col = slices == 3                      # exactly one native slice
    th[col] = 0.5
    grades, lesions, _ = _grade(th)
    assert grades["ccMF"]["grade"] == "0"
    assert lesions == []


def test_plate_margin_is_excluded_from_grading() -> None:
    """The natural taper at the plate edge must never read as a lesion."""
    verts, faces, areas, slices = _plate()
    n = len(verts)
    th = np.full(n, 2.0)
    edge = verts[:, 1] < 2.0                # a 2 mm strip along one edge, tapered
    th[edge] = 0.4
    boundary = verts[:, 1] == 0.0
    codes = np.array(["ccMF"] * n, dtype=object)
    grades, lesions, summary = grade_plate(
        th=th, areas=areas, res_eff=np.full(n, 0.3), codes=codes,
        interface_verts_iso=verts, interface_faces=faces, slice_index=slices,
        points_lps=verts, denuded_patches=[], subregion_confidence={"ccMF": "high"},
        params=GradingParams(), boundary=boundary)
    assert grades["ccMF"]["grade"] == "0", lesions
    assert summary["edgeExcludedAreaFraction"] > 0.05


def test_params_come_from_the_reference_yaml() -> None:
    p = load_grading_params({"outerbridge": {"deficit_ii": 0.3, "min_lesion_area_mm2": 25}})
    assert p.deficit_ii == 0.3 and p.min_lesion_area_mm2 == 25.0
    assert p.deficit_iii == 0.50                                    # untouched default
    assert load_grading_params(None) == GradingParams()


# --------------------------------------------------------------- coverage

def test_hole_in_a_cartilage_shell_is_a_denuded_patch() -> None:
    """Sphere bone R=15, cartilage shell 2 mm, a circular hole ~50 mm2 punched
    through the shell. Coverage must report a bare patch of about that area."""
    from mriviewer.morph.coverage import bone_coverage
    from mriviewer.morph.geometry import iso_fields
    from mriviewer.seg.mesh import _have_vtk
    if not _have_vtk():
        pytest.skip("VTK not available")

    iso = 0.5
    n = 100
    c = (n - 1) / 2 * iso
    g = np.arange(n) * iso
    K, J, I = np.meshgrid(g - c, g - c, g - c, indexing="ij")
    R = np.sqrt(K**2 + J**2 + I**2)
    bone = R <= 15.0
    shell = (R > 15.0) & (R <= 17.0)
    # hole: a cone around the +I axis with base radius ~4 mm on the bone surface
    hole = (I > 0) & (np.sqrt(K**2 + J**2) < 4.0)
    cart = shell & ~hole
    masks = {1: bone, 4: cart}

    class Geom:
        spacing = (iso, iso, iso)
        origin = (0.0, 0.0, 0.0)
        normal = (0.0, 0.0, 1.0); row_cosine = (1.0, 0.0, 0.0); col_cosine = (0.0, 1.0, 0.0)

    class Frame:
        medial_sign = 1.0
        axes = np.eye(3)
        notes: list = []

    fields, grid = iso_fields(masks, Geom(), [1, 4], iso_mm=iso, margin=2)
    cov = bone_coverage(fields, grid, Geom(), Frame(), 1, [4],
                        closing_radius_mm=6.0, min_denuded_area_mm2=10.0, min_denuded_slices=1)
    assert cov is not None
    expected = np.pi * 4.0**2                                     # ~50 mm2
    assert len(cov.denuded) == 1
    assert cov.denuded[0].area_mm2 == pytest.approx(expected, rel=0.35)
    assert cov.dab_mm2 == pytest.approx(cov.denuded[0].area_mm2)
    assert 0.5 < cov.dab_interior_pct < 5.0
    assert cov.denuded[0].enclosure > 0.9              # a hole, surrounded by cartilage
