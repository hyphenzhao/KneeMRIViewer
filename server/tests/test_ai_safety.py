"""The two checks that decide whether this feature is safe to switch on.

1. Nothing identifying can reach a third party.
2. Nothing the measurements do not support can reach a clinician.

Both are tested with deliberately poisoned inputs, because a test that only
feeds clean data proves nothing about a guard.
"""
from __future__ import annotations

import pytest

from mriviewer.ai.client import EgressBlocked, check_egress
from mriviewer.ai.fallback import render_template_report
from mriviewer.ai.guardrail import check, collect_numbers
from mriviewer.ai.payload import (DeidentificationError, age_band,
                                  assert_deidentified, build_payload)

MORPH = {
    "frame": {"laterality": "R", "medial_lateral_consistent": True, "notes": []},
    "metrics": {
        "plates": [{
            "label": 4, "name": "股骨内侧软骨 MFC", "volume_mm3": 5391.0,
            "interface_area_mm2": 2258.0,
            "thickness": {"mean": 2.22, "p10": 1.30, "p90": 2.97, "max": 4.1,
                          "n": 100, "areaMm2": 2258.0},
            "histogram": {"binMm": 0.1, "edges": [], "areaMm2": []},
            "subregions": {
                "ccMF": {"moaks": "cMF", "labelZh": "股骨内侧髁中央-中央带",
                         "thicknessMm": {"mean": 2.30, "p10": 1.76, "p90": 2.92,
                                         "max": 3.3, "n": 50, "areaMm2": 400.0},
                         "sliceSupport": 5, "reliableAreaFraction": 0.71,
                         "effectiveResolutionMm": 0.55, "confidence": "medium"},
            },
            "qc": {"escapedFraction": 0.012, "rayVsNnMedianRatio": 1.02,
                   "reliableAreaFraction": 0.59, "effectiveResolutionMm": 0.72,
                   "isoMm": 0.5, "notes": []},
        }],
        "compartments": {
            "medial": {"femoralThicknessMm": 2.22, "tibialThicknessMm": 2.09,
                       "sumThicknessMm": 4.30, "volumeMm3": 8050.0},
            "lateral": {"femoralThicknessMm": 2.29, "tibialThicknessMm": 2.61,
                        "sumThicknessMm": 4.89, "volumeMm3": 11100.0},
            "asymmetryPct": -12.8, "medialLateralRatio": 0.879,
        },
    },
    "qc": {"reliableAreaFraction": 0.60, "effectiveResolutionMm": 0.74,
           "escapedFraction": 0.016, "medialLateralConsistent": True,
           "frameNotes": [],
           "subregionConfidence": {"high": 10, "medium": 10, "low": 2}},
}

REFS = {
    "subregions": {"ccMF": {"label_zh": "股骨内侧髁中央-中央带",
                            "male": [2.54, 0.44], "female": [2.05, 0.37]}},
    "reference_source": {"applicability": "indirect"},
    "thresholds": {"reliable_resolution_mm": 0.6, "min_slices_for_confidence": 3},
    "measurement_caveat": {"zh": "层厚 3 mm 导致系统性低估。",
                           "implications_zh": ["绝对值不可直接比较。"]},
}


def _payload():
    return build_payload(MORPH, REFS, sex="M", age=67, laterality="R",
                         patient_context=["ageBand", "sex", "laterality"])


# --------------------------------------------------------------- payload

def test_age_is_reduced_to_a_decade() -> None:
    assert age_band(67) == "60-69"
    assert age_band(None) is None
    assert _payload()["patientContext"]["ageBand"] == "60-69"


def test_payload_carries_the_numbers_the_report_needs() -> None:
    p = _payload()
    assert p["compartments"]["asymmetryPct"] == -12.8
    assert p["subregions"][0]["code"] == "ccMF"
    assert p["subregions"][0]["referenceMeanMm"] == 2.54


@pytest.mark.parametrize("poison", [
    {"patientName": "Zhang San"},
    {"external_id": "0826-1"},
    {"seriesId": 9219},
    {"note": "StudyInstanceUID 1.2.840.113619.2.55.3.1234"},
    {"note": "检查日期 2025-08-28"},
    {"note": "扫描于 20250828 完成"},
    {"note": "/media/haifeng/Elements/0826/dicom/1"},
    {"note": "联系电话 13812345678"},
    {"nested": [{"deep": {"accession": "A12345"}}]},
])
def test_poisoned_payloads_are_refused(poison: dict) -> None:
    """Each of these must raise BEFORE any socket is opened."""
    with pytest.raises(DeidentificationError):
        assert_deidentified({**_payload(), **poison})


def test_a_clean_payload_passes() -> None:
    assert_deidentified(_payload())          # must not raise


