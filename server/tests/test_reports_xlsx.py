"""Hospital report workbook -> `report` table.

The workbook keys studies by ``序号`` stored inconsistently as text and number,
pastes the impression into the findings cell for most rows, and its sibling
information file carries real names. Each of those is a test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import openpyxl
import pytest

from mriviewer.db.session import init_db
from mriviewer.reports import _norm_external_id, load_reports


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "index.sqlite")
    c.execute("INSERT INTO dataset(id, key, name, root_path, adapter) VALUES (1, 'ds', 'ds', '/x', 'generic')")
    for ext in ("1", "2", "3"):
        c.execute("INSERT INTO patient(dataset_id, external_id, name) VALUES (1, ?, ?)",
                  (ext, "P" + ext))
    c.commit()
    yield c
    c.close()


def _workbook(path: Path, header, rows) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


HEADER = ["序号", "性别", "年龄", "诊断意见", "影像描述"]


def test_mixed_str_and_int_ids_join_on_the_same_key(conn, tmp_path) -> None:
    path = _workbook(tmp_path / "r.xlsx", HEADER, [
        ["1", "女", 25, "左膝积液。", "左侧膝关节各组成骨皮质光整。"],
        [2, "男", 51, "右膝髌骨软化症。", "右膝髌骨软骨磨损。"],
        ["3.0", "男", 40, "同上。", "同上。"],
    ])
    stats = load_reports(conn, 1, path)
    assert stats["rows"] == 3
    ids = [r[0] for r in conn.execute(
        "SELECT external_id FROM report ORDER BY external_id").fetchall()]
    assert ids == ["1", "2", "3"]


def test_copy_paste_rows_are_flagged(conn, tmp_path) -> None:
    path = _workbook(tmp_path / "r.xlsx", HEADER, [
        ["1", "女", 25, "左膝积液。", "左侧膝关节各组成骨皮质光整。"],
        ["2", "男", 51, "右膝髌骨软化症。", "右膝髌骨软化症。"],
    ])
    load_reports(conn, 1, path)
    flagged = {r[0]: '"findingsEqualsImpression": true' in r[1] for r in conn.execute(
        "SELECT external_id, raw_json FROM report").fetchall()}
    assert flagged == {"1": False, "2": True}


def test_sex_and_age_backfill_onto_patients(conn, tmp_path) -> None:
    """The 0826 patient list has no sex; the report workbook does."""
    path = _workbook(tmp_path / "r.xlsx", HEADER, [["1", "女", 25, "a", "b"]])
    load_reports(conn, 1, path)
    row = conn.execute("SELECT sex, age FROM patient WHERE external_id='1'").fetchone()
    assert (row["sex"], row["age"]) == ("女", "25")


def test_a_name_column_is_refused(conn, tmp_path) -> None:
    """0_information.xlsx must never be pointed at this loader."""
    path = _workbook(tmp_path / "info.xlsx", ["序号", "DICOM患者姓名", "年龄"],
                     [[1, "Zhang San", 30]])
    with pytest.raises(ValueError, match="patient-name column"):
        load_reports(conn, 1, path)
    assert conn.execute("SELECT count(*) FROM report").fetchone()[0] == 0


def test_reload_is_idempotent(conn, tmp_path) -> None:
    path = _workbook(tmp_path / "r.xlsx", HEADER, [["1", "女", 25, "a", "b"]])
    load_reports(conn, 1, path)
    load_reports(conn, 1, path)
    assert conn.execute("SELECT count(*) FROM report").fetchone()[0] == 1


@pytest.mark.parametrize("raw,expect", [
    (5, "5"), (5.0, "5"), ("5", "5"), (" 5 ", "5"), ("5.0", "5"),
    ("P0728", "P0728"), (None, ""), ("", ""), (True, ""),
])
def test_external_id_normalisation(raw, expect) -> None:
    assert _norm_external_id(raw) == expect


def test_csv_path_still_works(conn, tmp_path) -> None:
    p = tmp_path / "r.csv"
    p.write_text("Anonymized_ID,性别,年龄,描述,诊断\nP1,男,40,所见,诊断\n", encoding="utf-8-sig")
    assert load_reports(conn, 1, p)["rows"] == 1
    row = conn.execute("SELECT findings, diagnosis FROM report").fetchone()
    assert (row["findings"], row["diagnosis"]) == ("所见", "诊断")
