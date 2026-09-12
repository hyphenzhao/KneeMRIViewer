"""Wiring: cached segmentation -> metric tree -> database row.

Separate from ``compute.py`` so the maths stays runnable on a bare pair of
NIfTI files during development, with no database in the picture.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..volume.cache import VolumeCache
from .compute import (ALGO_VERSION, MIN_RELIABLE_AREA_FRACTION,
                      MIN_SLICES_FOR_CONFIDENCE, RAY_NN_RATIO_RANGE,
                      compute_morphometry)
from .parcellation import (CENTRAL_SPAN_FRACTION, PATELLA_FACET_SHIFT,
                           TIBIAL_CENTRAL_AREA_FRACTION,
                           TROCHLEA_SPAN_FRACTION)
from .figures import build_figures, figure_params
from .outerbridge import GradingParams, load_grading_params
from .probe import RELIABLE_RES_MM, TAU_BONE_MM


def current_params(grading: GradingParams | None = None) -> dict[str, Any]:
    """Every threshold that changes a number, in one place.

    Hashed into the cache key so that adjusting any of them yields a new row
    rather than silently returning last week's figures. Grading thresholds
    come from the reference YAML and are part of the key for the same reason.
    """
    return {
        "outerbridge": (grading or GradingParams()).to_json(),
        "reliableResMm": RELIABLE_RES_MM,
        "tauBoneMm": TAU_BONE_MM,
        "trochleaSpanFraction": TROCHLEA_SPAN_FRACTION,
        "centralSpanFraction": CENTRAL_SPAN_FRACTION,
        "tibialCentralAreaFraction": TIBIAL_CENTRAL_AREA_FRACTION,
        "patellaFacetShift": PATELLA_FACET_SHIFT,
        "minSlicesForConfidence": MIN_SLICES_FOR_CONFIDENCE,
        "minReliableAreaFraction": MIN_RELIABLE_AREA_FRACTION,
        "rayNnRatioRange": list(RAY_NN_RATIO_RANGE),
        # Figures are drawn from the same vertices; how they are rasterised
        # is part of the key so a changed pitch or scale redraws them.
        "figures": figure_params(),
    }


def params_hash(params: dict[str, Any]) -> str:
    blob = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def grading_params(cfg: Any) -> GradingParams:
    """Thresholds from the reference YAML (the file doctors edit)."""
    try:
        import yaml
        refs = yaml.safe_load((cfg.refs_dir / "knee_cartilage_reference_v1.yaml")
                              .read_text(encoding="utf-8"))
    except (OSError, AttributeError):
        refs = None
    return load_grading_params(refs)


def get_morphometry(conn: sqlite3.Connection, seg_id: int,
                    cfg: Any = None) -> dict[str, Any] | None:
    """The stored result for the current algorithm and thresholds, if any."""
    params = current_params(grading_params(cfg) if cfg is not None else None)
    row = conn.execute(
        "SELECT * FROM segmentation_morphometry"
        " WHERE segmentation_id=? AND algo_version=? AND params_hash=?",
        (seg_id, ALGO_VERSION, params_hash(params))).fetchone()
    return _row_to_json(row) if row else None


def _row_to_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "segmentationId": row["segmentation_id"],
        "algoVersion": row["algo_version"],
        "paramsHash": row["params_hash"],
        "params": json.loads(row["params_json"]),
        "frame": json.loads(row["frame_json"]) if row["frame_json"] else None,
        "metrics": json.loads(row["metrics_json"]) if row["metrics_json"] else None,
        "qc": json.loads(row["qc_json"]) if row["qc_json"] else None,
        "figures": json.loads(row["figures_json"]) if row["figures_json"] else None,
        "state": row["state"],
        "error": row["error"],
        "durationMs": row["duration_ms"],
        "computedAt": row["computed_at"],
    }


def figure_dir(cfg: Any, seg_key: str, p_hash: str) -> Path:
    return VolumeCache(cfg.cache_dir).fig_dir(seg_key, ALGO_VERSION, p_hash)


def figure_file(conn: sqlite3.Connection, cfg: Any, seg_id: int, name: str) -> Path | None:
    """The cached file behind a figure name, if the current row lists it.

    Never computes anything: a figure that is not there is a 404, and the
    caller is expected to regenerate the report rather than have a GET run
    seven seconds of morphometry.
    """
    stored = get_morphometry(conn, seg_id, cfg)
    if not stored or not stored.get("figures"):
        return None
    if name not in (stored["figures"].get("files") or {}):
        return None
    row = conn.execute("SELECT seg_key FROM segmentation WHERE id=?", (seg_id,)).fetchone()
    if row is None or not row["seg_key"]:
        return None
    path = figure_dir(cfg, row["seg_key"], stored["paramsHash"]) / name
    return path if path.is_file() else None


def compute_and_store(conn: sqlite3.Connection, cfg: Any, seg_id: int,
                      *, force: bool = False) -> dict[str, Any]:
    """Compute morphometry for one segmentation and persist it.

    Idempotent: returns the stored row unless ``force``. A failure is stored
    too - a knee whose medial and lateral labels are swapped must keep saying
    so on every request, not silently retry and burn 7 seconds each time.
    """
    grading = grading_params(cfg)
    params = current_params(grading)
    p_hash = params_hash(params)

    if not force:
        existing = get_morphometry(conn, seg_id, cfg)
        if existing is not None:
            return existing

    seg = conn.execute(
        "SELECT sg.id, sg.seg_key, sg.series_id, sg.seg_state, p.laterality,"
        "  s.volume_key, s.volume_state, ls.key AS label_set_key"
        " FROM segmentation sg"
        " JOIN series s ON s.id=sg.series_id"
        " JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id"
        " LEFT JOIN label_set ls ON ls.id=sg.label_set_id"
        " WHERE sg.id=?", (seg_id,)).fetchone()
    if seg is None:
        raise KeyError("segmentation %d" % seg_id)
    if seg["seg_state"] != "ready" or not seg["seg_key"]:
        raise ValueError("segmentation %d is not materialised yet" % seg_id)

    started = time.perf_counter()
    state, error, result = "ready", None, None
    try:
        result = _run(conn, cfg, seg, grading, p_hash)
    except Exception as exc:                      # noqa: BLE001
        state, error = "failed", "%s: %s" % (type(exc).__name__, exc)
        # A failed knee has no figures; make sure none from an earlier
        # attempt under the same key can be served against the failure.
        if seg["seg_key"]:
            shutil.rmtree(figure_dir(cfg, seg["seg_key"], p_hash), ignore_errors=True)

    duration_ms = int((time.perf_counter() - started) * 1000)
    conn.execute(
        "INSERT INTO segmentation_morphometry"
        " (segmentation_id, algo_version, params_hash, params_json, frame_json,"
        "  metrics_json, qc_json, figures_json, state, error, duration_ms, computed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(segmentation_id, algo_version, params_hash) DO UPDATE SET"
        "  params_json=excluded.params_json, frame_json=excluded.frame_json,"
        "  metrics_json=excluded.metrics_json, qc_json=excluded.qc_json,"
        "  figures_json=excluded.figures_json,"
        "  state=excluded.state, error=excluded.error,"
        "  duration_ms=excluded.duration_ms, computed_at=excluded.computed_at",
        (seg_id, ALGO_VERSION, p_hash, json.dumps(params, sort_keys=True),
         json.dumps(result["frame"]) if result else None,
         json.dumps(result["metrics"]) if result else None,
         json.dumps(result["qc"]) if result else None,
         json.dumps(result["figures"], ensure_ascii=False) if result and result.get("figures") else None,
         state, error, duration_ms))
    conn.commit()

    stored = get_morphometry(conn, seg_id, cfg)
    assert stored is not None
    return stored


def _run(conn: sqlite3.Connection, cfg: Any, seg: sqlite3.Row,
         grading: GradingParams, p_hash: str) -> dict[str, Any]:
    from ..labels import get_labelset
    from ..seg.ingest import series_geometry

    cache = VolumeCache(cfg.cache_dir)
    entry = cache.segmentation(seg["seg_key"])
    if not entry.exists:
        raise FileNotFoundError("segmentation labelmap is not in the cache")
    meta = entry.read_meta()
    cols, rows, slices = meta["dimensions"]
    canonical = entry.read_array((slices, rows, cols), meta["numpyDtype"])

    geom, _ = series_geometry(conn, int(seg["series_id"]))
    tree = compute_morphometry(canonical, geom, seg["laterality"], grading=grading,
                               keep_arrays=True)
    arrays = tree.pop("arrays", {})

    # Figures are drawn now, from these vertices, and cached under the same
    # key as the numbers. Their failure is noted, never fatal: a report with
    # numbers and no map beats no report.
    figures: dict[str, Any] | None
    try:
        vol = None
        keys = seg.keys() if hasattr(seg, "keys") else []
        if "volume_key" in keys and seg["volume_key"] and seg["volume_state"] == "ready":
            ventry = cache.volume(seg["volume_key"])
            if ventry.exists:
                vmeta = ventry.read_meta()
                vc, vr, vs = vmeta["dimensions"]
                vol = ventry.read_array((vs, vr, vc), vmeta["numpyDtype"])
        colors: dict[int, str] = {}
        ls_key = seg["label_set_key"] if "label_set_key" in keys else None
        if ls_key:
            ls = get_labelset(conn, ls_key)
            colors = {int(l["value"]): l["color"] for l in (ls or {}).get("labels", []) if l.get("color")}
        fig_dir = cache.fig_dir(seg["seg_key"], ALGO_VERSION, p_hash)
        shutil.rmtree(fig_dir, ignore_errors=True)
        figures = build_figures(fig_dir, int(seg["id"]), ALGO_VERSION, p_hash, arrays,
                                vol=vol, lab=canonical if vol is not None else None,
                                geom=geom, label_colors=colors)
    except Exception as exc:                      # noqa: BLE001
        figures = {"version": None, "error": "%s: %s" % (type(exc).__name__, exc),
                   "plates": {}, "slices": {}, "files": {}}
        tree["frame"]["notes"].append("figures failed: %s" % exc)

    return {
        "frame": tree["frame"],
        "metrics": {"plates": tree["plates"], "compartments": tree["compartments"],
                    "coverage": tree.get("coverage", {}), "grading": tree.get("grading", {})},
        "qc": _overall_qc(tree),
        "figures": figures,
    }


def _overall_qc(tree: dict[str, Any]) -> dict[str, Any]:
    """Study-level quality summary, area-weighted across plates.

    Kept at the top of the payload so a reader meets the caveats before the
    numbers rather than after them.
    """
    plates = tree["plates"]
    if not plates:
        return {"plateCount": 0}

    areas = np.array([p["interface_area_mm2"] for p in plates], float)
    total = float(areas.sum()) or 1.0

    def wavg(key: str) -> float:
        return float(sum(p["qc"][key] * a for p, a in zip(plates, areas)) / total)

    confidences = [s["confidence"]
                   for p in plates for s in p["subregions"].values()]
    return {
        "plateCount": len(plates),
        "reliableAreaFraction": round(wavg("reliableAreaFraction"), 3),
        "effectiveResolutionMm": round(wavg("effectiveResolutionMm"), 3),
        "escapedFraction": round(wavg("escapedFraction"), 4),
        "medialLateralConsistent": tree["frame"]["medial_lateral_consistent"],
        "frameNotes": tree["frame"]["notes"],
        "subregionConfidence": {
            level: confidences.count(level) for level in ("high", "medium", "low")},
        # Repeated in every payload deliberately: it is the one caveat that
        # must never be separated from the numbers it applies to.
        "systematicBiasNote": (
            "层厚 3 mm 的各向异性采集会使软骨厚度系统性低估约 7-18%%，"
            "且偏差随真实厚度非单调变化，无法用固定系数校正。"
            "绝对值不可直接与文献参考值比较；同一膝关节内的内外侧对比更可靠。"),
    }
