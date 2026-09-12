"""Figures for the illustrated report, drawn from the vertices that were measured.

Three kinds, all produced in the same process and at the same moment as the
numbers, so a figure can never show a different measurement than the table
beside it:

* **Plate rasters** - each cartilage plate flattened into the 2D chart its
  parcellation was defined in (``parcellation.to_frame2d``): the femoral
  condyle unrolled along its fitted circle, the tibial plateau seen from
  above, the patella from its articular side. Thickness is area-weighted into
  0.5 mm pixels; the subregion, reliability and lesion membership of every
  pixel ride along as separate layers. This is the Chondrometrics / OAI
  "thickness map" convention. The raster is data, not a picture: the browser
  paints it, so colours and text follow the report's palette and print well.
* **Key slices** - the three canonical planes through the most informative
  point (the worst lesion, else the central medial condyle), with the label
  contours drawn in the label set's colours. Plain PNGs; the browser adds the
  scale bar and captions.
* **The manifest** - what was written, where, and why each slice was chosen.

Nothing here needs a display, a font, or matplotlib: numpy, scipy.ndimage
and Pillow only, all already in the offline bundle.
"""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from .outerbridge import GRADE_RANK
from .parcellation import SUBREGION_NAMES, boundaries2d, to_frame2d
from .probe import RELIABLE_RES_MM

FIGURES_VERSION = 1
SCHEMA = "mriv-plate-raster/1"
PX_MM = 0.5                 # raster pitch; the iso grid the surfaces came from
SCALE_MAX_MM = 4.0          # one colour scale for every plate, 0-4 mm
FILL_RADIUS_PX = 2          # holes up to 1 mm between vertex rows are bridged
SLICE_MARGIN_MM = 15.0      # crop margin around the labels on a key slice

# Pixel classes of a plate raster.
CLS_OUTSIDE, CLS_MEASURED, CLS_UNRELIABLE, CLS_FILLED, CLS_DENUDED = 0, 1, 2, 3, 4

AXES_ZH = {
    "femur": {"x": {"labelZh": "滑车 → 后髁", "unit": "mm（弧长）"},
              "y": {"labelZh": "外侧 ↔ 内侧", "unit": "mm"}},
    "tibia": {"x": {"labelZh": "外侧 ↔ 内侧", "unit": "mm"},
              "y": {"labelZh": "后 ↔ 前", "unit": "mm"}},
    "patella": {"x": {"labelZh": "外侧 ↔ 内侧", "unit": "mm"},
                "y": {"labelZh": "下 ↔ 上", "unit": "mm"}},
}

PLANE_OF_AXIS = {0: "sagittal", 1: "coronal", 2: "axial"}   # dominant LPS component


def figure_params() -> dict[str, Any]:
    """The figure settings that go into the morphometry params hash."""
    return {"pxMm": PX_MM, "scaleMaxMm": SCALE_MAX_MM, "fillRadiusPx": FILL_RADIUS_PX,
            "sliceMarginMm": SLICE_MARGIN_MM, "schema": FIGURES_VERSION}


# ------------------------------------------------------------ plate rasters

def _b64(arr: np.ndarray, dtype: str) -> str:
    return base64.b64encode(np.ascontiguousarray(arr.astype(dtype)).tobytes()).decode("ascii")


