"""Reject a report that says anything the measurements do not support.

The dangerous failure here is not a refusal or a crash - it is a fluent Chinese
paragraph containing a plausible number that no measurement produced. So the
central check is **numeric traceback**: every number in the prose must be
findable in the payload. A report that invents "2.1 mm" is rejected whole.

The model is never asked to grade, classify, or compute. Python does that and
passes the results in; the model writes sentences around them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .payload import ALLOWED_SUBREGIONS

# Structures that labels 1-8 cannot support a statement about. A report that
# mentions a meniscus tear is not slightly wrong, it is fabricated.
BANNED_TERMS = [
    "半月板", "韧带", "交叉韧带", "前交叉", "后交叉", "侧副韧带",
    "骨髓水肿", "骨髓", "积液", "滑膜炎", "囊肿", "腘窝囊肿",
    "撕裂", "骨折", "骨赘", "游离体", "滑膜", "肌腱", "髌腱",
]

# Tolerances for matching a number in the prose back to the payload.
#
# Tight on purpose. These exist to absorb *rounding* - a model writing "2.1 mm"
# for a measured 2.09 - and nothing else. An earlier value of 0.051 let a
# fabricated "2.13 mm" match a real 2.09, which is precisely the failure this
# check is for.
MM_TOLERANCE = 0.015
PCT_TOLERANCE = 1.01

REQUIRED_SECTIONS = ("findings", "quant", "impression", "advice")

# "II 级", "Outerbridge III级", "3级" - the ways a grade gets written. Roman
# numerals I..IV and Arabic 0..4 both count.
_GRADE = re.compile(r"(?:Outerbridge\s*)?\b(0|I{1,3}|IV|[1-4])\s*级")
_ROMAN = {"0": "0", "I": "I", "II": "II", "III": "III", "IV": "IV",
          "1": "I", "2": "II", "3": "III", "4": "IV"}

# A leading '-' counts as a minus sign only when it is not between digits:
# "60-69" and "7-18%" are ranges in our own wording, not negative numbers.
_NUMBER = re.compile(r"(?<![\d.])(-?)(\d+(?:\.\d+)?)")


@dataclass
class GuardrailResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    unmatched_numbers: list[float] = field(default_factory=list)
    banned_hits: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "violations": self.violations,
            "unmatchedNumbers": self.unmatched_numbers,
            "bannedHits": self.banned_hits,
        }


def collect_numbers(payload: Any, out: list[float] | None = None) -> list[float]:
    """Every numeric value anywhere in the payload, flattened."""
    if out is None:
        out = []
    if isinstance(payload, bool):
        return out
    if isinstance(payload, (int, float)):
        out.append(float(payload))
    elif isinstance(payload, str):
        # Numbers inside payload strings count as known. The limitations text
        # we supply says "3 mm" and "7-18%"; a report quoting it back is
        # faithful, and flagging that would make the check useless.
        for m in _NUMBER.finditer(payload):
            out.append(float(m.group(1) + m.group(2)))
    elif isinstance(payload, dict):
        for v in payload.values():
            collect_numbers(v, out)
    elif isinstance(payload, (list, tuple)):
        for v in payload:
            collect_numbers(v, out)
    return out


def derived_numbers(payload: dict[str, Any]) -> list[float]:
    """Counts the report may legitimately state.

    "共测量 22 个亚区" is a true statement about the payload even though 22
    appears nowhere inside it. Without this the guardrail rejects an accurate
    sentence, and a check that cries wolf is a check people switch off.
    """
    out: list[float] = []
    subs = payload.get("subregions") or []
    plates = payload.get("plates") or []
    out += [float(len(subs)), float(len(plates))]
    for level in ("high", "medium", "low"):
        n = ((payload.get("quality") or {}).get("subregionConfidence") or {}).get(level)
        if n is not None:
            out.append(float(n))
    for key in ("confidence",):
        for level in ("high", "medium", "low"):
            out.append(float(sum(1 for s in subs if s.get(key) == level)))
    return out


def check(report: dict[str, Any], payload: dict[str, Any], *,
          banned_terms: list[str] | None = None,
          allowed_codes: set[str] | None = None,
          required_sections: tuple[str, ...] | None = None) -> GuardrailResult:
    """Validate a model response against the numbers it was given.

    Every vocabulary this checks against is per chapter, because the cartilage
    chapter's vocabulary is wrong for every other structure: the cartilage
    chapter forbids 半月板 and knows 22 subregion codes, a meniscus chapter
    must allow the word and knows none of the codes. Each argument defaults to
    the cartilage list, which keeps the original single-report path unchanged.
    """
    violations: list[str] = []
    terms = BANNED_TERMS if banned_terms is None else list(banned_terms)
    codes = ALLOWED_SUBREGIONS if allowed_codes is None else set(allowed_codes)
    sections = REQUIRED_SECTIONS if required_sections is None else tuple(required_sections)

    missing = [s for s in sections if not str(report.get(s) or "").strip()]
    if missing:
        violations.append("缺少必需段落: " + ", ".join(missing))

    prose = "\n".join(str(report.get(s) or "") for s in sections)

    banned_hits = sorted({t for t in terms if t in prose})
    if banned_hits:
        violations.append(
            "提到了本章的数据无法支持的结构: " + ", ".join(banned_hits))

    # Subregion codes must be ones we actually measure.
    bad_codes = sorted({
        c for c in re.findall(r"\b(?:[a-z]{1,2})?[A-Z]{1,3}[A-Za-z]?\b", prose)
        if c in _CODE_LOOKALIKES and c not in codes})
    if bad_codes:
        violations.append("使用了未定义的亚区代码: " + ", ".join(bad_codes))

    refs = report.get("metricRefs") or []
    bad_refs = [r for r in refs if r not in codes]
    if bad_refs:
        violations.append("metricRefs 含未知亚区: " + ", ".join(map(str, bad_refs)))

    violations += grade_violations(prose, payload)

    known = collect_numbers(payload) + derived_numbers(payload)
    unmatched = [n for n in _numbers_in(prose) if not _matches(n, known)]
    if unmatched:
        violations.append(
            "正文出现了载荷中不存在的数字: "
            + ", ".join("%g" % n for n in unmatched[:8]))

    return GuardrailResult(
        ok=not violations, violations=violations,
        unmatched_numbers=unmatched, banned_hits=banned_hits)


def grade_violations(prose: str, payload: dict[str, Any]) -> list[str]:
    """Every grade in the prose must be one Python assigned.

    Three rules: no grades at all when the payload carries none; the set of
    grades mentioned must be a subset of the payload's; and a grade written
    next to a subregion name must be that subregion's grade. "I 级" is only
    tolerated in the fixed grading note, never as a finding.
    """
    out: list[str] = []
    # The fixed grading note names every grade by definition ("II 级为…"). A
    # report quoting it is faithful, so it is removed before scanning.
    note = str(payload.get("gradingNote") or "").strip()
    if note:
        prose = prose.replace(note, "")
    mentioned = [_ROMAN.get(m.group(1), m.group(1)) for m in _GRADE.finditer(prose)]
    if not mentioned:
        return out
    grades = payload.get("grades") or []
    if not grades:
        return ["提到了分级，但载荷中没有任何分级"]
    allowed = {str(g.get("grade")) for g in grades} | {"0"}
    for g in set(mentioned):
        if g not in allowed:
            out.append("正文出现了载荷中不存在的分级: %s 级" % g)
    by_label = {}
    for g in grades:
        for key in (g.get("labelZh"), g.get("code")):
            if key:
                by_label[str(key)] = str(g.get("grade"))
    for label, grade in by_label.items():
        for m in re.finditer(re.escape(label) + r"[^。；;\n]{0,24}?" + _GRADE.pattern, prose):
            said = _ROMAN.get(m.group(1), m.group(1))
            if said != grade:
                out.append("%s 的分级写成 %s 级，载荷为 %s 级" % (label, said, grade))
    return out


# Tokens that look like subregion codes; anything matching this shape but not
# in the allowlist is a fabricated region name.
_CODE_LOOKALIKES = {
    *ALLOWED_SUBREGIONS,
    "MFC", "LFC", "MTC", "LTC", "PC", "MFTC", "LFTC", "cMF", "cLF", "MT", "LT",
    "aMF", "eMF", "iMF", "aLF", "eLF", "iLF", "cMP", "cLP", "MPF", "LPF",
}


def _numbers_in(text: str) -> list[float]:
    """Numbers a reader would take as a measurement.

    Years, list markers and small counts are excluded: "1." opening a bullet or
    "3 mm" quoted from the fixed limitations text are not claims about this
    knee, and flagging them would train everyone to ignore the check.
    """
    out: list[float] = []
    for m in _NUMBER.finditer(text):
        raw = m.group(1) + m.group(2)
        after = text[m.end():m.end() + 2]
        before = text[max(0, m.start() - 1):m.start()]
        if "." not in raw and abs(float(raw)) <= 10 and not after.startswith("%"):
            continue                       # ordinals, counts, "3 mm" slice talk
        if after.startswith("年") or before == "#":
            continue
        out.append(float(raw))
    return out


def _matches(value: float, known: list[float]) -> bool:
    for k in known:
        if abs(value - k) <= MM_TOLERANCE:
            return True
        if abs(value - k) <= PCT_TOLERANCE and abs(k) > 5:
            return True                    # percentages, rounded in prose
    return False
