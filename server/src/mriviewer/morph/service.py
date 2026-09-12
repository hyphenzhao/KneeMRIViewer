"""Wiring: cached segmentation -> metric tree -> database row.

Separate from ``compute.py`` so the maths stays runnable on a bare pair of
NIfTI files during development, with no database in the picture.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

import numpy as np

from ..volume.cache import VolumeCache
from .compute import (ALGO_VERSION, MIN_RELIABLE_AREA_FRACTION,
                      MIN_SLICES_FOR_CONFIDENCE, RAY_NN_RATIO_RANGE,
                      compute_morphometry)
from .parcellation import (CENTRAL_SPAN_FRACTION, PATELLA_FACET_SHIFT,
                           TIBIAL_CENTRAL_AREA_FRACTION,
                           TROCHLEA_SPAN_FRACTION)
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
        "state": row["state"],
        "error": row["error"],
        "durationMs": row["duration_ms"],
        "computedAt": row["computed_at"],
    }


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
        "SELECT sg.id, sg.seg_key, sg.series_id, sg.seg_state, p.laterality"
        " FROM segmentation sg"
        " JOIN series s ON s.id=sg.series_id"
        " JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id"
        " WHERE sg.id=?", (seg_id,)).fetchone()
    if seg is None:
        raise KeyError("segmentation %d" % seg_id)
    if seg["seg_state"] != "ready" or not seg["seg_key"]:
        raise ValueError("segmentation %d is not materialised yet" % seg_id)

    started = time.perf_counter()
    state, error, result = "ready", None, None
    try:
        result = _run(conn, cfg, seg, grading)
    except Exception as exc:                      # noqa: BLE001
        state, error = "failed", "%s: %s" % (type(exc).__name__, exc)

    duration_ms = int((time.perf_counter() - started) * 1000)
    conn.execute(
        "INSERT INTO segmentation_morphometry"
        " (segmentation_id, algo_version, params_hash, params_json, frame_json,"
        "  metrics_json, qc_json, state, error, duration_ms, computed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))"
        " ON CONFLICT(segmentation_id, algo_version, params_hash) DO UPDATE SET"
        "  params_json=excluded.params_json, frame_json=excluded.frame_json,"
        "  metrics_json=excluded.metrics_json, qc_json=excluded.qc_json,"
        "  state=excluded.state, error=excluded.error,"
        "  duration_ms=excluded.duration_ms, computed_at=excluded.computed_at",
        (seg_id, ALGO_VERSION, p_hash, json.dumps(params, sort_keys=True),
         json.dumps(result["frame"]) if result else None,
         json.dumps(result["metrics"]) if result else None,
         json.dumps(result["qc"]) if result else None,
         state, error, duration_ms))
    conn.commit()

    stored = get_morphometry(conn, seg_id, cfg)
    assert stored is not None
    return stored


def _run(conn: sqlite3.Connection, cfg: Any, seg: sqlite3.Row,
         grading: GradingParams) -> dict[str, Any]:
    from ..seg.ingest import series_geometry

    entry = VolumeCache(cfg.cache_dir).segmentation(seg["seg_key"])
    if not entry.exists:
        raise FileNotFoundError("segmentation labelmap is not in the cache")
    meta = entry.read_meta()
    cols, rows, slices = meta["dimensions"]
    canonical = entry.read_array((slices, rows, cols), meta["numpyDtype"])

    geom, _ = series_geometry(conn, int(seg["series_id"]))
    tree = compute_morphometry(canonical, geom, seg["laterality"], grading=grading)

    return {
        "frame": tree["frame"],
        "metrics": {"plates": tree["plates"], "compartments": tree["compartments"],
                    "coverage": tree.get("coverage", {}), "grading": tree.get("grading", {})},
        "qc": _overall_qc(tree),
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