def test_report_text_never_reaches_the_payload() -> None:
    """The radiologist free-text report is the richest identifier we hold."""
    with pytest.raises(DeidentificationError):
        assert_deidentified({**_payload(), "report": "患者主诉右膝疼痛三年"})


# -------------------------------------------------------------- guardrail

def _good_report():
    return {
        "findings": "本次分析的骨-软骨界面中，可靠面积占 60%。",
        "quant": "内侧间室股胫合计 4.30 mm，外侧 4.89 mm，不对称度 -12.8%。",
        "impression": "内侧间室相对偏薄。受层厚限制存在系统性低估。",
        "advice": "建议医师复核。\n建议加扫各向同性序列。",
        "metricRefs": ["ccMF"],
    }


def test_a_faithful_report_passes() -> None:
    assert check(_good_report(), _payload()).ok


def test_an_invented_number_rejects_the_whole_report() -> None:
    """The failure mode this feature exists to prevent."""
    bad = {**_good_report(), "quant": "内侧间室平均软骨厚度 2.13 mm。"}
    result = check(bad, _payload())
    assert not result.ok
    assert 2.13 in result.unmatched_numbers


@pytest.mark.parametrize("term", ["半月板", "骨髓水肿", "积液", "撕裂"])
def test_structures_we_never_segmented_are_rejected(term: str) -> None:
    bad = {**_good_report(), "impression": "另见%s异常。" % term}
    result = check(bad, _payload())
    assert not result.ok
    assert result.banned_hits


def test_unknown_subregion_codes_are_rejected() -> None:
    bad = {**_good_report(), "metricRefs": ["ccMF", "xxYY"]}
    assert not check(bad, _payload()).ok


def test_a_missing_section_is_rejected() -> None:
    bad = {**_good_report(), "impression": ""}
    assert not check(bad, _payload()).ok


def test_numbers_quoted_from_the_fixed_caveat_are_not_flagged() -> None:
    """3 mm comes from our own limitations text, not from the model."""
    ok = {**_good_report(), "impression": "层厚 3 mm，存在系统性低估。"}
    assert check(ok, _payload()).ok


def test_collect_numbers_reaches_nested_values() -> None:
    assert 2.54 in collect_numbers(_payload())


# --------------------------------------------------------------- fallback

def test_fallback_report_has_all_four_sections() -> None:
    r = render_template_report(_payload())
    for key in ("findings", "quant", "impression", "advice"):
        assert r[key].strip(), key


def test_fallback_report_passes_its_own_guardrail() -> None:
    """The offline path must satisfy the same check as the model output."""
    p = _payload()
    assert check(render_template_report(p), p).ok


def test_fallback_states_the_asymmetry_it_was_given() -> None:
    assert "-12.8" in render_template_report(_payload())["quant"]


def test_fallback_quotes_the_caveat_rather_than_restating_it() -> None:
    """The limitation wording lives in the YAML; duplicating it lets it drift."""
    p = _payload()
    assert p["limitations"]["zh"] in render_template_report(p)["impression"]


def test_fallback_states_a_count_the_payload_supports() -> None:
    """"共测量 N 个亚区" must match the payload, and derived_numbers knows it."""
    p = _payload()
    assert str(len(p["subregions"])) in render_template_report(p)["findings"]


# ----------------------------------------------------------------- egress

def test_public_host_is_blocked_without_permission() -> None:
    with pytest.raises(EgressBlocked):
        check_egress("https://api.deepseek.com/v1", allow_egress=False)


def test_public_host_is_allowed_when_permitted() -> None:
    check_egress("https://api.deepseek.com/v1", allow_egress=True)


def test_localhost_needs_no_egress_permission() -> None:
    """An air-gapped box pointing at a local model must work with egress off."""
    check_egress("http://127.0.0.1:11434/v1", allow_egress=False)


def test_the_prompt_says_json_because_deepseek_requires_it() -> None:
    """response_format=json_object is rejected unless the prompt contains json."""
    from pathlib import Path

    # tests/ -> server/ -> prompts/. Resolving via the installed package points
    # into site-packages, where the prompt (a data file doctors edit) does not live.
    prompt = Path(__file__).resolve().parents[1] / "prompts" / "knee_zh_v1.md"
    if not prompt.is_file():
        pytest.skip("prompts/ not present in this install layout")
    assert "json" in prompt.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("raw,expect", [
    ("29", "20-29"), ("067Y", "60-69"), (45, "40-49"),
    ("", None), ("unknown", None), (None, None), (999, None),
])
def test_age_band_accepts_what_the_database_actually_holds(raw, expect) -> None:
    """DICOM ages arrive as strings like '067Y'; the column is TEXT."""
    assert age_band(raw) == expect