def rasterize_plate(pa: Any, px_mm: float = PX_MM) -> dict[str, Any]:
    """Flatten one plate into its parcellation chart.

    ``pa`` is a ``compute.PlateArrays``. Row 0 is the top of the chart (max
    y), so the browser can blit rows straight onto a canvas, and every
    overlay coordinate in the result is in pixel units of that canvas.
    """
    xy = to_frame2d(pa.points_af, pa.frame2d)
    # Robust extent: a few stray vertices on the notch must not stretch the
    # chart. Lesions are never stray - a denuded patch lies off the plate by
    # definition - so their points widen the extent explicitly.
    lo = np.percentile(xy, 0.5, axis=0)
    hi = np.percentile(xy, 99.5, axis=0)
    lesions_sorted = sorted(pa.lesions, key=lambda l: (GRADE_RANK.get(l.grade, 0), l.area_mm2))
    lesion_xy = []
    for les in lesions_sorted:
        pts = getattr(les, "points_lps", None)
        if pts is None or len(pts) == 0:
            lesion_xy.append(None)
            continue
        lxy = to_frame2d(np.asarray(pts, float) @ pa.axes.T, pa.frame2d)
        lesion_xy.append(lxy)
        lo = np.minimum(lo, lxy.min(axis=0))
        hi = np.maximum(hi, lxy.max(axis=0))
    pad = 2.0 * px_mm
    x0, y0 = float(lo[0] - pad), float(lo[1] - pad)
    x1, y1 = float(hi[0] + pad), float(hi[1] + pad)
    w = max(int(np.ceil((x1 - x0) / px_mm)), 1)
    h = max(int(np.ceil((y1 - y0) / px_mm)), 1)

    ix = np.floor((xy[:, 0] - x0) / px_mm).astype(int)
    iy = np.floor((y1 - xy[:, 1]) / px_mm).astype(int)
    inside = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
    flat = iy * w + ix

    th = np.asarray(pa.th, float)
    areas = np.asarray(pa.areas, float)
    finite = np.isfinite(th)
    reliable = finite & (np.asarray(pa.res_eff, float) <= RELIABLE_RES_MM)

    def acc(mask: np.ndarray, weights: np.ndarray) -> np.ndarray:
        out = np.zeros(h * w, float)
        np.add.at(out, flat[mask], weights[mask])
        return out.reshape(h, w)

    num = acc(inside & reliable, np.where(reliable, th, 0.0) * areas)
    den = acc(inside & reliable, areas)
    cov = acc(inside, areas)
    unrel = acc(inside & ~reliable, areas)

    measured = den > 0
    thickness = np.where(measured, num / np.maximum(den, 1e-12), 0.0)
    # The plate silhouette. Closing bridges the 1-px cracks between vertex
    # rows; the union keeps the border pixels closing would erode away.
    plate = (cov > 0) | ndimage.binary_closing(cov > 0, iterations=2)

    # Fill small gaps - pixels no vertex fell in at all - from the nearest
    # measured pixel, and say so. A pixel that has vertices but no reliable
    # one is not a gap; it stays "unreliable" below.
    edt, (ri, ci) = ndimage.distance_transform_edt(~measured, return_indices=True)
    fill = plate & (cov == 0) & (edt <= FILL_RADIUS_PX)
    thickness[fill] = thickness[ri[fill], ci[fill]]

    cls = np.zeros((h, w), np.uint8)
    cls[plate] = CLS_UNRELIABLE
    cls[measured] = CLS_MEASURED
    cls[measured & (unrel > 0.5 * cov)] = CLS_UNRELIABLE
    cls[fill] = CLS_FILLED

    # Subregion of each pixel: the code with most interface area in it; plate
    # pixels no vertex fell in take the nearest vertex's code.
    codes = [c for c in SUBREGION_NAMES if c in set(pa.codes.tolist())]
    area_by_code = np.stack([acc(inside & (pa.codes == c), areas) for c in codes], axis=0) \
        if codes else np.zeros((0, h, w))
    code_px = np.zeros((h, w), np.uint8)
    if codes:
        code_px = (area_by_code.argmax(axis=0) + 1).astype(np.uint8)
        has_code = area_by_code.sum(axis=0) > 0
        _, (rj, cj) = ndimage.distance_transform_edt(~has_code, return_indices=True)
        code_px = np.where(has_code, code_px, code_px[rj, cj])
        code_px[~plate] = 0

    # Lesions, worst drawn last so it wins where they overlap.
    lesion_px = np.zeros((h, w), np.uint8)
    lesion_meta = []
    for idx, (les, lxy) in enumerate(zip(lesions_sorted, lesion_xy), start=1):
        if lxy is None:
            continue
        lx = np.floor((lxy[:, 0] - x0) / px_mm).astype(int)
        ly = np.floor((y1 - lxy[:, 1]) / px_mm).astype(int)
        ok = (lx >= 0) & (lx < w) & (ly >= 0) & (ly < h)
        mark = np.zeros((h, w), bool)
        mark[ly[ok], lx[ok]] = True
        mark |= ndimage.binary_closing(mark, iterations=1)
        lesion_px[mark] = idx
        if les.grade == "IV":
            # Bare bone: wherever the patch lies and no thickness was
            # measured, the pixel is denuded - its own class, not "unreliable".
            cls[mark & ~measured & ~fill] = CLS_DENUDED
        lesion_meta.append({"index": idx, "code": les.code, "grade": les.grade,
                            "areaMm2": round(float(les.area_mm2), 1),
                            "kind": "denuded" if les.grade == "IV" else "deficit"})

    labels = []
    for k, c in enumerate(codes):
        a = area_by_code[k]
        tot = float(a.sum())
        if tot <= 0:
            continue
        ys, xs = np.mgrid[0:h, 0:w]
        labels.append({"code": c, "labelZh": SUBREGION_NAMES[c][1],
                       "px": [round(float((a * xs).sum() / tot) + 0.5, 1),
                              round(float((a * ys).sum() / tot) + 0.5, 1)]})

    def to_px(p: list[float]) -> list[float]:
        return [round((p[0] - x0) / px_mm, 2), round((y1 - p[1]) / px_mm, 2)]

    boundaries = []
    for b in boundaries2d(pa.frame2d, (x0, x1, y0, y1)):
        if b["type"] == "line":
            boundaries.append({"type": "line", "a": to_px(b["a"]), "b": to_px(b["b"])})
        elif b["type"] == "ellipse":
            boundaries.append({"type": "ellipse", "c": to_px(b["c"]),
                               "r": [round(b["r"][0] / px_mm, 2), round(b["r"][1] / px_mm, 2)]})

    total_area = float(areas.sum()) or 1.0
    return {
        "schema": SCHEMA, "label": int(pa.label), "name": pa.name,
        "kind": pa.frame2d.get("kind"), "medial": pa.frame2d.get("medial"),
        "pxMm": px_mm, "width": w, "height": h,
        "originMm": [round(x0, 3), round(y1, 3)],
        "axes": AXES_ZH.get(pa.frame2d.get("kind"), {}),
        "frame2d": pa.frame2d,
        "layers": {
            "thicknessUm": _b64(np.clip(np.round(thickness * 1000.0), 0, 65535), "<u2"),
            "cls": _b64(cls, "u1"),
            "code": _b64(code_px, "u1"),
            "lesion": _b64(lesion_px, "u1"),
        },
        "codes": codes,
        "labels": labels,
        "boundaries": boundaries,
        "lesions": lesion_meta,
        "scale": {"minMm": 0.0, "maxMm": SCALE_MAX_MM},
        "stats": {
            "reliableAreaFraction": round(float(areas[reliable].sum() / total_area), 3),
            "meanMm": (round(float((th[reliable] * areas[reliable]).sum()
                                   / max(float(areas[reliable].sum()), 1e-9)), 3)
                       if reliable.any() else None),
            "measuredPixels": int(measured.sum()), "platePixels": int(plate.sum()),
        },
    }


