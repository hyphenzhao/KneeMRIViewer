"""Generate, store, override and review the chaptered report.

One document per segmentation, replaced on regenerate. Doctor overrides live
in the same row and survive regeneration - flagged stale - because a doctor's
correction should not evaporate because someone pressed a button.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from ..ai.payload import age_band
from . import radiologist as rad
from .render import find_target, render
from .sources import GenContext, resolve
from .template import ReportTemplate, load_template

TEMPLATE_KEY = "knee_zh_v1"
GRADE_VALUES = ("0", "II", "III", "IV", "未评估")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ reads

def _row_to_json(row: sqlite3.Row) -> dict[str, Any]:
    doc = {
        "id": row["id"],
        "segmentationId": row["segmentation_id"],
        "morphometryId": row["morphometry_id"],
        "aiReportId": row["ai_report_id"],
        "templateKey": row["template_key"],
        "templateHash": row["template_hash"],
        "generation": row["generation"],
        "chapters": json.loads(row["chapters_json"] or "[]"),
        "overrides": json.loads(row["overrides_json"] or "[]"),
        "header": json.loads(row["header_json"]) if row["header_json"] else {},
        "status": row["status"],
        "error": row["error"],
        "generatedAt": row["generated_at"],
        "generatedBy": row["generated_by"],
        "reviewState": row["review_state"],
        "reviewedBy": row["reviewed_by"],
        "reviewedAt": row["reviewed_at"],
        "signed": json.loads(row["signed_json"]) if row["signed_json"] else None,
    }
    hdr = doc["header"]
    doc["titleZh"] = hdr.get("titleZh")
    doc["disclaimerZh"] = hdr.get("disclaimerZh")
    doc["rendered"] = render(doc)
    return doc


def get_document(conn: sqlite3.Connection, seg_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM report_document WHERE segmentation_id=?",
                       (seg_id,)).fetchone()
    return _row_to_json(row) if row else None


def get_document_by_id(conn: sqlite3.Connection, doc_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM report_document WHERE id=?", (doc_id,)).fetchone()
    return _row_to_json(row) if row else None


def document_state(conn: sqlite3.Connection, seg_id: int) -> dict[str, Any]:
    """Absence is data (`exists: false`), never a 404."""
    row = conn.execute(
        "SELECT d.id, d.generation, d.status, d.review_state, d.generated_at,"
        " d.chapters_json, a.created_at AS ai_created"
        " FROM report_document d LEFT JOIN ai_report a ON a.id = d.ai_report_id"
        " WHERE d.segmentation_id=?", (seg_id,)).fetchone()
    if row is None:
        return {"exists": False}
    chapters = json.loads(row["chapters_json"] or "[]")
    with_data = sum(1 for c in chapters if c.get("status") == "ok"
                    and c.get("section") == "findings")
    findings = sum(1 for c in chapters if c.get("section") == "findings")
    return {
        "exists": True, "id": row["id"], "generation": row["generation"],
        "status": row["status"], "reviewState": row["review_state"],
        "generatedAt": row["generated_at"],
        "findingsChaptersWithData": with_data, "findingsChapters": findings,
        # The sidebar's cartilage button regenerates ai_report on its own; if
        # that happened after this document was built, the document is behind.
        "cartilageStale": bool(row["ai_created"] and row["generated_at"]
                               and row["ai_created"] > row["generated_at"]),
    }


# --------------------------------------------------------------- generate

def generate_document(conn: sqlite3.Connection, cfg: Any, seg_id: int, *,
                      actor: str = "") -> dict[str, Any]:
    """Generate the whole document. Drains the streaming generator below."""
    doc = None
    for event in iter_generate_document(conn, cfg, seg_id, actor=actor):
        if "document" in event:
            doc = event["document"]
    assert doc is not None
    return doc


def iter_generate_document(conn: sqlite3.Connection, cfg: Any, seg_id: int, *,
                           actor: str = "") -> Iterator[dict[str, Any]]:
    """Generate the document, yielding a progress event before each step.

    Progress is emitted from the work itself rather than animated on the
    client: morphometry takes seconds, the cartilage model takes tens of
    seconds, and the remaining chapters are near-instant. A reader watching
    the list sees where the time actually goes.

    The final event carries ``document``.
    """
    from ..ai.service import generate_report, get_report
    from ..morph.service import compute_and_store

    tpl: ReportTemplate = load_template(cfg, TEMPLATE_KEY)
    total = 3 + len(tpl.chapters)

    yield {"stage": "morphometry", "labelZh": "测量软骨形态学", "done": 0, "total": total}
    morph = compute_and_store(conn, cfg, seg_id)
    if morph.get("state") != "ready":
        raise ValueError(morph.get("error") or "morphometry failed")

    meta = conn.execute(
        "SELECT p.sex, p.age, p.external_id, p.dataset_id, s.description AS series_description"
        " FROM segmentation sg JOIN series s ON s.id=sg.series_id"
        " JOIN study st ON st.id=s.study_id JOIN patient p ON p.id=st.patient_id"
        " WHERE sg.id=?", (seg_id,)).fetchone()
    report_row = conn.execute(
        "SELECT findings, diagnosis, raw_json FROM report"
        " WHERE dataset_id=? AND external_id=?",
        (meta["dataset_id"], meta["external_id"])).fetchone() if meta else None

    errors: dict[str, str] = {}
    cart_spec = tpl.chapter("cartilage")
    ai_rep = None
    # The 3D reconstruction in the report is rendered by the browser from the
    # cached display meshes; build them now so the PDF renderer only reads.
    yield {"stage": "mesh", "labelZh": "三维表面", "done": 1, "total": total}
    try:
        _ensure_meshes(conn, cfg, seg_id)
    except Exception as exc:                      # noqa: BLE001 - a report without 3D is still a report
        errors["mesh"] = "%s: %s" % (type(exc).__name__, exc)

    yield {"stage": "cartilage_ai", "labelZh": "软骨报告（模型撰写）", "done": 2, "total": total}
    try:
        ai_rep = generate_report(conn, cfg, seg_id,
                                 banned_terms=tpl.banned_terms_for("cartilage"))
    except Exception as exc:                 # noqa: BLE001 - recorded, not hidden
        errors[cart_spec.id] = "%s: %s" % (type(exc).__name__, exc)
        ai_rep = get_report(conn, seg_id)

    laterality = (morph.get("frame") or {}).get("laterality")
    sex_zh = {"M": "男", "F": "女", "男": "男", "女": "女"}.get(str(meta["sex"] or "").upper()[:1]
                                                             if meta and meta["sex"] else "", None)
    ctx = GenContext(
        template=tpl, morph=morph, ai_report=ai_rep,
        payload=(ai_rep or {}).get("input"),
        radiologist=rad.prefill(report_row, tpl),
        laterality=laterality, sex_zh=sex_zh,
        age_band=age_band(meta["age"]) if meta else None,
        errors=errors,
    )

    chapters = []
    for i, spec in enumerate(tpl.chapters):
        yield {"stage": "chapter", "chapter": spec.id, "labelZh": spec.title_zh,
               "done": 3 + i, "total": total}
        chapters.append(resolve(spec, ctx))
    failed = [c["id"] for c in chapters if c["status"] == "failed"]
    status = "partial" if failed else "ok"

    header = {
        "titleZh": tpl.title_zh, "disclaimerZh": tpl.disclaimer_zh,
        "lateralityZh": {"L": "左膝", "R": "右膝"}.get(laterality or "", None),
        "ageBand": ctx.age_band, "sexZh": sex_zh,
        # The study index. Not a name, not a record number; never sent anywhere.
        "caseLabel": meta["external_id"] if meta else None,
        "seriesDescription": meta["series_description"] if meta else None,
        "generatedAt": _now(), "algoVersion": morph.get("algoVersion"),
        "templateKey": tpl.key, "templateVersion": tpl.version,
    }

    prev = get_document(conn, seg_id)
    generation = (prev["generation"] + 1) if prev else 1
    overrides, dropped = _carry_overrides(prev, chapters, generation)

    conn.execute(
        "INSERT INTO report_document (segmentation_id, morphometry_id, ai_report_id,"
        " template_key, template_hash, generation, chapters_json, overrides_json,"
        " header_json, status, error, generated_at, generated_by,"
        " review_state, reviewed_by, reviewed_at, signed_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,'unreviewed',NULL,NULL,NULL)"
        " ON CONFLICT(segmentation_id) DO UPDATE SET"
        "  morphometry_id=excluded.morphometry_id, ai_report_id=excluded.ai_report_id,"
        "  template_key=excluded.template_key, template_hash=excluded.template_hash,"
        "  generation=excluded.generation, chapters_json=excluded.chapters_json,"
        "  overrides_json=excluded.overrides_json, header_json=excluded.header_json,"
        "  status=excluded.status, error=excluded.error,"
        "  generated_at=excluded.generated_at, generated_by=excluded.generated_by,"
        # A regenerated report has not been reviewed.
        "  review_state='unreviewed', reviewed_by=NULL, reviewed_at=NULL, signed_json=NULL",
        (seg_id, morph.get("id"), (ai_rep or {}).get("id"), tpl.key, tpl.hash, generation,
         json.dumps(chapters, ensure_ascii=False), json.dumps(overrides, ensure_ascii=False),
         json.dumps(header, ensure_ascii=False), status,
         "; ".join("%s: %s" % kv for kv in errors.items()) or None, actor[:120]))
    conn.commit()

    doc = get_document(conn, seg_id)
    assert doc is not None
    for ov in dropped:
        _log(conn, doc["id"], ov, "auto_drop", editor="system", generation=generation)
    yield {"stage": "done", "labelZh": "完成", "done": total, "total": total, "document": doc}


def _carry_overrides(prev: dict[str, Any] | None, chapters: list[dict[str, Any]],
                     generation: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep doctor overrides across a regeneration, marking them stale.

    An override that now equals the generated value is no longer a change and
    is dropped (logged); one whose target no longer exists is dropped too.
    """
    if not prev:
        return [], []
    kept, dropped = [], []
    for ov in prev.get("overrides") or []:
        if ov.get("revokedAt"):
            kept.append(ov)
            continue
        hit = find_target(chapters, ov["target"])
        if hit is None:
            dropped.append(ov)
            continue
        if _same(hit.get("value"), ov.get("value")):
            dropped.append(ov)
            continue
        kept.append({**ov, "stale": ov.get("generation", 0) < generation,
                     "generatedNow": hit.get("value")})
    return kept, dropped


