"""Report figures: the flattened plate rasters and the key-slice PNGs.

Synthetic plates only (no VTK, no DICOM). What is pinned here is that the
picture says what the numbers say: the persisted 2D frame reproduces the
parcellation exactly, a vertex's thickness lands in its own pixel, the
unreliable and denuded pixels are told apart from the measured ones, and a
changed parameter hash lands in a different cache directory.
"""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from mriviewer.morph.figures import (CLS_DENUDED, CLS_FILLED, CLS_MEASURED,
                                     CLS_OUTSIDE, CLS_UNRELIABLE, SCALE_MAX_MM,
                                     build_figures, decode_raster_layer,
                                     lps_to_index, plane_names, rasterize_plate,
                                     render_key_slices, select_key_slices)
from mriviewer.morph.parcellation import (boundaries2d, codes_from_frame2d,
                                          parcellate_femur, parcellate_patella,
                                          parcellate_tibia, to_frame2d)


# ------------------------------------------------------------ synthetic data

def _condyle(n: int = 4000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    theta = rng.uniform(np.deg2rad(20.0), np.deg2rad(200.0), n)
    ml = rng.uniform(-12.0, 12.0, n)
    points = np.stack([ml, 18.0 * np.cos(theta), -18.0 * np.sin(theta)], axis=1)
    return points, np.round(ml / 3.0).astype(int)


def _plateau(n: int = 3000, seed: int = 1) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    ml = rng.uniform(-14.0, 14.0, n)
    ap = rng.uniform(-20.0, 20.0, n)
    points = np.stack([ml, ap, 0.02 * ml ** 2 * 0.0 + rng.normal(0, 0.2, n)], axis=1)
    return points, np.round(ml / 3.0).astype(int)


def _patella(n: int = 2000, seed: int = 2) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    ml = rng.uniform(-15.0, 15.0, n)
    si = rng.uniform(-12.0, 12.0, n)
    return np.stack([ml, 0.0 * ml, si], axis=1), np.round(ml / 3.0).astype(int)


def _grid_plate(nx: int = 40, ny: int = 30, pitch: float = 0.5) -> np.ndarray:
    """A regular patellar-style grid in (ML, AP, SI): one vertex per pixel."""
    xs = (np.arange(nx) + 0.5) * pitch - nx * pitch / 2
    ys = (np.arange(ny) + 0.5) * pitch - ny * pitch / 2
    ml, si = np.meshgrid(xs, ys)
    return np.stack([ml.ravel(), np.zeros(ml.size), si.ravel()], axis=1)


def _arrays(points_af: np.ndarray, th: np.ndarray, *, res_eff=None, lesions=(),
            label: int = 8, name: str = "髌骨软骨") -> SimpleNamespace:
    slice_index = np.round(points_af[:, 0] / 3.0).astype(int)
    if label in (4, 5):
        parc = parcellate_femur(points_af, slice_index, medial=(label == 4))
    elif label in (6, 7):
        parc = parcellate_tibia(points_af, slice_index, medial=(label == 6))
    else:
        parc = parcellate_patella(points_af, slice_index)
    n = len(points_af)
    return SimpleNamespace(
        label=label, name=name, axes=np.eye(3), points_lps=points_af.copy(),
        points_af=points_af, th=th, areas=np.full(n, 0.25),
        res_eff=np.full(n, 0.3) if res_eff is None else res_eff,
        codes=parc.codes, boundary=np.zeros(n, bool), frame2d=parc.frame2d,
        lesions=list(lesions))


# --------------------------------------------------------- frame2d oracle

@pytest.mark.parametrize("kind", ["femur_m", "femur_l", "tibia_m", "tibia_l", "patella"])
def test_persisted_frame_reproduces_the_parcellation(kind: str) -> None:
    """The map is drawn from frame2d; it must be the cut that made the numbers."""
    if kind.startswith("femur"):
        pts, sl = _condyle()
        parc = parcellate_femur(pts, sl, medial=kind.endswith("m"))
    elif kind.startswith("tibia"):
        pts, sl = _plateau()
        parc = parcellate_tibia(pts, sl, medial=kind.endswith("m"))
    else:
        pts, sl = _patella()
        parc = parcellate_patella(pts, sl)
    again = codes_from_frame2d(pts, parc.frame2d)
    assert (again == parc.codes).all()
    assert json.dumps(parc.frame2d)          # JSON-serialisable, nothing numpy left


def test_frame2d_keys_per_kind() -> None:
    pts, sl = _condyle()
    f = parcellate_femur(pts, sl, medial=True).frame2d
    assert f["kind"] == "femur" and set(f["circle"]) == {"cx", "cy", "r"}
    assert f["thetaLo"] < f["trochleaMax"] < f["centralMax"] < f["thetaHi"]
    assert len(f["bandEdges"]) == 4
    pts, sl = _plateau()
    t = parcellate_tibia(pts, sl, medial=False).frame2d
    assert t["kind"] == "tibia" and t["rCut"] > 0 and t["sU"] > 0 and t["sV"] > 0
    pts, sl = _patella()
    assert parcellate_patella(pts, sl).frame2d["kind"] == "patella"


def test_femur_chart_unrolls_along_the_arc() -> None:
    """Arc length grows monotonically with the angle; ML passes through."""
    pts, sl = _condyle()
    parc = parcellate_femur(pts, sl, medial=True)
    xy = to_frame2d(pts, parc.frame2d)
    from mriviewer.morph.parcellation import femur_theta
    theta = femur_theta(pts, parc.frame2d)
    order = np.argsort(theta)
    assert (np.diff(xy[order, 0]) >= -1e-9).all()
    assert np.allclose(xy[:, 1], pts[:, 0])
    # Boundaries fall between the subregions they separate.
    b = boundaries2d(parc.frame2d, (xy[:, 0].min(), xy[:, 0].max(), xy[:, 1].min(), xy[:, 1].max()))
    x_t = b[0]["a"][0]
    assert xy[parc.codes == "MFT", 0].max() <= x_t + 1e-6 <= xy[parc.codes == "ccMF", 0].min() + 1e-6


def test_tibia_boundaries_include_the_ellipse_and_four_rays() -> None:
    pts, sl = _plateau()
    parc = parcellate_tibia(pts, sl, medial=True)
    b = boundaries2d(parc.frame2d, (-15, 15, -20, 20))
    assert [x["type"] for x in b] == ["ellipse", "line", "line", "line", "line"]


# ------------------------------------------------------------- rasteriser

def test_raster_shape_and_pixel_values() -> None:
    pts = _grid_plate(40, 30)
    th = np.full(len(pts), 2.0)
    th[0] = 3.5                                  # one known vertex
    r = rasterize_plate(_arrays(pts, th))
    assert r["schema"].startswith("mriv-plate-raster")
    assert r["width"] >= 40 and r["height"] >= 30
    assert r["scale"] == {"minMm": 0.0, "maxMm": SCALE_MAX_MM}
    thick = decode_raster_layer(r, "thicknessUm")
    cls = decode_raster_layer(r, "cls")
    x0, y1 = r["originMm"]
    ix = int(np.floor((pts[0, 0] - x0) / r["pxMm"]))
    iy = int(np.floor((y1 - pts[0, 2]) / r["pxMm"]))
    assert thick[iy, ix] == 3500
    assert cls[iy, ix] == CLS_MEASURED
    inside = cls == CLS_MEASURED
    assert np.all(thick[inside][thick[inside] != 3500] == 2000)
    assert (cls[0, :] == CLS_OUTSIDE).all()      # padding row


def test_unreliable_and_nan_vertices_are_class_unreliable() -> None:
    pts = _grid_plate(40, 30)
    th = np.full(len(pts), 2.0)
    res = np.full(len(pts), 0.3)
    res[:200] = 3.0                              # first rows sampled along the slice axis
    th[200:250] = np.nan                         # escaped rays
    r = rasterize_plate(_arrays(pts, th, res_eff=res))
    cls = decode_raster_layer(r, "cls")
    x0, y1 = r["originMm"]
    for k in (0, 199, 220):
        ix = int(np.floor((pts[k, 0] - x0) / r["pxMm"]))
        iy = int(np.floor((y1 - pts[k, 2]) / r["pxMm"]))
        assert cls[iy, ix] == CLS_UNRELIABLE
    assert (cls == CLS_MEASURED).sum() > 900


def test_small_holes_are_filled_and_large_ones_are_not() -> None:
    pts = _grid_plate(60, 40)
    keep = np.ones(len(pts), bool)
    ml, si = pts[:, 0], pts[:, 2]
    keep &= ~((np.abs(ml + 8) < 0.6) & (np.abs(si) < 0.6))     # 1 mm hole
    keep &= ~((np.abs(ml - 8) < 2.5) & (np.abs(si) < 2.5))     # 5 mm hole
    pts = pts[keep]
    r = rasterize_plate(_arrays(pts, np.full(len(pts), 1.5)))
    cls = decode_raster_layer(r, "cls")
    thick = decode_raster_layer(r, "thicknessUm")
    x0, y1 = r["originMm"]
    px = r["pxMm"]
    small = cls[int((y1 - 0) / px), int((-8 - x0) / px)]
    big = cls[int((y1 - 0) / px), int((8 - x0) / px)]
    assert small == CLS_FILLED
    assert thick[int((y1 - 0) / px), int((-8 - x0) / px)] == 1500
    assert big in (CLS_OUTSIDE, CLS_UNRELIABLE) and big != CLS_FILLED


def test_lesion_layer_marks_the_lesion_and_denuded_gets_its_own_class() -> None:
    pts = _grid_plate(40, 30)
    th = np.full(len(pts), 2.0)
    sel = (np.abs(pts[:, 0] - 3) < 2) & (np.abs(pts[:, 2] - 2) < 2)
    th[sel] = 0.8
    lesion = SimpleNamespace(code="MP", grade="II", area_mm2=16.0, centroid_lps=[3, 0, 2],
                             points_lps=pts[sel])
    bare = SimpleNamespace(code="LP", grade="IV", area_mm2=9.0, centroid_lps=[-13, 0, 9],
                           points_lps=np.array([[-13.0, 0.0, 9.0], [-12.5, 0.0, 9.5],
                                                [-13.5, 0.0, 8.5], [-13.0, 0.0, 10.0]]))
    pa = _arrays(pts, th, lesions=[lesion, bare])
    # the bare patch sits outside the plate: remove the plate vertices there
    r = rasterize_plate(pa)
    les = decode_raster_layer(r, "lesion")
    assert len(r["lesions"]) == 2
    assert r["lesions"][-1]["grade"] == "IV" and r["lesions"][-1]["kind"] == "denuded"
    x0, y1 = r["originMm"]
    ix = int(np.floor((3 - x0) / r["pxMm"]))
    iy = int(np.floor((y1 - 2) / r["pxMm"]))
    assert les[iy, ix] == r["lesions"][0]["index"]
    assert (les == r["lesions"][-1]["index"]).any()


def test_denuded_class_when_patch_is_outside_the_plate() -> None:
    pts = _grid_plate(40, 30)
    bare_pts = np.array([[30.0, 0.0, 0.0], [30.5, 0.0, 0.0], [30.0, 0.0, 0.5], [31.0, 0.0, 1.0]])
    bare = SimpleNamespace(code="MP", grade="IV", area_mm2=9.0, centroid_lps=[30, 0, 0],
                           points_lps=bare_pts)
    all_pts = np.vstack([pts, bare_pts])         # extend the chart to include the patch
    th = np.full(len(all_pts), 2.0)
    th[-4:] = np.nan
    pa = _arrays(all_pts, th, lesions=[bare])
    pa.res_eff[-4:] = 9.0
    r = rasterize_plate(pa)
    cls = decode_raster_layer(r, "cls")
    assert (cls == CLS_DENUDED).sum() >= 1


def test_subregion_layer_and_labels_follow_the_codes() -> None:
    pts, _ = _patella()
    r = rasterize_plate(_arrays(pts, np.full(len(pts), 2.0)))
    code = decode_raster_layer(r, "code")
    assert r["codes"] == ["MP", "LP"]
    assert {c["code"] for c in r["labels"]} == {"MP", "LP"}
    x0, y1 = r["originMm"]
    # +x medial: the right half of the chart is MP (index 1), the left LP (2)
    cls = decode_raster_layer(r, "cls")
    inside = cls > 0
    right = np.zeros_like(inside); right[:, r["width"] * 3 // 4:] = True
    left = np.zeros_like(inside); left[:, : r["width"] // 4] = True
    assert (code[inside & right] == 1).mean() > 0.95
    assert (code[inside & left] == 2).mean() > 0.95
    assert r["boundaries"][0]["type"] == "line"


def test_layers_roundtrip_little_endian() -> None:
    pts = _grid_plate(10, 8)
    r = rasterize_plate(_arrays(pts, np.full(len(pts), 1.234)))
    raw = base64.b64decode(r["layers"]["thicknessUm"])
    assert len(raw) == 2 * r["width"] * r["height"]
    assert np.frombuffer(raw, "<u2").max() == 1234


# ------------------------------------------------------------- key slices

def _geom(shape_kji=(30, 64, 64)):
    """A sagittal 2D-style geometry: k runs along +L, 3 mm slices."""
    k, j, i = shape_kji
    return SimpleNamespace(
        dims=(i, j, k), spacing=(0.3, 0.3, 3.0), origin=(0.0, 0.0, 0.0),
        direction=(0, 1, 0, 0, 0, -1, 1, 0, 0),
        row_cosine=(0.0, 1.0, 0.0),      # i advances posteriorly
        col_cosine=(0.0, 0.0, -1.0),     # j advances inferiorly
        normal=(1.0, 0.0, 0.0),          # k advances to the patient's left
        plane="sagittal", anisotropy=10.0)


def test_plane_names_and_index_mapping() -> None:
    g = _geom()
    assert plane_names(g) == {0: "sagittal", 1: "axial", 2: "coronal"}
    idx = lps_to_index(np.array([[9.0, 3.0, -6.0]]), g)[0]
    assert np.allclose(idx, [3.0, 20.0, 10.0])


def _volume_and_labels(shape=(30, 64, 64)):
    vol = np.random.default_rng(0).integers(50, 200, size=shape).astype(np.int16)
    lab = np.zeros(shape, np.uint8)
    lab[10:20, 20:30, 20:40] = 1          # "femur"
    lab[10:20, 30:34, 20:40] = 4          # "medial femoral cartilage"
    lab[5:8, 40:50, 10:20] = 8            # "patellar cartilage"
    return vol, lab


def test_select_key_slices_prefers_the_worst_lesion_then_ccmf() -> None:
    g = _geom()
    _, lab = _volume_and_labels()
    pts = np.array([[45.0, 9.0, -9.5], [45.0, 9.5, -9.5]])
    pa4 = SimpleNamespace(label=4, name="MFC", codes=np.array(["ccMF", "ccMF"], dtype=object),
                          areas=np.array([1.0, 1.0]), points_lps=pts,
                          lesions=[SimpleNamespace(code="ccMF", grade="III", area_mm2=40.0,
                                                   centroid_lps=[30.0, 6.0, -3.0])])
    pa8 = SimpleNamespace(label=8, name="PC", codes=np.array(["MP"], dtype=object),
                          areas=np.array([1.0]), points_lps=np.array([[18.0, 13.5, -4.5]]),
                          lesions=[])
    chosen = select_key_slices({4: pa4, 8: pa8}, lab, g)
    assert chosen["sagittal"]["index"] == 10 and "III" in chosen["sagittal"]["why"]
    assert chosen["coronal"]["index"] == 20
    assert chosen["axial"]["index"] == 15 and "髌" in chosen["axial"]["why"]
    pa4.lesions = []
    chosen = select_key_slices({4: pa4}, lab, g)
    assert chosen["sagittal"]["index"] == 15 and "ccMF" in chosen["sagittal"]["why"]


def test_render_key_slices_squares_pixels_and_paints_contours() -> None:
    from PIL import Image
    g = _geom()
    vol, lab = _volume_and_labels()
    chosen = {"sagittal": {"axis": 0, "index": 15, "why": "t"},
              "coronal": {"axis": 2, "index": 30, "why": "t"},
              "axial": {"axis": 1, "index": 32, "why": "t"}}
    out = render_key_slices(vol, lab, g, {1: "#ff0000", 4: "#00ff00", 8: "#0000ff"}, chosen,
                            margin_mm=3.0)
    assert set(out) == {"sagittal", "coronal", "axial"}
    for plane, (png, meta) in out.items():
        img = Image.open(io.BytesIO(png))
        assert img.size == (meta["widthPx"], meta["heightPx"])
        assert meta["mmPerPx"] == pytest.approx(0.3)
        arr = np.asarray(img.convert("RGB"))
        assert (arr == [0, 255, 0]).all(axis=2).sum() > 10, plane      # cartilage contour present
    # the coronal reformat is the k-axis stretched 10x: taller than a native slice
    assert out["coronal"][1]["nativeSliceMm"] == 0.3
    assert out["sagittal"][1]["labels"] == [1, 4]


# --------------------------------------------------------------- manifest

def test_build_figures_writes_into_a_hash_named_directory(tmp_path) -> None:
    from mriviewer.volume.cache import VolumeCache
    cache = VolumeCache(tmp_path)
    pts = _grid_plate(20, 16)
    pa = _arrays(pts, np.full(len(pts), 2.0))
    d1 = cache.fig_dir("abcdef", "morph-9.9.9", "hash1")
    m1 = build_figures(d1, 7, "morph-9.9.9", "hash1", {8: pa}, vol=None, lab=None,
                       geom=_geom(), label_colors={})
    assert (d1 / "plate_8.json").is_file()
    assert m1["plates"]["8"]["url"] == "/api/v1/segmentations/7/figures/plate_8.json?v=hash1"
    assert set(m1["files"]) == {"plate_8.json"}
    assert m1["slices"] == {} and any("skipped" in n for n in m1["notes"])
    assert d1.parent.name == "abcdef" and d1.parent.parent.name == "ab"
    d2 = cache.fig_dir("abcdef", "morph-9.9.9", "hash2")
    build_figures(d2, 7, "morph-9.9.9", "hash2", {8: pa}, vol=None, lab=None,
                  geom=_geom(), label_colors={})
    assert d1.is_dir() and d2.is_dir() and d1 != d2
    raster = json.loads((d1 / "plate_8.json").read_text(encoding="utf-8"))
    assert raster["label"] == 8 and raster["kind"] == "patella"


def test_build_figures_with_volume_writes_three_slices(tmp_path) -> None:
    vol, lab = _volume_and_labels()
    pts = np.array([[45.0, 9.0, -9.5], [45.0, 9.5, -9.5], [45.5, 9.0, -9.0]])
    pa = _arrays(pts, np.array([2.0, 2.0, 2.0]), label=4, name="MFC")
    m = build_figures(tmp_path / "fig", 7, "a", "h", {4: pa}, vol=vol, lab=lab,
                      geom=_geom(), label_colors={4: "#00ff00"})
    assert set(m["slices"]) == {"sagittal", "coronal", "axial"}
    for plane in m["slices"]:
        assert (tmp_path / "fig" / ("slice_%s.png" % plane)).stat().st_size > 100
    assert m["error"] is None