def decode_raster_layer(raster: dict[str, Any], name: str) -> np.ndarray:
    """Inverse of the base64 layers, for tests and offline tooling."""
    dtype = "<u2" if name == "thicknessUm" else "u1"
    buf = base64.b64decode(raster["layers"][name])
    return np.frombuffer(buf, dtype=dtype).reshape(raster["height"], raster["width"])


# --------------------------------------------------------------- key slices

def lps_to_index(points_lps: np.ndarray, geom: Any) -> np.ndarray:
    """LPS mm -> fractional canonical (k, j, i) voxel indices."""
    d = np.atleast_2d(np.asarray(points_lps, float)) - np.asarray(geom.origin, float)
    col_sp, row_sp, sl_sp = geom.spacing
    k = d @ np.asarray(geom.normal, float) / sl_sp
    j = d @ np.asarray(geom.col_cosine, float) / row_sp
    i = d @ np.asarray(geom.row_cosine, float) / col_sp
    return np.stack([k, j, i], axis=1)


def axis_directions(geom: Any) -> list[np.ndarray]:
    """Unit LPS direction of canonical axes k, j, i."""
    return [np.asarray(geom.normal, float), np.asarray(geom.col_cosine, float),
            np.asarray(geom.row_cosine, float)]


def plane_names(geom: Any) -> dict[int, str]:
    """Which anatomical plane each canonical axis cuts, made unique."""
    names: dict[int, str] = {}
    taken: set[str] = set()
    for axis, d in enumerate(axis_directions(geom)):
        for comp in np.argsort(-np.abs(d)):
            name = PLANE_OF_AXIS[int(comp)]
            if name not in taken:
                names[axis] = name
                taken.add(name)
                break
    return names


