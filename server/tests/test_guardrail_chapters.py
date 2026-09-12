"""Banned terms are a property of the chapter, not of the guardrail.

The cartilage chapter must reject 半月板 (labels 1-8 cannot see a meniscus);
a future meniscus chapter fed by a meniscus model must be allowed to say it.
Same function, different list.
"""
from __future__ import annotations

from mriviewer.ai.guardrail import BANNED_TERMS, check

PAYLOAD = {
    "compartments": {"asymmetryPct": -12.8},
    "subregions": [{"code": "ccMF", "meanThicknessMm": 2.30}],
    "quality": {"reliableAreaPct": 60.0},
}


def _report(impression: str) -> dict:
    return {
        "findings": "可靠面积占 60%。",
        "quant": "不对称度 -12.8%。",
        "impression": impression,
        "advice": "建议医师复核。",
        "metricRefs": ["ccMF"],
    }


def test_default_list_is_the_cartilage_list() -> None:
    r = check(_report("另见半月板异常。"), PAYLOAD)
    assert not r.ok and "半月板" in r.banned_hits


def test_a_chapter_may_allow_what_another_forbids() -> None:
    meniscus_terms = [t for t in BANNED_TERMS if t not in ("半月板", "撕裂")]
    ok = check(_report("外侧半月板体部撕裂。"), PAYLOAD, banned_terms=meniscus_terms)
    assert ok.ok, ok.violations


def test_explicit_empty_list_disables_term_checks_only() -> None:
    """Numbers are still traced back even when no terms are banned."""
    r = check(_report("半月板厚度 2.13 mm。"), PAYLOAD, banned_terms=[])
    assert not r.ok
    assert not r.banned_hits
    assert 2.13 in r.unmatched_numbers


def test_global_terms_can_be_combined_with_chapter_terms() -> None:
    r = check(_report("确诊骨关节炎。"), PAYLOAD, banned_terms=["确诊"])
    assert not r.ok and r.banned_hits == ["确诊"]
