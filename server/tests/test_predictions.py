"""Model outputs: how they get in, and what the report is allowed to say.

The platform runs no models. A model drops a labelmap into predictions_roots
and these tests pin the contract on both sides of that: the file is bound to
the series named by its SeriesInstanceUID (never guessed at), and a chapter
with no model output for it stays a placeholder rather than falling silent -
silence reads as "normal", which is the failure that matters clinically.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mriviewer.config import AiConfig
from mriviewer.db.session import init_db
from mriviewer.labels import load_labelset_files
from mriviewer.report import document as doc_mod
from mriviewer.report.template import GRADE_SCALES, load_template
from mriviewer.scan.predictions import (ingest_predictions, iter_prediction_files,
                                        model_segmentations)

REPO = Path(__file__).resolve().parents[1]
SERIES_UID = "1.2.840.113619.2.1.5.99.1"

# Two labels of the shipped meniscus set, as label_stats would report them.
STATS = {
    "voxelVolumeMm3": 0.2554,
    "labels": [
        {"value": 9, "voxels": 9000, "volumeMm3": 2299.0, "volumeCm3": 2.299,
         "centroidLps": [1.0, 2.0, 3.0], "extentMm": [30.0, 40.0, 18.0],
         "sliceRange": [4, 10]},
        {"value": 10, "voxels": 10000, "volumeMm3": 2554.0, "volumeCm3": 2.554,
         "centroidLps": [-20.0, 2.0, 3.0], "extentMm": [28.0, 38.0, 21.0],
         "sliceRange": [12, 19]},
    ],
}


@dataclass
class Cfg:
    state_dir: Path
    predictions_roots: list[Path] = field(default_factory=list)
    labelsets_dir: Path = REPO / "labelsets"
    reports_dir: Path = REPO / "reports"
    refs_dir: Path = REPO / "refs"
    ai: AiConfig = field(default_factory=AiConfig)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "index.sqlite")
    load_labelset_files(c, REPO / "labelsets")
    c.executescript("""
        INSERT INTO dataset(id, key, name, root_path, adapter) VALUES (1,'ds','ds','/x','generic');
        INSERT INTO patient(id, dataset_id, external_id, name, sex, age)
            VALUES (1, 1, '2', 'P', 'M', '29');
        INSERT INTO study(id, patient_id, path_rel) VALUES (1, 1, '2');
        INSERT INTO series(id, study_id, path_rel, description, series_uid)
            VALUES (1, 1, '2/s', 'pd_fse_sag', '%s');
        INSERT INTO segmentation(id, dataset_id, series_id, kind, source_path_rel, origin)
            VALUES (5, 1, 1, 'nifti', 'seg/2.nii', 'ground_truth');
    """ % SERIES_UID)
    c.commit()
    return c


def _drop(root: Path, series_uid: str = SERIES_UID, label_set: str = "knee_meniscus_v1",
          model: str = "meniscus_v1", version: str = "0.9") -> Path:
    d = root / model / version / series_uid
    d.mkdir(parents=True, exist_ok=True)
    f = d / (label_set + ".nii.gz")
    f.write_bytes(b"not really a nifti, never opened by the scanner")
    return f


# ---------------------------------------------------------------- the scanner

def test_layout_is_parsed_and_junk_ignored(tmp_path: Path) -> None:
    root = tmp_path / "pred"
    _drop(root)
    (root / "meniscus_v1" / "0.9" / SERIES_UID / "notes.txt").write_text("x")
    (root / "loose.nii.gz").parent.mkdir(parents=True, exist_ok=True)
    (root / "loose.nii.gz").write_bytes(b"")           # not four levels deep
    files = list(iter_prediction_files([root]))
    assert len(files) == 1
    assert (files[0].model, files[0].version, files[0].series_uid, files[0].label_set) == (
        "meniscus_v1", "0.9", SERIES_UID, "knee_meniscus_v1")


def test_prediction_binds_to_the_series_by_uid(conn, tmp_path: Path) -> None:
    root = tmp_path / "pred"
    path = _drop(root)
    stats = ingest_predictions(conn, Cfg(tmp_path, [root]))
    assert stats == {"found": 1, "ingested": 1, "unmatched": 0, "no_label_set": 0}

    row = conn.execute("SELECT * FROM segmentation WHERE origin='model'").fetchone()
    assert row["series_id"] == 1 and row["dataset_id"] == 1
    assert row["model_name"] == "meniscus_v1" and row["model_version"] == "0.9"
    assert row["association_rule"] == "predictions.series_uid"
    assert row["seg_state"] == "pending"
    # Absolute, because predictions live outside every dataset root and
    # seg.ingest resolves an absolute source_path_rel as-is.
    assert Path(row["source_path_rel"]) == path.resolve()
    assert conn.execute("SELECT key FROM label_set WHERE id=?",
                        (row["label_set_id"],)).fetchone()["key"] == "knee_meniscus_v1"
    # The ground-truth segmentation is untouched: both live on the same series.
    assert len(model_segmentations(conn, 1)) == 1
    assert conn.execute("SELECT COUNT(*) c FROM segmentation").fetchone()["c"] == 2


def test_rerunning_does_not_duplicate(conn, tmp_path: Path) -> None:
    root = tmp_path / "pred"
    _drop(root)
    cfg = Cfg(tmp_path, [root])
    ingest_predictions(conn, cfg)
    ingest_predictions(conn, cfg)
    assert conn.execute("SELECT COUNT(*) c FROM segmentation WHERE origin='model'").fetchone()["c"] == 1


def test_unknown_series_uid_is_refused_not_guessed(conn, tmp_path: Path) -> None:
    """The dataset path associates by 'the patient's first series'; this must not."""
    root = tmp_path / "pred"
    _drop(root, series_uid="1.2.3.999.not.here")
    stats = ingest_predictions(conn, Cfg(tmp_path, [root]))
    assert stats["unmatched"] == 1 and stats["ingested"] == 0
    assert not conn.execute("SELECT 1 FROM segmentation WHERE origin='model'").fetchone()
    err = conn.execute("SELECT * FROM scan_error WHERE kind='prediction'").fetchone()
    assert "1.2.3.999.not.here" in err["message"]