def _same(a: Any, b: Any) -> bool:
    try:
        if isinstance(a, (int, float)) and not isinstance(a, bool):
            return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return False
    return (str(a).strip() if a is not None else None) == (str(b).strip() if b is not None else None)


# -------------------------------------------------------------- overrides

def set_override(conn: sqlite3.Connection, doc_id: int, target: dict[str, Any],
                 value: Any, editor: str, reason: str = "") -> dict[str, Any]:
    doc = get_document_by_id(conn, doc_id)
    if doc is None:
        raise KeyError("document %d" % doc_id)
    editor = (editor or "").strip()
    if not editor:
        raise ValueError("修改人不能为空")
    kind = target.get("kind")
    if kind not in ("prose", "fact", "grade", "item"):
        raise ValueError("bad override kind %r" % kind)
    hit = find_target(doc["chapters"], target)
    if hit is None or not hit.get("exists", True):
        raise ValueError("override target does not exist in this report")
    if kind == "fact":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("数值必须是数字") from None
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("数值必须有限")
    if kind == "grade" and str(value) not in GRADE_VALUES:
        raise ValueError("分级只能是 %s" % "/".join(GRADE_VALUES))
    if kind in ("prose", "item"):
        value = str(value)
        if not value.strip():
            raise ValueError("文字不能为空")

    overrides = [o for o in doc["overrides"]
                 if not (not o.get("revokedAt") and _same_target(o["target"], target))]
    ov = {
        "id": "ov_" + uuid.uuid4().hex[:8], "target": target, "kind": kind,
        "original": hit.get("value"), "value": value, "editor": editor[:120],
        "editedAt": _now(), "reason": (reason or "")[:500],
        "generation": doc["generation"], "stale": False, "revokedAt": None, "revokedBy": None,
    }
    overrides.append(ov)
    conn.execute("UPDATE report_document SET overrides_json=?, review_state='edited' WHERE id=?",
                 (json.dumps(overrides, ensure_ascii=False), doc_id))
    conn.commit()
    _log(conn, doc_id, ov, "set", editor=editor, generation=doc["generation"], reason=reason)
    out = get_document_by_id(conn, doc_id)
    assert out is not None
    return out


