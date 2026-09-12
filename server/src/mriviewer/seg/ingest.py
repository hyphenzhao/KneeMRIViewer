"""Materialize a segmentation into the cache and compute its per-label stats.

The stats panel is what lets a user work out what an unnamed label actually is:
volume, bounding box, centroid, slice range and mean image intensity are enough
to tell a femur from a patella without a radiologist.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

from ..dicomio.probe import is_dicom_file, probe
from ..dicomio.series import geometry_from_headers, sort_headers
from ..labels import ensure_labelset_for_values
from ..volume.cache import VolumeCache, hash_inputs
from .nifti_io import SegmentationMismatch, load_segmentation, merge_binary_masks


def series_geometry(conn: sqlite3.Connection, series_id: int):
    """Re-derive the geometry of a series from its files."""
    row = conn.execute(
        "SELECT s.path_rel, d.root_path FROM series s JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id JOIN dataset d ON d.id=p.dataset_id"
        " WHERE s.id=?", (series_id,)).fetchone()
    if row is None:
        raise KeyError("series %d" % series_id)
    abs_path = Path(row["root_path"]) / row["path_rel"]
    if abs_path.is_file():
        import nibabel as nib
        from ..scan.runner import geometry_from_nifti
        img = nib.load(str(abs_path))
        return geometry_from_nifti(img.affine, img.shape[:3]), abs_path
    from ..scan.runner import is_junk
    files = [p for p in sorted(abs_path.iterdir())
             if p.is_file() and not is_junk(p.name) and is_dicom_file(p)]
    headers = [h for h in (probe(f) for f in files) if h and h.is_image]
    if not headers:
        raise ValueError("no image instances under %s" % abs_path)
    ordered, _ = sort_headers(headers)
    return geometry_from_headers(ordered), abs_path


def label_stats(canonical: np.ndarray, geom, image: np.ndarray | None = None
                ) -> dict[str, Any]:
    """Per-label geometry and (optionally) intensity summary, in patient mm."""
    row_cos = np.asarray(geom.col_cosine, float)     # advances with row index j
    col_cos = np.asarray(geom.row_cosine, float)     # advances with column index i
    normal = np.asarray(geom.normal, float)
    col_sp, row_sp, sl_sp = geom.spacing
    origin = np.asarray(geom.origin, float)
    vox_mm3 = float(col_sp * row_sp * sl_sp)

    out: list[dict[str, Any]] = []
    for value in sorted(int(v) for v in np.unique(canonical) if v != 0):
        idx = np.argwhere(canonical == value)          # (n, 3) as (k, j, i)
        if not len(idx):
            continue
        world = (origin
                 + idx[:, 1:2] * (row_cos * row_sp)
                 + idx[:, 2:3] * (col_cos * col_sp)
                 + idx[:, 0:1] * (normal * sl_sp))
        centroid = world.mean(0)
        lo, hi = world.min(0), world.max(0)
        rec: dict[str, Any] = {
            "value": value,
            "voxels": int(len(idx)),
            "volumeMm3": round(len(idx) * vox_mm3, 1),
            "volumeCm3": round(len(idx) * vox_mm3 / 1000.0, 3),
            "centroidLps": [round(float(x), 2) for x in centroid],
            "extentMm": [round(float(x), 1) for x in (hi - lo)],
            "sliceRange": [int(idx[:, 0].min()), int(idx[:, 0].max())],
        }
        if image is not None and image.shape == canonical.shape:
            vals = image[canonical == value].astype(np.float32)
            rec["intensity"] = {
                "mean": round(float(vals.mean()), 1),
                "median": round(float(np.median(vals)), 1),
                "p10": round(float(np.percentile(vals, 10)), 1),
                "p90": round(float(np.percentile(vals, 90)), 1),
            }
        out.append(rec)
    return {"labels": out, "voxelVolumeMm3": round(vox_mm3, 6)}


def materialize_segmentation(conn: sqlite3.Connection, cfg: Any, seg_id: int,
                             *, force: bool = False,
                             with_intensity: bool = True) -> dict[str, Any]:
    row = conn.execute(
        "SELECT sg.*, d.root_path, d.key AS ds_key, s.volume_key"
        " FROM segmentation sg JOIN dataset d ON d.id=sg.dataset_id"
        " LEFT JOIN series s ON s.id=sg.series_id WHERE sg.id=?", (seg_id,)).fetchone()
    if row is None:
        raise KeyError("segmentation %d" % seg_id)
    if row["series_id"] is None:
        raise ValueError("segmentation %d is not associated with a series" % seg_id)

    cache = VolumeCache(cfg.cache_dir)
    geom, _ = series_geometry(conn, int(row["series_id"]))

    # Segmentations live under one of three roots. Predictions are stored with
    # an absolute path because their root is configurable and outside any dataset.
    src_rel = Path(row["source_path_rel"])
    if src_rel.is_absolute():
        src = src_rel
    elif row["source_root"] == "derived":
        src = Path(cfg.derived_dir) / src_rel
    else:
        src = Path(row["root_path"]) / src_rel

    key = hash_inputs(row["ds_key"], row["source_path_rel"],
                      src.stat().st_size if src.exists() else 0,
                      row["volume_key"] or "")
    entry = cache.segmentation(key)
    if entry.exists and not force and row["seg_state"] == "ready":
        return entry.read_meta()

    try:
        if row["kind"] == "nifti_binary_set":
            comps = json.loads(row["present_values_json"] or "[]")
            paths = _component_paths(src, comps)
            canonical, tr, _ = merge_binary_masks(paths, geom)
            names = {v: p.name.replace(".nii.gz", "") for v, p in paths.items()}
            ls_id = ensure_labelset_for_values(
                conn, "auto_" + row["ds_key"] + "_" + str(seg_id),
                row["display_name"] or "自动标签集", list(paths))
            for v, nm in names.items():
                conn.execute(
                    "UPDATE label_def SET name=? WHERE label_set_id=? AND value=? AND name LIKE 'Label %'",
                    (nm.replace("_", " "), ls_id, v))
            conn.execute("UPDATE segmentation SET label_set_id=? WHERE id=?", (ls_id, seg_id))
        else:
            canonical, tr = load_segmentation(src, geom)
    except SegmentationMismatch as exc:
        conn.execute("UPDATE segmentation SET seg_state='failed', seg_error=? WHERE id=?",
                     (str(exc), seg_id))
        conn.commit()
        raise

    image = None
    if with_intensity and row["volume_key"]:
        vol_entry = cache.volume(row["volume_key"])
        if vol_entry.exists:
            vmeta = vol_entry.read_meta()
            d = vmeta["dimensions"]
            image = vol_entry.read_array((d[2], d[1], d[0]), vmeta["numpyDtype"])

    stats = label_stats(canonical, geom, image)
    present = [int(v) for v in np.unique(canonical) if v != 0]
    meta = {
        "segmentationId": "mriv:seg:" + key,
        "dimensions": list(geom.dims),
        "spacing": list(geom.spacing),
        "origin": list(geom.origin),
        "direction": list(geom.direction),
        "dataType": "Uint8Array",
        "numpyDtype": "uint8",
        "presentValues": present,
        "stats": stats,
    }
    entry.write(canonical, meta)
    meta = entry.read_meta()

    conn.execute(
        "UPDATE segmentation SET seg_state='ready', seg_key=?, seg_error=NULL,"
        " ingest_transform_json=?, dims_json=?, affine_json=?, present_values_json=?,"
        " stats_json=? WHERE id=?",
        (key, tr.to_json(), json.dumps(list(geom.dims)),
         json.dumps(tr.nifti_affine), json.dumps(present),
         json.dumps(stats, ensure_ascii=False), seg_id))
    conn.commit()
    return meta


def _component_paths(base: Path, values: list[int]) -> dict[int, Path]:
    """Rebuild the value -> file mapping for a merged binary-mask set."""
    files: list[Path] = []
    for sub in sorted(p for p in base.iterdir() if p.is_dir()):
        files.extend(sorted(sub.glob("*.nii.gz")))
    names = sorted(files, key=lambda p: p.name)
    return {i + 1: p for i, p in enumerate(names)}
