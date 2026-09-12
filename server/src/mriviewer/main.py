"""FastAPI application.

One process serves both the API and the built frontend, so an air-gapped
install needs no nginx. Volume and mesh payloads are stored already gzipped and
streamed straight out with ``Content-Encoding: gzip`` - the server never
compresses per request and the browser decompresses natively.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, load_config
from .db.session import connect, init_db
from .labels import get_labelset, list_labelsets, load_labelset_files, update_label
from .volume.cache import VolumeCache

VERSION = "0.1.0"

CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
    "worker-src 'self' blob:; script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; font-src 'self'; object-src 'none'; "
    "base-uri 'self'"
)

app = FastAPI(title="MRI Viewer", version=VERSION, default_response_class=ORJSONResponse)

_state: dict[str, Any] = {}
_build_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cfg() -> Config:
    return _state["cfg"]


def db() -> sqlite3.Connection:
    return _state["db"]


def cache() -> VolumeCache:
    return _state["cache"]


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _build_locks.setdefault(key, threading.Lock())


@app.on_event("startup")
def _startup() -> None:
    c = load_config(os.environ.get("MRIVIEWER_CONFIG"))
    c.ensure_dirs()
    conn = init_db(c.db_path)
    load_labelset_files(conn, c.labelsets_dir)
    _state.update(cfg=c, db=conn, cache=VolumeCache(c.cache_dir))

    web = c.web_dir
    if web and Path(web).is_dir():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


def _gz(path: Path, media_type: str = "application/octet-stream") -> FileResponse:
    if not path.exists():
        raise HTTPException(404, "not cached")
    return FileResponse(
        path, media_type=media_type,
        headers={
            "Content-Encoding": "gzip",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


# ------------------------------------------------------------------ meta

@app.get("/api/v1/version")
def version() -> dict[str, Any]:
    return {"version": VERSION, "cache": str(cfg().cache_dir)}


@app.get("/api/v1/healthz")
def healthz() -> dict[str, str]:
    db().execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/v1/datasets")
def datasets() -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT d.id, d.key, d.name, d.adapter, d.viewer, d.delivery_profile,"
        " (SELECT COUNT(*) FROM patient p WHERE p.dataset_id=d.id) n_patients,"
        " (SELECT COUNT(*) FROM series s JOIN study st ON st.id=s.study_id"
        "   JOIN patient p ON p.id=st.patient_id WHERE p.dataset_id=d.id) n_series,"
        " (SELECT COUNT(*) FROM segmentation g WHERE g.dataset_id=d.id) n_segmentations"
        " FROM dataset d ORDER BY d.id").fetchall()   # config.toml order
    return [dict(r) for r in rows]


# -------------------------------------------------------------- patients

@app.get("/api/v1/patients")
def patients(dataset: str | None = None, q: str | None = None,
             report_q: str | None = None,
             has_segmentation: bool | None = None,
             limit: int = Query(100, le=1000), offset: int = 0) -> dict[str, Any]:
    where, params = ["1=1"], []
    if dataset:
        where.append("d.key=?")
        params.append(dataset)
    if q:
        where.append("(p.external_id LIKE ? OR p.name LIKE ? OR p.group_name LIKE ?)")
        params += ["%" + q + "%"] * 3
    if report_q:
        # Substring, not FTS5 - see reports.search() for why that matters here.
        where.append(
            "EXISTS (SELECT 1 FROM report r WHERE r.dataset_id=p.dataset_id"
            " AND r.external_id=p.external_id"
            " AND (r.findings LIKE ? OR r.diagnosis LIKE ? OR r.suggestion LIKE ?))"
        )
        params += ["%" + report_q + "%"] * 3

    # seg_count is a computed column, and SQLite will not accept an alias in
    # WHERE - wrap the projection in a subquery and filter outside it.
    inner = (
        "SELECT p.id, p.external_id, p.name, p.sex, p.age, p.group_name, p.laterality,"
        " d.key AS dataset, d.viewer,"
        " (SELECT COUNT(*) FROM study st WHERE st.patient_id=p.id) n_studies,"
        " (SELECT COUNT(*) FROM series s JOIN study st ON st.id=s.study_id"
        "   WHERE st.patient_id=p.id) n_series,"
        " (SELECT COUNT(*) FROM segmentation g JOIN series s ON s.id=g.series_id"
        "   JOIN study st ON st.id=s.study_id WHERE st.patient_id=p.id) seg_count"
        " FROM patient p JOIN dataset d ON d.id=p.dataset_id"
        " WHERE " + " AND ".join(where)
    )
    outer_where = " WHERE seg_count > 0" if has_segmentation else ""
    rows = db().execute(
        "SELECT * FROM (" + inner + ")" + outer_where +
        " ORDER BY dataset, CAST(external_id AS INTEGER), external_id LIMIT ? OFFSET ?",
        params + [limit, offset]).fetchall()
    total = db().execute(
        "SELECT COUNT(*) n FROM (" + inner + ")" + outer_where, params).fetchone()
    return {"total": int(total["n"]), "items": [dict(r) for r in rows]}


@app.get("/api/v1/reports/search")
def reports_search(q: str, dataset: str | None = None,
                   limit: int = Query(100, le=1000)) -> dict[str, Any]:
    """Substring search over the de-identified reports.

    Useful for picking training data: on the Changzheng set this finds 402 of
    2485 knees whose report mentions 软骨.
    """
    from .reports import count_matches, search
    ds_id = None
    if dataset:
        row = db().execute("SELECT id FROM dataset WHERE key=?", (dataset,)).fetchone()
        if row is None:
            raise HTTPException(404, "dataset not found")
        ds_id = int(row["id"])
    return {
        "query": q,
        "total": count_matches(db(), q, ds_id),
        "items": search(db(), q, ds_id, limit=limit),
    }


@app.get("/api/v1/patients/{patient_id}")
def patient_detail(patient_id: int) -> dict[str, Any]:
    p = db().execute(
        "SELECT p.*, d.key AS dataset, d.viewer FROM patient p"
        " JOIN dataset d ON d.id=p.dataset_id WHERE p.id=?", (patient_id,)).fetchone()
    if p is None:
        raise HTTPException(404, "patient not found")
    out: dict[str, Any] = dict(p)
    out["extra"] = json.loads(p["extra_json"]) if p["extra_json"] else {}
    out.pop("extra_json", None)

    studies = []
    for st in db().execute("SELECT * FROM study WHERE patient_id=? ORDER BY id",
                           (patient_id,)).fetchall():
        s_rows = db().execute(
            "SELECT id, series_uid, series_number, modality, description, path_rel,"
            " rows, cols, n_slices, n_instances, slice_spacing, acquisition_plane,"
            " anisotropy_ratio, volume_state, geometry_warning, dtype"
            " FROM series WHERE study_id=? ORDER BY COALESCE(series_number, id)",
            (st["id"],)).fetchall()
        series = []
        for s in s_rows:
            d = dict(s)
            d["segmentations"] = [
                dict(g) for g in db().execute(
                    "SELECT id, display_name, origin, kind, seg_state, model_name,"
                    " model_version, version_int, association_rule, seg_error"
                    " FROM segmentation WHERE series_id=? AND is_active=1"
                    " ORDER BY version_int, id", (s["id"],)).fetchall()
            ]
            series.append(d)
        studies.append({**dict(st), "series": series})
    out["studies"] = studies

    rep = db().execute(
        "SELECT * FROM report WHERE dataset_id=? AND external_id=?",
        (p["dataset_id"], p["external_id"])).fetchone()
    out["report"] = dict(rep) if rep else None
    return out


# ---------------------------------------------------------------- series

def _series_row(series_id: int) -> sqlite3.Row:
    row = db().execute(
        "SELECT s.*, d.key AS ds_key, d.root_path, p.external_id AS patient_external_id,"
        " p.name AS patient_name, d.viewer"
        " FROM series s JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id JOIN dataset d ON d.id=p.dataset_id"
        " WHERE s.id=?", (series_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "series not found")
    return row


@app.get("/api/v1/series/{series_id}")
def series_detail(series_id: int) -> dict[str, Any]:
    row = _series_row(series_id)
    out = {k: row[k] for k in row.keys() if k != "root_path"}
    out["segmentations"] = [
        dict(g) for g in db().execute(
            "SELECT id, display_name, origin, kind, seg_state, seg_error, model_name,"
            " model_version, version_int, parent_segmentation_id, association_rule,"
            " present_values_json FROM segmentation"
            " WHERE series_id=? AND is_active=1 ORDER BY version_int, id",
            (series_id,)).fetchall()
    ]
    for g in out["segmentations"]:
        g["presentValues"] = json.loads(g.pop("present_values_json") or "[]")
    return out


def _ensure_volume(series_id: int) -> tuple[str, dict[str, Any]]:
    from .scan.runner import SourceUnreadable, materialize_series
    row = _series_row(series_id)
    if row["volume_state"] == "error":
        # Known-bad source. Re-reading it costs hundreds of MB and fails again.
        raise HTTPException(422, "source unreadable: %s" % (row["volume_error"] or ""))
    if row["volume_state"] == "ready" and row["volume_key"]:
        entry = cache().volume(row["volume_key"])
        if entry.exists:
            return row["volume_key"], entry.read_meta()
    with _lock_for("vol:%d" % series_id):
        row = _series_row(series_id)
        if row["volume_state"] == "ready" and row["volume_key"]:
            entry = cache().volume(row["volume_key"])
            if entry.exists:
                return row["volume_key"], entry.read_meta()
        try:
            meta = materialize_series(db(), cfg(), series_id)
        except SourceUnreadable as exc:
            # The bytes on disk are bad; that is a 422, not a server fault.
            raise HTTPException(422, "source unreadable: %s" % exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, "volume build failed: %s" % exc) from exc
        row = _series_row(series_id)
        return row["volume_key"], meta


@app.get("/api/v1/series/{series_id}/volume.json")
def volume_meta(series_id: int, variant: str = "native") -> dict[str, Any]:
    key, meta = _ensure_volume(series_id)
    if variant != "native":
        entry = cache().volume(key, variant)
        if not entry.exists:
            raise HTTPException(404, "variant %s not built" % variant)
        meta = entry.read_meta()
    meta["seriesId"] = series_id
    meta["url"] = "/api/v1/series/%d/volume.raw?variant=%s" % (series_id, meta["variant"])
    return meta


@app.get("/api/v1/series/{series_id}/volume.raw")
def volume_raw(series_id: int, variant: str = "native") -> FileResponse:
    key, _ = _ensure_volume(series_id)
    return _gz(cache().volume(key, variant).raw_path)


@app.post("/api/v1/series/{series_id}/volume/build")
def volume_build(series_id: int, force: bool = False) -> dict[str, Any]:
    from .scan.runner import materialize_series
    with _lock_for("vol:%d" % series_id):
        meta = materialize_series(db(), cfg(), series_id, force=force)
    return {"status": "ready", "dimensions": meta["dimensions"],
            "byteLength": meta["byteLength"]}


# --------------------------------------------------------- segmentations

def _seg_row(seg_id: int) -> sqlite3.Row:
    row = db().execute("SELECT * FROM segmentation WHERE id=?", (seg_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "segmentation not found")
    return row


def _ensure_seg(seg_id: int) -> tuple[str, dict[str, Any]]:
    from .seg.ingest import materialize_segmentation
    row = _seg_row(seg_id)
    if row["seg_state"] == "ready" and row["seg_key"]:
        entry = cache().segmentation(row["seg_key"])
        if entry.exists:
            return row["seg_key"], entry.read_meta()
    with _lock_for("seg:%d" % seg_id):
        row = _seg_row(seg_id)
        if row["seg_state"] == "ready" and row["seg_key"]:
            entry = cache().segmentation(row["seg_key"])
            if entry.exists:
                return row["seg_key"], entry.read_meta()
        if row["series_id"]:
            _ensure_volume(int(row["series_id"]))
        try:
            meta = materialize_segmentation(db(), cfg(), seg_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, "labelmap build failed: %s" % exc) from exc
        return _seg_row(seg_id)["seg_key"], meta


@app.get("/api/v1/segmentations/{seg_id}")
def segmentation_detail(seg_id: int) -> dict[str, Any]:
    row = _seg_row(seg_id)
    out = {k: row[k] for k in row.keys()
           if k not in {"ingest_transform_json", "affine_json"}}
    out["stats"] = json.loads(row["stats_json"]) if row["stats_json"] else None
    out.pop("stats_json", None)
    out["presentValues"] = json.loads(row["present_values_json"] or "[]")
    out.pop("present_values_json", None)
    if row["label_set_id"]:
        ls = db().execute("SELECT key FROM label_set WHERE id=?",
                          (row["label_set_id"],)).fetchone()
        out["labelSet"] = get_labelset(db(), ls["key"]) if ls else None
    return out


@app.get("/api/v1/segmentations/{seg_id}/labelmap.json")
def labelmap_meta(seg_id: int) -> dict[str, Any]:
    key, meta = _ensure_seg(seg_id)
    meta["segmentationDbId"] = seg_id
    meta["url"] = "/api/v1/segmentations/%d/labelmap.raw" % seg_id
    row = _seg_row(seg_id)
    if row["label_set_id"]:
        ls = db().execute("SELECT key FROM label_set WHERE id=?",
                          (row["label_set_id"],)).fetchone()
        meta["labelSet"] = get_labelset(db(), ls["key"]) if ls else None
    return meta


@app.get("/api/v1/segmentations/{seg_id}/labelmap.raw")
def labelmap_raw(seg_id: int) -> FileResponse:
    key, _ = _ensure_seg(seg_id)
    return _gz(cache().segmentation(key).raw_path)


@app.get("/api/v1/segmentations/{seg_id}/meshes")
def segmentation_meshes(seg_id: int) -> dict[str, Any]:
    _ensure_seg(seg_id)
    from .seg.mesh import MESH_PIPELINE_VERSION
    row = _seg_row(seg_id)
    rows = db().execute(
        "SELECT label_value, n_points, n_tris, bytes, params_json FROM segmentation_mesh"
        " WHERE segmentation_id=? ORDER BY label_value", (seg_id,)).fetchall()
    # Only meshes the current pipeline wrote, and whose file is really there.
    # Anything else is reported as absent so the client's "empty -> build" path
    # rebuilds it, rather than showing a surface today's code would not produce.
    current = []
    for r in rows:
        try:
            params = json.loads(r["params_json"] or "{}")
        except ValueError:
            params = {}
        if params.get("pipeline") != MESH_PIPELINE_VERSION:
            continue
        if row["seg_key"] and not cache().mesh_path(row["seg_key"], r["label_value"]).exists():
            continue
        current.append({
            "label_value": r["label_value"], "n_points": r["n_points"],
            "n_tris": r["n_tris"], "bytes": r["bytes"],
            "url": "/api/v1/segmentations/%d/mesh/%d.bin" % (seg_id, r["label_value"]),
        })
    return {"segmentationId": seg_id, "state": row["mesh_state"], "meshes": current}


@app.post("/api/v1/segmentations/{seg_id}/meshes/build")
def segmentation_meshes_build(seg_id: int, force: bool = False) -> dict[str, Any]:
    from .seg.mesh import build_meshes_for_segmentation
    _ensure_seg(seg_id)
    with _lock_for("mesh:%d" % seg_id):
        built = build_meshes_for_segmentation(db(), cfg(), seg_id, force=force)
    return {"status": "ready", "built": built}


# ------------------------------------------------------- morphometry

@app.get("/api/v1/segmentations/{seg_id}/morphometry")
def segmentation_morphometry(seg_id: int) -> dict[str, Any]:
    """Stored cartilage morphometry, or a 404 telling the caller to build it."""
    from .morph.service import get_morphometry
    _seg_row(seg_id)
    stored = get_morphometry(db(), seg_id)
    if stored is None:
        raise HTTPException(404, "not computed")
    return stored


@app.post("/api/v1/segmentations/{seg_id}/morphometry/build")
def segmentation_morphometry_build(seg_id: int, force: bool = False) -> dict[str, Any]:
    """Compute morphometry, roughly 7 s per knee.

    Synchronous on purpose. At this cost a job queue would add failure modes
    and latency without buying anything; if it ever grows, the lock is already
    the natural place to hand off to a worker.
    """
    from .morph.service import compute_and_store
    _ensure_seg(seg_id)
    with _lock_for("morph:%d" % seg_id):
        try:
            return compute_and_store(db(), cfg(), seg_id, force=force)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc


@app.get("/api/v1/segmentations/{seg_id}/mesh/{label_value}.bin")
def segmentation_mesh(seg_id: int, label_value: int) -> FileResponse:
    row = _seg_row(seg_id)
    if not row["seg_key"]:
        raise HTTPException(404, "labelmap not built")
    return _gz(cache().mesh_path(row["seg_key"], label_value))


# ------------------------------------------------------------- labelsets

@app.get("/api/v1/labelsets")
def labelsets() -> list[dict[str, Any]]:
    return list_labelsets(db())


# --------------------------------------------------------- AI reports

@app.get("/api/v1/ai/status")
def ai_status() -> dict[str, Any]:
    """Whether report generation is on, and whether the model is actually there.

    Probes /models rather than trusting config: DeepSeek retired the model ids
    this was first written against, and a stale name should surface here rather
    than as a failed report.
    """
    from .ai.client import EgressBlocked, LlmError, check_egress, list_models
    from .ai.settings import resolved_ai
    a = resolved_ai(db(), cfg())
    out: dict[str, Any] = {
        "enabled": a.enabled, "allowEgress": a.allow_egress,
        "baseUrl": a.base_url, "model": a.model,
        "keyPresent": bool(a.read_api_key()),
        "patientContext": list(a.patient_context),
        "reachable": False, "modelPresent": None, "detail": None,
    }
    if not a.enabled:
        out["detail"] = "ai.enabled is false - reports use the built-in template"
        return out
    try:
        check_egress(a.base_url, a.allow_egress)
    except EgressBlocked as exc:
        out["detail"] = str(exc)
        return out
    try:
        models = list_models(a.base_url, a.read_api_key(), allow_egress=a.allow_egress)
        out["reachable"] = True
        out["modelPresent"] = a.model in models
        if not out["modelPresent"]:
            out["detail"] = "configured model not advertised; available: " + ", ".join(models[:8])
    except LlmError as exc:
        out["detail"] = str(exc)
    return out


@app.get("/api/v1/ai/settings")
def ai_settings_get() -> dict[str, Any]:
    """Current effective settings. Never includes the API key itself."""
    from .ai.settings import read_settings, resolved_ai
    a = resolved_ai(db(), cfg())
    return {
        "enabled": a.enabled, "allowEgress": a.allow_egress,
        "baseUrl": a.base_url, "model": a.model, "timeoutS": a.timeout_s,
        "keyPresent": bool(a.read_api_key()),
        "patientContext": list(a.patient_context),
        # Which values come from the database rather than the config file, so
        # the UI can show what an operator has overridden.
        "overridden": sorted(read_settings(db())),
    }


@app.put("/api/v1/ai/settings")
def ai_settings_put(payload: dict[str, Any]) -> dict[str, Any]:
    """Update settings from the web UI.

    NOTE: this platform has no authentication - anyone who can reach it on the
    LAN can change these, including where report data is sent. That is the
    deployment's deliberate choice; the UI states it in the panel.
    """
    from .ai.settings import clear_api_key, resolved_ai, write_api_key, write_settings

    patch = {
        "enabled": payload.get("enabled"),
        "allow_egress": payload.get("allowEgress"),
        "base_url": payload.get("baseUrl"),
        "model": payload.get("model"),
        "timeout_s": payload.get("timeoutS"),
    }
    patch = {k: v for k, v in patch.items() if v is not None}
    if "base_url" in patch and not str(patch["base_url"]).startswith(("http://", "https://")):
        raise HTTPException(400, "baseUrl must start with http:// or https://")
    write_settings(db(), patch, updated_by=str(payload.get("updatedBy") or ""))

    # The key travels one way only: in. It is written to a 0600 file and is
    # never returned by any endpoint.
    key = payload.get("apiKey")
    if key is not None:
        a = resolved_ai(db(), cfg())
        if str(key).strip():
            write_api_key(a.api_key_file, str(key))
        else:
            clear_api_key(a.api_key_file)
    return ai_settings_get()


@app.post("/api/v1/ai/probe")
def ai_probe(payload: dict[str, Any]) -> dict[str, Any]:
    """Try settings before saving them.

    Saving first and discovering the model name is wrong afterwards is the
    workflow this avoids - DeepSeek has already retired model ids we shipped.
    """
    from .ai.client import EgressBlocked, LlmError, list_models
    from .ai.settings import resolved_ai

    a = resolved_ai(db(), cfg())
    base_url = str(payload.get("baseUrl") or a.base_url)
    allow_egress = bool(payload.get("allowEgress", a.allow_egress))
    key = payload.get("apiKey") or a.read_api_key()
    try:
        models = list_models(base_url, key, allow_egress=allow_egress)
    except EgressBlocked as exc:
        return {"ok": False, "models": [], "detail": str(exc), "egressBlocked": True}
    except LlmError as exc:
        return {"ok": False, "models": [], "detail": str(exc)}
    wanted = str(payload.get("model") or a.model)
    return {"ok": True, "models": models, "modelPresent": wanted in models,
            "detail": None}


@app.get("/api/v1/segmentations/{seg_id}/ai-report")
def ai_report_get(seg_id: int) -> dict[str, Any]:
    from .ai.service import get_report
    _seg_row(seg_id)
    rep = get_report(db(), seg_id)
    if rep is None:
        raise HTTPException(404, "no report yet")
    return rep


@app.get("/api/v1/segmentations/{seg_id}/ai-report/state")
def ai_report_state(seg_id: int) -> dict[str, Any]:
    """Whether a report exists. Absence is data, not an error."""
    from .ai.service import report_state
    _seg_row(seg_id)
    return report_state(db(), seg_id)


@app.post("/api/v1/segmentations/{seg_id}/ai-report")
def ai_report_build(seg_id: int) -> dict[str, Any]:
    """Write a new report version. Always inserts, never overwrites."""
    from .ai.service import generate_report
    _ensure_seg(seg_id)
    with _lock_for("aireport:%d" % seg_id):
        try:
            return generate_report(db(), cfg(), seg_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc


@app.patch("/api/v1/ai-reports/{report_id}")
def ai_report_review(report_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Record a clinician's review of a report version."""
    row = db().execute("SELECT segmentation_id FROM ai_report WHERE id=?",
                       (report_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "report not found")
    state = payload.get("reviewState")
    if state not in ("unreviewed", "approved", "rejected", "edited"):
        raise HTTPException(400, "bad reviewState")
    db().execute(
        "UPDATE ai_report SET review_state=?, reviewed_by=?, reviewed_text=?,"
        " reviewed_at=datetime('now') WHERE id=?",
        (state, str(payload.get("reviewedBy") or "")[:120],
         payload.get("reviewedText"), report_id))
    db().commit()
    from .ai.service import get_report
    rep = get_report(db(), int(row["segmentation_id"]))
    assert rep is not None
    return rep


# ------------------------------------------------------ report documents

@app.get("/api/v1/report-templates/{key}")
def report_template(key: str) -> dict[str, Any]:
    """The chapter skeleton, as JSON. Re-read from disk every call so a YAML
    edit takes effect on save."""
    from .report.template import load_template
    try:
        return load_template(cfg(), key).to_json()
    except FileNotFoundError:
        raise HTTPException(404, "template not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/segmentations/{seg_id}/report-document/state")
def report_document_state(seg_id: int) -> dict[str, Any]:
    from .report.document import document_state
    _seg_row(seg_id)
    return document_state(db(), seg_id)


@app.get("/api/v1/segmentations/{seg_id}/report-document")
def report_document_get(seg_id: int) -> dict[str, Any]:
    from .report.document import get_document
    _seg_row(seg_id)
    doc = get_document(db(), seg_id)
    if doc is None:
        raise HTTPException(404, "no report document yet")
    return doc


@app.post("/api/v1/segmentations/{seg_id}/report-document")
def report_document_build(seg_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Generate (or regenerate) the chaptered report. Replaces the prior one;
    doctor overrides are carried forward and flagged stale."""
    from .report.document import generate_document
    _ensure_seg(seg_id)
    actor = str((payload or {}).get("actor") or "")
    with _lock_for("report:%d" % seg_id):
        try:
            return generate_document(db(), cfg(), seg_id, actor=actor)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc


@app.post("/api/v1/report-documents/{doc_id}/overrides")
def report_override_set(doc_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    from .report.document import set_override
    try:
        return set_override(db(), doc_id, dict(payload.get("target") or {}),
                            payload.get("value"), str(payload.get("editor") or ""),
                            str(payload.get("reason") or ""))
    except KeyError:
        raise HTTPException(404, "document not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/v1/report-documents/{doc_id}/overrides/{override_id}")
def report_override_revoke(doc_id: int, override_id: str,
                           payload: dict[str, Any]) -> dict[str, Any]:
    from .report.document import revoke_override
    try:
        return revoke_override(db(), doc_id, override_id, str(payload.get("editor") or ""))
    except KeyError:
        raise HTTPException(404, "document not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/v1/report-documents/{doc_id}")
def report_document_review(doc_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    from .report.document import review_document
    try:
        return review_document(db(), doc_id, str(payload.get("reviewState") or ""),
                               str(payload.get("reviewedBy") or ""))
    except KeyError:
        raise HTTPException(404, "document not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/pdf/status")
def pdf_status() -> dict[str, Any]:
    from .report.pdf import pdf_available
    return pdf_available(cfg())


@app.get("/api/v1/segmentations/{seg_id}/report.pdf")
def report_pdf(seg_id: int) -> Response:
    """The print view of the report document, rendered to A4 by headless Chromium.

    A plain `def` on purpose: Playwright's sync API cannot run on the event
    loop thread, and FastAPI runs sync handlers in its thread pool.
    """
    from .report.document import get_document
    from .report.pdf import pdf_available, render_report_pdf
    _seg_row(seg_id)
    doc = get_document(db(), seg_id)
    if doc is None:
        raise HTTPException(409, "尚未生成报告文档，请先在阅片页生成膝关节报告")
    avail = pdf_available(cfg())
    if not avail["available"]:
        raise HTTPException(503, avail["detail"] or "PDF 渲染不可用")
    try:
        pdf = render_report_pdf(cfg(), seg_id, doc.get("header") or {},
                                (doc.get("header") or {}).get("disclaimerZh") or "")
    except Exception as exc:                     # noqa: BLE001
        raise HTTPException(500, "PDF 渲染失败：%s" % str(exc)[:300]) from exc
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": 'attachment; filename="report_%d.pdf"' % doc["id"],
        "Cache-Control": "no-store",
    })


@app.get("/api/v1/references/{key}")
def references(key: str) -> dict[str, Any]:
    """Clinical reference values, served from a YAML file doctors can edit.

    Reloaded from disk on every request, so a correction to a reference value
    or an explanation takes effect on save with no deployment. The volume is a
    handful of requests per report, so the read cost does not matter.
    """
    import yaml

    if not key.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "bad reference key")
    path = (cfg().refs_dir / (key + ".yaml"))
    if not path.is_file():
        raise HTTPException(404, "reference set not found")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@app.get("/api/v1/labelsets/{key}")
def labelset(key: str) -> dict[str, Any]:
    ls = get_labelset(db(), key)
    if ls is None:
        raise HTTPException(404, "label set not found")
    return ls


@app.patch("/api/v1/labelsets/{key}/labels/{value}")
async def patch_label(key: str, value: int, request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        return update_label(db(), key, value, name=body.get("name"),
                            name_zh=body.get("nameZh"), color=body.get("color"),
                            opacity=body.get("opacity"))
    except KeyError:
        raise HTTPException(404, "label set not found") from None