def revoke_override(conn: sqlite3.Connection, doc_id: int, override_id: str,
                    editor: str) -> dict[str, Any]:
    doc = get_document_by_id(conn, doc_id)
    if doc is None:
        raise KeyError("document %d" % doc_id)
    found = None
    for o in doc["overrides"]:
        if o["id"] == override_id and not o.get("revokedAt"):
            o["revokedAt"] = _now()
            o["revokedBy"] = (editor or "")[:120]
            found = o
    if found is None:
        raise ValueError("override not found")
    conn.execute("UPDATE report_document SET overrides_json=? WHERE id=?",
                 (json.dumps(doc["overrides"], ensure_ascii=False), doc_id))
    conn.commit()
    _log(conn, doc_id, found, "revoke", editor=editor, generation=doc["generation"])
    out = get_document_by_id(conn, doc_id)
    assert out is not None
    return out


def _same_target(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return (a.get("chapter"), a.get("kind"), a.get("key") or "") == \
           (b.get("chapter"), b.get("kind"), b.get("key") or "")


def _log(conn: sqlite3.Connection, doc_id: int, ov: dict[str, Any], action: str, *,
         editor: str, generation: int, reason: str = "") -> None:
    conn.execute(
        "INSERT INTO report_edit_log (document_id, override_id, action, target,"
        " original_json, value_json, editor, reason, generation)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, ov["id"], action, json.dumps(ov["target"], ensure_ascii=False),
         json.dumps(ov.get("original"), ensure_ascii=False),
         json.dumps(ov.get("value"), ensure_ascii=False),
         (editor or "")[:120], (reason or "")[:500], generation))
    conn.commit()