def select_key_slices(arrays: dict[int, Any], lab: np.ndarray, geom: Any
                      ) -> dict[str, dict[str, Any]]:
    """Pick one index per canonical axis, and say why.

    The focus is the worst lesion (IV > III > II, then area); with none, the
    centre of the central medial condyle - the region that degenerates first
    and the one a reader looks at first; failing that, the medial femoral
    plate itself. The axis that cuts axially is instead put through the
    patellar cartilage, because that is where the patellofemoral joint reads.
    """
    focus, why = None, ""
    worst = None
    for pa in arrays.values():
        for les in pa.lesions:
            key = (GRADE_RANK.get(les.grade, 0), les.area_mm2)
            if worst is None or key > worst[0]:
                worst = (key, les)
    if worst is not None:
        les = worst[1]
        focus = np.asarray(les.centroid_lps, float)
        why = "病灶 %s %s 级中心" % (les.code or "?", les.grade)
    elif 4 in arrays:
        pa = arrays[4]
        sel = pa.codes == "ccMF"
        if sel.any():
            wts = pa.areas[sel]
            focus = (pa.points_lps[sel] * wts[:, None]).sum(0) / wts.sum()
            why = "股骨内侧髁中央负重区中心（ccMF）"
        else:
            focus = pa.points_lps.mean(0)
            why = "股骨内侧软骨中心"
    else:
        first = next(iter(arrays.values()), None)
        if first is None:
            return {}
        focus = first.points_lps.mean(0)
        why = "%s中心" % first.name

    patella = None
    if 8 in arrays:
        patella = arrays[8].points_lps.mean(0)

    shape = lab.shape
    names = plane_names(geom)
    out: dict[str, dict[str, Any]] = {}
    for axis in range(3):
        plane = names[axis]
        point, reason = focus, why
        if plane == "axial" and patella is not None:
            point, reason = patella, "髌骨软骨中心（髌股关节）"
        idx = int(np.clip(np.round(lps_to_index(point, geom)[0, axis]), 0, shape[axis] - 1))
        out[plane] = {"axis": axis, "index": idx, "why": reason}
    return out


_TARGETS = {   # image x (right) and y (down) in LPS for each plane
    "sagittal": (np.array([0.0, 1.0, 0.0]), np.array([0.0, 0.0, -1.0])),   # anterior left, superior up
    "coronal": (np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, -1.0])),    # patient's left on the right
    "axial": (np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])),       # anterior up
}


def _hex_rgb(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _take(arr: np.ndarray, axis: int, index: int) -> np.ndarray:
    return np.take(arr, index, axis=axis)


def render_key_slices(vol: np.ndarray, lab: np.ndarray, geom: Any,
                      label_colors: dict[int, str], slices: dict[str, dict[str, Any]],
                      margin_mm: float = SLICE_MARGIN_MM) -> dict[str, tuple[bytes, dict[str, Any]]]:
    """Three PNGs with label contours, in conventional orientation.

    Pixels are squared by integer repetition (3.0 / 0.29 mm is ~10x on this
    data), so the reformats look blocky rather than smoothly interpolated:
    that is honest about what a 3 mm slice knows.
    """
    from PIL import Image
    from ..volume.build import window_from_percentiles

    wc, ww = window_from_percentiles(vol)
    lo = wc - ww / 2.0
    dirs = axis_directions(geom)
    col_sp, row_sp, sl_sp = geom.spacing
    spacing_kji = (sl_sp, row_sp, col_sp)

    out: dict[str, tuple[bytes, dict[str, Any]]] = {}
    for plane, sel in slices.items():
        axis, index = int(sel["axis"]), int(sel["index"])
        img = _take(vol, axis, index).astype(np.float32)
        lbl = _take(lab, axis, index)
        kept = [a for a in range(3) if a != axis]          # (rows, cols) axes
        r_dir, c_dir = dirs[kept[0]], dirs[kept[1]]
        r_sp, c_sp = spacing_kji[kept[0]], spacing_kji[kept[1]]

        xt, yt = _TARGETS[plane]
        if abs(c_dir @ xt) < abs(r_dir @ xt):             # crossed: transpose
            img, lbl = img.T, lbl.T
            r_dir, c_dir, r_sp, c_sp = c_dir, r_dir, c_sp, r_sp
        if c_dir @ xt < 0:
            img, lbl = img[:, ::-1], lbl[:, ::-1]
        if r_dir @ yt < 0:
            img, lbl = img[::-1, :], lbl[::-1, :]

        fine = min(r_sp, c_sp)
        fr, fc = max(int(round(r_sp / fine)), 1), max(int(round(c_sp / fine)), 1)
        img = np.repeat(np.repeat(img, fr, axis=0), fc, axis=1)
        lbl = np.repeat(np.repeat(lbl, fr, axis=0), fc, axis=1)

        grey = np.clip((img - lo) / max(ww, 1e-6), 0.0, 1.0)
        rgb = np.repeat((grey * 255.0).astype(np.uint8)[:, :, None], 3, axis=2)
        present = [int(v) for v in np.unique(lbl) if v != 0]
        for v in present:
            m = lbl == v
            edge = m & ~ndimage.binary_erosion(m)
            edge = ndimage.binary_dilation(edge)
            rgb[edge] = _hex_rgb(label_colors.get(v, "#ffffff"))

        margin_px = int(round(margin_mm / fine))
        if present:
            ys, xs = np.nonzero(lbl > 0)
            y_lo, y_hi = max(ys.min() - margin_px, 0), min(ys.max() + margin_px + 1, rgb.shape[0])
            x_lo, x_hi = max(xs.min() - margin_px, 0), min(xs.max() + margin_px + 1, rgb.shape[1])
            rgb = rgb[y_lo:y_hi, x_lo:x_hi]

        buf = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buf, "PNG", optimize=True)
        out[plane] = (buf.getvalue(), {
            "axis": axis, "index": index, "why": sel.get("why", ""),
            "widthPx": int(rgb.shape[1]), "heightPx": int(rgb.shape[0]),
            "mmPerPx": round(float(fine), 4), "labels": present,
            "nativeSliceMm": round(float(spacing_kji[axis]), 3),
        })
    return out


