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


def check(report: dict[str, Any], payload: dict[str, Any]) -> GuardrailResult:
    """Validate a model response against the numbers it was given."""
    violations: list[str] = []

    missing = [s for s in REQUIRED_SECTIONS if not str(report.get(s) or "").strip()]
    if missing:
        violations.append("缺少必需段落: " + ", ".join(missing))

    prose = "\n".join(str(report.get(s) or "") for s in REQUIRED_SECTIONS)

    banned_hits = sorted({t for t in BANNED_TERMS if t in prose})
    if banned_hits:
        violations.append(
            "提到了标签 1-8 无法支持的结构: " + ", ".join(banned_hits))

    # Subregion codes must be ones we actually measure.
    bad_codes = sorted({
        c for c in re.findall(r"\b(?:[a-z]{1,2})?[A-Z]{1,3}[A-Za-z]?\b", prose)
        if c in _CODE_LOOKALIKES and c not in ALLOWED_SUBREGIONS})
    if bad_codes:
        violations.append("使用了未定义的亚区代码: " + ", ".join(bad_codes))

    refs = report.get("metricRefs") or []
    bad_refs = [r for r in refs if r not in ALLOWED_SUBREGIONS]
    if bad_refs:
        violations.append("metricRefs 含未知亚区: " + ", ".join(map(str, bad_refs)))

    known = collect_numbers(payload) + derived_numbers(payload)
    unmatched = [n for n in _numbers_in(prose) if not _matches(n, known)]
    if unmatched:
        violations.append(
            "正文出现了载荷中不存在的数字: "
            + ", ".join("%g" % n for n in unmatched[:8]))

    return GuardrailResult(
        ok=not violations, violations=violations,
        unmatched_numbers=unmatched, banned_hits=banned_hits)


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