# ----------------------------------------------------------------- review

def review_document(conn: sqlite3.Connection, doc_id: int, state: str,
                    reviewer: str) -> dict[str, Any]:
    if state not in ("unreviewed", "approved", "rejected"):
        raise ValueError("bad review state")
    doc = get_document_by_id(conn, doc_id)
    if doc is None:
        raise KeyError("document %d" % doc_id)
    signed = json.dumps(doc["rendered"], ensure_ascii=False) if state == "approved" else None
    conn.execute(
        "UPDATE report_document SET review_state=?, reviewed_by=?, reviewed_at=datetime('now'),"
        " signed_json=? WHERE id=?",
        (state, (reviewer or "")[:120], signed, doc_id))
    conn.commit()
    out = get_document_by_id(conn, doc_id)
    assert out is not None
    return out


def _ensure_meshes(conn: sqlite3.Connection, cfg: Any, seg_id: int) -> None:
    """Build the display meshes unless the current pipeline's are cached."""
    from ..seg.mesh import MESH_PIPELINE_VERSION, build_meshes_for_segmentation
    from ..volume.cache import VolumeCache

    row = conn.execute("SELECT seg_key FROM segmentation WHERE id=?", (seg_id,)).fetchone()
    if row is None or not row["seg_key"]:
        return
    rows = conn.execute(
        "SELECT label_value, params_json FROM segmentation_mesh WHERE segmentation_id=?",
        (seg_id,)).fetchall()
    cache = VolumeCache(cfg.cache_dir)
    current = [r for r in rows
               if (json.loads(r["params_json"] or "{}").get("pipeline") == MESH_PIPELINE_VERSION
                   and cache.mesh_path(row["seg_key"], r["label_value"]).exists())]
    if not current:
        build_meshes_for_segmentation(conn, cfg, seg_id)