# ----------------------------------------------------------------- manifest

def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def figure_url(seg_id: int, name: str, p_hash: str) -> str:
    return "/api/v1/segmentations/%d/figures/%s?v=%s" % (seg_id, name, p_hash)


def build_figures(fig_dir: Path, seg_id: int, algo: str, p_hash: str,
                  arrays: dict[int, Any], *, vol: np.ndarray | None, lab: np.ndarray | None,
                  geom: Any, label_colors: dict[int, str]) -> dict[str, Any]:
    """Write every figure for one morphometry row and return the manifest.

    Plate rasters never depend on the intensity volume; key slices do, and
    are simply absent (with a note) when it is not cached. A failure in one
    figure is recorded in the manifest and does not lose the others.
    """
    fig_dir = Path(fig_dir)
    manifest: dict[str, Any] = {
        "version": FIGURES_VERSION, "algoVersion": algo, "paramsHash": p_hash,
        "plates": {}, "slices": {}, "scale": {"minMm": 0.0, "maxMm": SCALE_MAX_MM},
        "files": {}, "notes": [], "error": None,
    }
    for label in sorted(arrays):
        pa = arrays[label]
        try:
            raster = rasterize_plate(pa)
        except Exception as exc:                  # noqa: BLE001 - one plate, not all
            manifest["notes"].append("plate %d raster failed: %s" % (label, exc))
            continue
        name = "plate_%d.json" % label
        data = json.dumps(raster, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _write_atomic(fig_dir / name, data)
        manifest["plates"][str(label)] = {
            "name": name, "url": figure_url(seg_id, name, p_hash), "bytes": len(data),
            "kind": raster["kind"], "width": raster["width"], "height": raster["height"],
            "pxMm": raster["pxMm"], "stats": raster["stats"], "lesions": raster["lesions"],
        }
        manifest["files"][name] = len(data)

    if vol is None or lab is None:
        manifest["notes"].append("intensity volume not cached; key slices skipped")
    else:
        try:
            chosen = select_key_slices(arrays, lab, geom)
            rendered = render_key_slices(vol, lab, geom, label_colors, chosen)
        except Exception as exc:                  # noqa: BLE001
            manifest["notes"].append("key slices failed: %s" % exc)
            rendered = {}
        for plane, (png, meta) in rendered.items():
            name = "slice_%s.png" % plane
            _write_atomic(fig_dir / name, png)
            manifest["slices"][plane] = dict(meta, name=name,
                                             url=figure_url(seg_id, name, p_hash))
            manifest["files"][name] = len(png)
    return manifest