def test_unknown_label_set_is_refused(conn, tmp_path: Path) -> None:
    """A typo must not auto-create a label set of grey 'Label 9' entries."""
    root = tmp_path / "pred"
    _drop(root, label_set="knee_meniscus_v2")
    stats = ingest_predictions(conn, Cfg(tmp_path, [root]))
    assert stats["no_label_set"] == 1 and stats["ingested"] == 0
    err = conn.execute("SELECT * FROM scan_error WHERE kind='prediction'").fetchone()
    assert "knee_meniscus_v2" in err["message"]


def test_no_roots_configured_is_not_an_error(conn, tmp_path: Path) -> None:
    assert ingest_predictions(conn, Cfg(tmp_path, []))["found"] == 0


# ------------------------------------------------------- the report seam

def _ready_model_seg(conn: sqlite3.Connection, stats: dict | None = STATS) -> None:
    ls = conn.execute("SELECT id FROM label_set WHERE key='knee_meniscus_v1'").fetchone()["id"]
    conn.execute(
        "INSERT INTO segmentation(id, dataset_id, series_id, label_set_id, kind,"
        " source_path_rel, origin, model_name, model_version, seg_state, seg_key, stats_json)"
        " VALUES (9, 1, 1, ?, 'nifti', '/pred/m.nii.gz', 'model', 'meniscus_v1', '0.9',"
        " 'ready', 'k', ?)",
        (ls, json.dumps(stats) if stats else None))
    conn.commit()


def _structures(conn, tmp_path: Path) -> dict:
    tpl = load_template(Cfg(tmp_path), "knee_zh_v1")
    return doc_mod._structures(conn, Cfg(tmp_path), 5, tpl)


def test_structures_are_gathered_for_the_same_series(conn, tmp_path: Path) -> None:
    _ready_model_seg(conn)
    found = _structures(conn, tmp_path)
    assert set(found) == {"meniscus"}
    m = found["meniscus"]
    assert m["model"] == "meniscus_v1" and m["labelSet"] == "knee_meniscus_v1"
    assert [l["nameZh"] for l in m["labels"]] == ["内侧半月板", "外侧半月板"]
    # sliceRange [4, 10] spans 7 native slices, inclusive.
    assert m["labels"][0]["nSlices"] == 7 and m["labels"][0]["volumeCm3"] == 2.299


def test_chapter_reports_what_a_labelmap_can_support(conn, tmp_path: Path) -> None:
    from mriviewer.report.sources import GenContext, resolve
    _ready_model_seg(conn)
    tpl = load_template(Cfg(tmp_path), "knee_zh_v1")
    ctx = GenContext(template=tpl, morph={}, ai_report=None, payload=None,
                     radiologist={"byChapter": {}, "impressionSentences": [],
                                  "labelZh": "放射科报告", "noteZh": ""},
                     laterality="R", sex_zh="男", age_band="20-29",
                     structures=_structures(conn, tmp_path))
    ch = resolve(tpl.chapter("meniscus"), ctx)

    assert ch["status"] == "ok" and ch["proseOrigin"] == "template"
    assert "内侧半月板" in ch["prose"] and "2.30 cm³" in ch["prose"]
    # Measurements only. The chapter may say it did NOT assess a tear; it may
    # never describe one. So the diagnostic vocabulary is banned from the
    # descriptive sentences and allowed only in the closing disclaimer.
    described, _, disclaimed = ch["prose"].partition("本章")
    assert disclaimed, "the disclaimer clause must be there to be excluded"
    for forbidden in ("撕裂", "变性", "退变", "损伤", "正常", "未见异常"):
        assert forbidden not in described, forbidden
    assert ch["grades"] == []
    assert any("未在本院本序列上验证" in c for c in ch["caveatsZh"])
    assert ch["sourceRef"] == {"kind": "segmentation", "id": 9, "status": "ready"}
    assert {f["provenance"] for f in ch["facts"]} == {"model"}


