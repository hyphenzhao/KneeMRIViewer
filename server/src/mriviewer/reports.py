"""De-identified radiology reports -> SQLite.

``Knee_MR_Anonymized_CHANGZHENG/reports_deidentified.csv`` carries one row per
patient keyed by ``Anonymized_ID``, which is exactly the patient directory name
(``P0728``). Columns are Chinese; several are empty in the de-identified export.

The findings/diagnosis text is what makes 2485 unlabelled knees searchable.
Search is a LIKE scan, not FTS5 - see `search()` for why.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .db.session import upsert

# Source column -> our column. Several spellings are tolerated because the two
# exports on this drive do not agree on all of them.
COLUMN_MAP: dict[str, tuple[str, ...]] = {
    "external_id": ("Anonymized_ID", "anonymized_id", "ID", "编号"),
    "age": ("年龄", "age"),
    "sex": ("性别", "sex"),
    "exam_time": ("检查时间", "检查日期", "exam_time"),
    "department": ("申请科室", "科室", "department"),
    "body_part": ("检查部位,检查方法", "检查部位", "body_part"),
    "findings": ("描述", "影像所见", "findings"),
    "diagnosis": ("诊断", "诊断意见", "diagnosis"),
    "suggestion": ("建议", "suggestion"),
}


def _resolve(header: Iterable[str]) -> dict[str, str]:
    """Map our column names onto whichever header spelling this file uses."""
    present = {h.strip(): h for h in header}
    out: dict[str, str] = {}
    for ours, candidates in COLUMN_MAP.items():
        for c in candidates:
            if c in present:
                out[ours] = present[c]
                break
    return out


def load_reports(conn: sqlite3.Connection, dataset_id: int, csv_path: Path) -> dict[str, int]:
    """Ingest a report CSV. Idempotent - re-running updates in place."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return {"rows": 0, "skipped": 0, "missing_file": 1}

    stats = {"rows": 0, "skipped": 0, "missing_file": 0}
    # These exports are UTF-8 with a BOM more often than not.
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return {"rows": 0, "skipped": 0, "missing_file": 0}
        cols = _resolve(reader.fieldnames)
        if "external_id" not in cols:
            raise ValueError(
                "no patient id column in %s (saw %s)"
                % (csv_path.name, ", ".join(reader.fieldnames[:8]))
            )

        for row in reader:
            ext = (row.get(cols["external_id"]) or "").strip()
            if not ext:
                stats["skipped"] += 1
                continue

            def val(key: str) -> str | None:
                src = cols.get(key)
                if not src:
                    return None
                v = (row.get(src) or "").strip()
                return v or None

            upsert(conn, "report", ["dataset_id", "external_id"], {
                "dataset_id": dataset_id,
                "external_id": ext,
                "age": val("age"),
                "sex": val("sex"),
                "exam_time": val("exam_time"),
                "department": val("department"),
                "body_part": val("body_part"),
                "findings": val("findings"),
                "diagnosis": val("diagnosis"),
                "suggestion": val("suggestion"),
                "raw_json": json.dumps(
                    {k: v for k, v in row.items() if v and v.strip()}, ensure_ascii=False
                ),
            })

            stats["rows"] += 1

    # Backfill age/sex onto patients that carry none from the DICOM headers.
    conn.execute(
        "UPDATE patient SET"
        " age = COALESCE(age, (SELECT r.age FROM report r"
        "   WHERE r.dataset_id=patient.dataset_id AND r.external_id=patient.external_id)),"
        " sex = COALESCE(sex, (SELECT r.sex FROM report r"
        "   WHERE r.dataset_id=patient.dataset_id AND r.external_id=patient.external_id))"
        " WHERE dataset_id=?",
        (dataset_id,),
    )
    conn.commit()
    return stats


def search(conn: sqlite3.Connection, query: str, dataset_id: int | None = None,
           limit: int = 100) -> list[dict[str, Any]]:
    """Substring search over findings / diagnosis / suggestion.

    A LIKE scan rather than FTS5 because the corpus is Chinese: unicode61 turns
    an unbroken run of Chinese into one token, so 软骨 finds nothing inside
    「股骨软骨变薄」. 2485 rows of ~500 characters scans in well under a
    millisecond, and it is correct.
    """
    like = "%" + query.strip() + "%"
    sql = (
        "SELECT external_id, age, sex, body_part, findings, diagnosis, suggestion"
        " FROM report WHERE (findings LIKE ? OR diagnosis LIKE ? OR suggestion LIKE ?)"
    )
    params: list[Any] = [like, like, like]
    if dataset_id is not None:
        sql += " AND dataset_id=?"
        params.append(dataset_id)
    sql += " ORDER BY external_id LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    q = query.strip()
    for r in rows:
        # A short excerpt around the first hit, so the caller can show context.
        for field in ("diagnosis", "findings", "suggestion"):
            text = r.get(field) or ""
            i = text.find(q)
            if i >= 0:
                lo, hi = max(0, i - 20), min(len(text), i + len(q) + 30)
                body = " ".join(text[lo:hi].split())
                r["excerpt"] = ("…" if lo else "") + body + ("…" if hi < len(text) else "")
                r["excerptField"] = field
                break
        else:
            r["excerpt"] = (r.get("diagnosis") or "")[:60]
            r["excerptField"] = "diagnosis"
    return rows


def count_matches(conn: sqlite3.Connection, query: str,
                  dataset_id: int | None = None) -> int:
    like = "%" + query.strip() + "%"
    sql = ("SELECT COUNT(*) FROM report"
           " WHERE (findings LIKE ? OR diagnosis LIKE ? OR suggestion LIKE ?)")
    params: list[Any] = [like, like, like]
    if dataset_id is not None:
        sql += " AND dataset_id=?"
        params.append(dataset_id)
    return int(conn.execute(sql, params).fetchone()[0])
