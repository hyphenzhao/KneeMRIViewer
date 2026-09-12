"""De-identified radiology reports -> SQLite.

Two shapes are accepted:

* a CSV export (one row per patient keyed by an anonymised id that is also the
  patient directory name), and
* an Excel workbook such as ``knee_MR_report.xlsx`` - one row per knee study
  keyed by ``序号``, which is the same integer as the DICOM folder name and the
  segmentation label file. Its ``序号`` column is stored as text in some rows
  and as a number in others, so ids are normalised before joining.

What is deliberately NOT loaded: a patient-name column. The hospital's
``0_information.xlsx`` lists real names; names come only from the DICOM
headers via the dataset adapter, and a report file that carries a name column
is refused rather than partially read.

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
    "external_id": ("Anonymized_ID", "anonymized_id", "ID", "编号", "序号"),
    "age": ("年龄", "age"),
    "sex": ("性别", "sex"),
    "exam_time": ("检查时间", "检查日期", "exam_time"),
    "department": ("申请科室", "科室", "department"),
    "body_part": ("检查部位,检查方法", "检查部位", "body_part"),
    "findings": ("描述", "影像所见", "影像描述", "findings"),
    "diagnosis": ("诊断", "诊断意见", "diagnosis"),
    "suggestion": ("建议", "suggestion"),
}


# Headers that identify a person. Their presence aborts the load: silently
# skipping the column would still leave the file in a place the platform reads.
NAME_COLUMNS = {"DICOM患者姓名", "患者姓名", "姓名", "name", "patient_name", "PatientName"}


def _norm_external_id(value: Any) -> str:
    """``5``, ``5.0`` and ``" 5 "`` are the same study. Excel does not agree."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _iter_xlsx(path: Path):
    """(fieldnames, rows-as-dicts) from the first sheet of a workbook."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if not header:
            return [], []
        names = [str(h).strip() if h is not None else "" for h in header]
        rows = []
        for values in it:
            if values is None or all(v is None or str(v).strip() == "" for v in values):
                continue
            rows.append({n: v for n, v in zip(names, values) if n})
        return names, rows
    finally:
        wb.close()


def _iter_csv(path: Path):
    # These exports are UTF-8 with a BOM more often than not.
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        names = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]
    return names, rows


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
    """Ingest a report file (CSV or XLSX). Idempotent - re-running updates in place."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return {"rows": 0, "skipped": 0, "missing_file": 1}

    stats = {"rows": 0, "skipped": 0, "missing_file": 0}
    if csv_path.suffix.lower() in (".xlsx", ".xlsm"):
        fieldnames, rows = _iter_xlsx(csv_path)
    else:
        fieldnames, rows = _iter_csv(csv_path)
    if not fieldnames:
        return stats

    offending = [h for h in fieldnames if h.strip() in NAME_COLUMNS]
    if offending:
        raise ValueError(
            "refusing to ingest %s: it carries a patient-name column (%s)"
            % (csv_path.name, ", ".join(offending)))

    cols = _resolve(fieldnames)
    if "external_id" not in cols:
        raise ValueError(
            "no patient id column in %s (saw %s)"
            % (csv_path.name, ", ".join(fieldnames[:8]))
        )

    for row in rows:
        ext = _norm_external_id(row.get(cols["external_id"]))
        if not ext:
            stats["skipped"] += 1
            continue

        def val(key: str) -> str | None:
            src = cols.get(key)
            if not src:
                return None
            v = row.get(src)
            if v is None:
                return None
            v = str(v).strip()
            return v or None

        findings, diagnosis = val("findings"), val("diagnosis")
        raw = {k: str(v).strip() for k, v in row.items() if v is not None and str(v).strip()}
        # 88 of 157 rows in the sample workbook have the findings cell pasted
        # from the impression. Flag it so downstream never treats that text as
        # an independent description of the images.
        if findings and diagnosis and findings == diagnosis:
            raw["findingsEqualsImpression"] = True

        upsert(conn, "report", ["dataset_id", "external_id"], {
            "dataset_id": dataset_id,
            "external_id": ext,
            "age": val("age"),
            "sex": val("sex"),
            "exam_time": val("exam_time"),
            "department": val("department"),
            "body_part": val("body_part"),
            "findings": findings,
            "diagnosis": diagnosis,
            "suggestion": val("suggestion"),
            "raw_json": json.dumps(raw, ensure_ascii=False),
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
