"""The chaptered document: what it must never say, and what survives an edit.

Morphometry and the cartilage LLM are monkeypatched with fixed outputs so the
tests exercise composition, placeholders, overrides and review - not the
numerics, which have their own suites.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mriviewer.config import AiConfig
from mriviewer.db.session import init_db
from mriviewer.report import document as doc_mod

REPO = Path(__file__).resolve().parents[1]


@dataclass
class Cfg:
    state_dir: Path
    reports_dir: Path = REPO / "reports"
    refs_dir: Path = REPO / "refs"
    ai: AiConfig = field(default_factory=AiConfig)


MORPH = {
    "id": 17, "state": "ready", "algoVersion": "morph-0.1.0",
    "frame": {"laterality": "R", "medial_lateral_consistent": True, "notes": []},
    "metrics": {"plates": [], "compartments": {}},
    "qc": {},
}

PAYLOAD = {
    "patientContext": {"ageBand": "20-29", "sex": "男", "laterality": "右膝"},
    "acquisition": {"sliceThicknessMm": 3.0, "inPlaneMm": 0.2917},
    "compartments": {
        "medial": {"femoralThicknessMm": 2.22, "tibialThicknessMm": 2.09,
                   "sumThicknessMm": 4.30, "volumeCm3": 8.05},
        "lateral": {"femoralThicknessMm": 2.29, "tibialThicknessMm": 2.61,
                    "sumThicknessMm": 4.89, "volumeCm3": 11.1},
        "asymmetryPct": -12.8,
    },
    "subregions": [{"code": "ccMF", "labelZh": "股骨内侧髁中央-中央带", "meanThicknessMm": 2.30}],
    "quality": {"reliableAreaPct": 60.0, "effectiveResolutionMm": 0.74},
    "grades": [{"code": "ccMF", "labelZh": "股骨内侧髁中央-中央带", "grade": "II",
                "confidence": "medium", "lesionAreaMm2": 38.2, "deficitPct": 34,
                "baselineMm": 2.31}],
}

AI_REPORT = {
    "id": 42, "status": "ok", "input": PAYLOAD,
    "findings": "本次分析覆盖右膝软骨。", "quant": "内侧间室股胫合计 4.30 mm。",
    "impression": "内侧间室软骨相对偏薄。\n受层厚限制存在系统性低估。",
    "advice": "建议医师复核。\n建议加扫各向同性序列。",
}

RADIOLOGIST_FINDINGS = ("右侧膝关节各组成骨皮质光整。所示关节软骨面光整。"
                        "髌上囊少量积液。膝关节内外侧半月板形态可，信号未见明显异常。"
                        "髌韧带、前后交叉韧带形态信号无异常。膝关节周围软组织信号无异常。")
RADIOLOGIST_IMPRESSION = "右膝关节腔少量积液，腘窝囊肿。请结合临床其他检查，随访。"


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "index.sqlite")
    c.executescript("""
        INSERT INTO dataset(id, key, name, root_path, adapter) VALUES (1,'ds','ds','/x','generic');
        INSERT INTO patient(id, dataset_id, external_id, name, sex, age)
            VALUES (1, 1, '2', 'P', 'M', '29');
        INSERT INTO study(id, patient_id, path_rel) VALUES (1, 1, '2');
        INSERT INTO series(id, study_id, path_rel, description) VALUES (1, 1, '2/s', 'pd_fse_sag');
        INSERT INTO segmentation(id, dataset_id, series_id, kind, source_path_rel, origin)
            VALUES (5, 1, 1, 'nifti', 'seg/2.nii', 'ground_truth');
        INSERT INTO segmentation_morphometry(id, segmentation_id, algo_version, params_hash, params_json, state)
            VALUES (17, 5, 'morph-0.1.0', 'h', '{}', 'ready');
        INSERT INTO ai_report(id, segmentation_id, status) VALUES (42, 5, 'ok');
    """)
    c.commit()
    yield c
    c.close()


@pytest.fixture()
def cfg(tmp_path: Path) -> Cfg:
    return Cfg(state_dir=tmp_path)


@pytest.fixture()
def patched(monkeypatch):
    import mriviewer.ai.service as ai_service
    import mriviewer.morph.service as morph_service
    monkeypatch.setattr(morph_service, "compute_and_store", lambda conn, cfg, seg_id, **kw: MORPH)
    monkeypatch.setattr(ai_service, "generate_report", lambda conn, cfg, seg_id, **kw: dict(AI_REPORT))
    monkeypatch.setattr(ai_service, "get_report", lambda conn, seg_id: dict(AI_REPORT))


def _with_radiologist(conn, findings=RADIOLOGIST_FINDINGS, impression=RADIOLOGIST_IMPRESSION):
    conn.execute("INSERT INTO report(dataset_id, external_id, findings, diagnosis) VALUES (1,'2',?,?)",
                 (findings, impression))
    conn.commit()


# ------------------------------------------------------------ composition

def test_chapters_follow_the_template_order(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    assert [c["id"] for c in d["chapters"]][:3] == ["overview", "bone", "cartilage"]
    assert d["status"] == "ok" and d["generation"] == 1


def test_pending_chapters_have_placeholder_and_no_prose(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    for c in d["chapters"]:
        if c["source"] == "pending":
            assert c["status"] == "pending"
            assert c["prose"] is None
            assert "未评估" in c["placeholderZh"]


def test_nothing_asserts_normality_without_a_source(conn, cfg, patched) -> None:
    """Without a radiologist report, no pending chapter may say the anatomy is fine."""
    d = doc_mod.generate_document(conn, cfg, 5)
    for c in d["chapters"]:
        if c["source"] == "pending":
            text = " ".join(filter(None, [c["prose"], c["placeholderZh"]]))
            for w in ("未见异常", "正常", "光整", "形态可"):
                assert w not in text, (c["id"], w)
            assert c["radiologist"] is None


def test_radiologist_sentences_are_labelled_not_attributed(conn, cfg, patched) -> None:
    _with_radiologist(conn)
    d = doc_mod.generate_document(conn, cfg, 5)
    by_id = {c["id"]: c for c in d["chapters"]}
    men = by_id["meniscus"]
    assert men["prose"] is None                               # still not "our" finding
    assert men["radiologist"]["labelZh"] == "放射科报告"
    assert any("半月板" in s for s in men["radiologist"]["sentences"])
    assert "非本平台计算结果" in men["radiologist"]["note"]


def test_copy_paste_report_only_feeds_the_impression(conn, cfg, patched) -> None:
    _with_radiologist(conn, findings=RADIOLOGIST_IMPRESSION)
    d = doc_mod.generate_document(conn, cfg, 5)
    by_id = {c["id"]: c for c in d["chapters"]}
    assert all(by_id[i]["radiologist"] is None for i in ("bone", "meniscus", "soft_tissue"))
    origins = {it["origin"] for it in by_id["impression"]["items"]}
    assert "radiologist" in origins


def test_cartilage_chapter_takes_facts_from_morphometry_not_prose(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    cart = next(c for c in d["chapters"] if c["id"] == "cartilage")
    keys = {f["key"]: f["value"] for f in cart["facts"]}
    assert keys["compartments.asymmetryPct"] == -12.8
    assert keys["sub.ccMF.meanThicknessMm"] == 2.30
    assert cart["proseOrigin"] == "llm"
    assert cart["sourceRef"] == {"kind": "ai_report", "id": 42, "status": "ok"}


def test_impression_is_severity_first_and_ends_with_the_tail(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    imp = next(c for c in d["chapters"] if c["id"] == "impression")
    sev = [it["severity"] for it in imp["items"]]
    assert sev == sorted(sev, reverse=True)
    assert imp["items"][-1]["text"].startswith("请结合临床")


def test_a_failed_cartilage_chapter_is_marked_partial_not_normal(conn, cfg, patched, monkeypatch) -> None:
    import mriviewer.ai.service as ai_service

    def boom(conn, cfg, seg_id, **kw):
        raise RuntimeError("model down")
    monkeypatch.setattr(ai_service, "generate_report", boom)
    monkeypatch.setattr(ai_service, "get_report", lambda conn, seg_id: None)
    d = doc_mod.generate_document(conn, cfg, 5)
    cart = next(c for c in d["chapters"] if c["id"] == "cartilage")
    assert d["status"] == "partial"
    assert cart["status"] == "failed" and cart["prose"] is None
    assert "model down" in d["error"]


def test_header_is_de_identified(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    h = d["header"]
    assert h["ageBand"] == "20-29" and h["sexZh"] == "男" and h["lateralityZh"] == "右膝"
    assert h["caseLabel"] == "2"
    assert "P" not in str(h.values())            # no patient name anywhere


def test_state_reports_chapter_coverage(conn, cfg, patched) -> None:
    assert doc_mod.document_state(conn, 5) == {"exists": False}
    doc_mod.generate_document(conn, cfg, 5)
    st = doc_mod.document_state(conn, 5)
    assert st["exists"] and st["findingsChaptersWithData"] == 2      # overview + cartilage
    assert st["findingsChapters"] == 8


# -------------------------------------------------------------- overrides

def test_override_renders_with_original_and_editor(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    out = doc_mod.set_override(conn, d["id"],
                               {"chapter": "cartilage", "kind": "fact", "key": "compartments.asymmetryPct"},
                               -8.0, "张医生", "结合力线片")
    cart = next(c for c in out["rendered"]["chapters"] if c["id"] == "cartilage")
    f = next(x for x in cart["facts"] if x["key"] == "compartments.asymmetryPct")
    assert f["value"] == -12.8 and f["valueEffective"] == -8.0
    assert f["override"]["editor"] == "张医生" and f["override"]["original"] == -12.8
    assert f["override"]["stale"] is False
    assert out["reviewState"] == "edited"


def test_override_needs_an_editor_and_a_real_target(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    with pytest.raises(ValueError, match="修改人"):
        doc_mod.set_override(conn, d["id"], {"chapter": "cartilage", "kind": "prose", "key": ""}, "x", "")
    with pytest.raises(ValueError, match="target"):
        doc_mod.set_override(conn, d["id"], {"chapter": "cartilage", "kind": "fact", "key": "nope"}, 1, "a")
    with pytest.raises(ValueError, match="分级"):
        doc_mod.set_override(conn, d["id"], {"chapter": "cartilage", "kind": "grade", "key": "ccMF"}, "V", "a")


def test_override_survives_regeneration_flagged_stale(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    doc_mod.set_override(conn, d["id"], {"chapter": "cartilage", "kind": "prose", "key": ""},
                         "医生改写的软骨描述。", "张医生")
    d2 = doc_mod.generate_document(conn, cfg, 5)
    assert d2["generation"] == 2
    cart = next(c for c in d2["rendered"]["chapters"] if c["id"] == "cartilage")
    assert cart["proseEffective"] == "医生改写的软骨描述。"
    assert cart["proseOverride"]["stale"] is True
    assert "请复核" in cart["proseOverride"]["noteZh"]
    assert d2["reviewState"] == "unreviewed"                 # approval never carries over


def test_override_equal_to_regenerated_value_is_dropped_and_logged(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    doc_mod.set_override(conn, d["id"],
                         {"chapter": "cartilage", "kind": "fact", "key": "compartments.asymmetryPct"},
                         -12.8, "张医生")
    d2 = doc_mod.generate_document(conn, cfg, 5)
    assert [o for o in d2["overrides"] if not o.get("revokedAt")] == []
    actions = [r[0] for r in conn.execute(
        "SELECT action FROM report_edit_log WHERE document_id=? ORDER BY id", (d["id"],)).fetchall()]
    assert actions == ["set", "auto_drop"]


def test_revoke_restores_the_generated_value(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    out = doc_mod.set_override(conn, d["id"], {"chapter": "cartilage", "kind": "prose", "key": ""},
                               "改写。", "张医生")
    ov_id = out["overrides"][-1]["id"]
    out2 = doc_mod.revoke_override(conn, d["id"], ov_id, "张医生")
    cart = next(c for c in out2["rendered"]["chapters"] if c["id"] == "cartilage")
    assert cart["proseOverride"] is None
    assert cart["proseEffective"] == cart["prose"]


# ----------------------------------------------------------------- review

def test_approval_freezes_the_rendered_view(conn, cfg, patched) -> None:
    d = doc_mod.generate_document(conn, cfg, 5)
    out = doc_mod.review_document(conn, d["id"], "approved", "李医生")
    assert out["reviewState"] == "approved" and out["reviewedBy"] == "李医生"
    assert out["signed"]["chapters"][0]["id"] == "overview"
    d2 = doc_mod.generate_document(conn, cfg, 5)
    assert d2["reviewState"] == "unreviewed" and d2["signed"] is None