def test_chapter_without_model_output_stays_a_placeholder(conn, tmp_path: Path) -> None:
    """The invariant that matters: no output must never render as 'normal'."""
    from mriviewer.report.sources import GenContext, resolve
    tpl = load_template(Cfg(tmp_path), "knee_zh_v1")
    ctx = GenContext(template=tpl, morph={}, ai_report=None, payload=None,
                     radiologist={"byChapter": {}, "impressionSentences": [],
                                  "labelZh": "放射科报告", "noteZh": ""},
                     laterality="R", sex_zh=None, age_band=None, structures={})
    ch = resolve(tpl.chapter("meniscus"), ctx)
    assert ch["status"] == "pending"
    assert ch["prose"] is None and ch["placeholderZh"]
    assert not any(w in (ch["placeholderZh"] or "") for w in ("未见异常", "正常", "形态可"))


def test_an_unrelated_prediction_cannot_fill_a_chapter(conn, tmp_path: Path) -> None:
    ls = conn.execute("SELECT id FROM label_set WHERE key='knee_cartilage_0826_v1'").fetchone()["id"]
    conn.execute(
        "INSERT INTO segmentation(id, dataset_id, series_id, label_set_id, kind,"
        " source_path_rel, origin, seg_state, seg_key, stats_json)"
        " VALUES (9, 1, 1, ?, 'nifti', '/pred/other.nii.gz', 'model', 'ready', 'k', ?)",
        (ls, json.dumps(STATS)))
    conn.commit()
    assert _structures(conn, tmp_path) == {}


# ------------------------------------------------- per-chapter vocabularies

def test_each_chapter_carries_its_own_grade_scale(tmp_path: Path) -> None:
    tpl = load_template(Cfg(tmp_path), "knee_zh_v1")
    assert tpl.chapter("cartilage").grade_values == GRADE_SCALES["outerbridge_mri_thickness"]
    assert tpl.chapter("meniscus").grade_values == GRADE_SCALES["stoller"]
    # Outerbridge has no grade I; Stoller does. Validating one against the
    # other is exactly the silent mistake this replaces.
    assert "I" in tpl.chapter("meniscus").grade_values
    assert "I" not in tpl.chapter("cartilage").grade_values


def test_override_validates_against_the_chapters_own_scale() -> None:
    doc = {"chapters": [{"id": "meniscus", "gradeValues": ["0", "I", "II", "III", "未评估"]},
                        {"id": "cartilage", "gradeValues": ["0", "II", "III", "IV", "未评估"]}]}
    assert "I" in doc_mod._grade_values(doc, "meniscus")
    assert "I" not in doc_mod._grade_values(doc, "cartilage")
    # A document generated before the field existed keeps Outerbridge.
    assert doc_mod._grade_values({"chapters": [{"id": "x"}]}, "x") == \
        GRADE_SCALES["outerbridge_mri_thickness"]


def test_guardrail_vocabularies_are_per_chapter() -> None:
    from mriviewer.ai.guardrail import check
    report = {"findings": "内侧半月板体部形态可。", "quant": "体积 2.30 cm³。",
              "impression": "半月板未见明显异常。", "advice": "随访。"}
    payload = {"subregions": [], "grades": [], "volume": 2.30}

    # The cartilage vocabulary forbids the word outright...
    cart = check(report, payload)
    assert not cart.ok and any("半月板" in v for v in cart.violations)
    # ...and a meniscus chapter's does not.
    men = check(report, payload, banned_terms=["软骨", "韧带"])
    assert men.ok, men.violations
    # Subregion codes are per chapter too: ccMF means nothing in a meniscus
    # chapter, and a meniscus chapter that declares no codes rejects it.
    coded = dict(report, findings="ccMF 区形态可。")
    assert not check(coded, payload, banned_terms=[], allowed_codes=set()).ok


def test_required_sections_are_per_chapter() -> None:
    from mriviewer.ai.guardrail import check
    two = {"findings": "内侧半月板形态可。", "impression": "未见明显异常。"}
    assert not check(two, {}, banned_terms=[]).ok            # cartilage wants four
    assert check(two, {}, banned_terms=[], allowed_codes=set(),
                 required_sections=("findings", "impression")).ok
