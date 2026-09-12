"""What leaves this machine - built by whitelist, then asserted.

Two independent mechanisms, deliberately:

1. ``build_payload`` constructs a **new** dict from named fields. It never
   iterates a database row, so a column added later cannot silently start
   being transmitted.
2. ``assert_deidentified`` re-reads the finished payload and raises on anything
   that looks like an identifier. It is the backstop for a mistake in (1), and
   it runs **before a socket is opened**, not after.

The second exists because the first is only as good as the person editing it.
"""
from __future__ import annotations

import re
from typing import Any

# Things that must never appear in a payload, checked against every string.
_FORBIDDEN = [
    # DICOM UIDs: dotted digit runs, 4+ components.
    (re.compile(r"\b\d+(?:\.\d+){3,}\b"), "DICOM UID"),
    (re.compile(r"\b(19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b"), "date"),
    (re.compile(r"\b(19|20)\d{6}\b"), "compact date"),
    (re.compile(r"\b\d{2}:\d{2}(:\d{2})?\b"), "time"),
    (re.compile(r"(?:^|[\s\"'])(?:/|[A-Za-z]:\\\\)[\w./\\\\ -]{4,}"), "file path"),
    # Chinese ID card and long medical record numbers.
    (re.compile(r"\b\d{15}(\d{2}[\dxX])?\b"), "ID-like number"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "phone number"),
]

# Keys that must never appear anywhere in the payload, at any depth.
_FORBIDDEN_KEYS = {
    "name", "patient_name", "patientName", "external_id", "externalId",
    "id", "patient_id", "patientId", "series_id", "seriesId", "study_id",
    "studyId", "segmentation_id", "segmentationId", "seg_key", "segKey",
    "accession", "accession_number", "path", "path_rel", "pathRel",
    "source_path_rel", "dataset", "dataset_key", "datasetKey", "root_path",
    "report", "report_text", "findings_text", "birth_date", "birthDate",
    "study_date", "studyDate", "institution", "volume_key", "volumeKey",
}

# Subregion codes the model is allowed to mention.
ALLOWED_SUBREGIONS = {
    "MFT", "ecMF", "ccMF", "icMF", "pMF", "LFT", "ecLF", "ccLF", "icLF", "pLF",
    "cMT", "aMT", "eMT", "pMT", "iMT", "cLT", "aLT", "eLT", "pLT", "iLT",
    "MP", "LP",
}


class DeidentificationError(Exception):
    """Raised before any network call when a payload looks identifying."""


def age_band(age: Any) -> str | None:
    """A decade, never an exact age. 67 -> '60-69'.

    Accepts whatever the database holds: DICOM ages arrive as strings like
    "067Y" or "45", and an unparseable one yields no band rather than an error.
    """
    if age is None:
        return None
    if not isinstance(age, (int, float)):
        digits = re.match(r"\s*(\d{1,3})", str(age))
        if not digits:
            return None
        age = int(digits.group(1))
    if age < 0 or age > 120:
        return None
    lo = (int(age) // 10) * 10
    return "%d-%d" % (lo, lo + 9)


def build_payload(morph: dict[str, Any], refs: dict[str, Any],
                  *, sex: str | None, age: Any, laterality: str | None,
                  patient_context: list[str]) -> dict[str, Any]:
    """Assemble the de-identified prompt input from named fields only.

    Every number the model is allowed to state appears here. Nothing is
    computed on the model's side, so the narrative and the dashboard cannot
    disagree.
    """
    metrics = morph.get("metrics") or {}
    qc = morph.get("qc") or {}
    ref_sub = refs.get("subregions", {})
    sex_key = "male" if (sex or "").upper().startswith("M") else (
        "female" if (sex or "").upper().startswith("F") else None)

    subregions = []
    for plate in metrics.get("plates", []):
        for code, s in (plate.get("subregions") or {}).items():
            if code not in ALLOWED_SUBREGIONS:
                continue
            mean = (s.get("thicknessMm") or {}).get("mean")
            if mean is None:
                continue
            band = ref_sub.get(code, {}).get(sex_key) if sex_key else None
            entry: dict[str, Any] = {
                "code": code,
                "labelZh": s.get("labelZh"),
                "plateZh": plate.get("name"),
                "meanThicknessMm": mean,
                "p10Mm": (s.get("thicknessMm") or {}).get("p10"),
                "p90Mm": (s.get("thicknessMm") or {}).get("p90"),
                "sliceSupport": s.get("sliceSupport"),
                "reliableAreaPct": round(100 * (s.get("reliableAreaFraction") or 0), 0),
                "confidence": s.get("confidence"),
            }
            if band:
                entry["referenceMeanMm"], entry["referenceSdMm"] = band[0], band[1]
                entry["referenceApplicability"] = refs.get(
                    "reference_source", {}).get("applicability", "indirect")
            subregions.append(entry)

    plates = [{
        "plateZh": p.get("name"),
        "meanThicknessMm": (p.get("thickness") or {}).get("mean"),
        "volumeCm3": round((p.get("volume_mm3") or 0) / 1000.0, 2),
        "reliableAreaPct": round(100 * (p.get("qc") or {}).get("reliableAreaFraction", 0), 0),
    } for p in metrics.get("plates", [])]

    comp = metrics.get("compartments") or {}
    compartments = {
        side: {
            "femoralThicknessMm": (comp.get(side) or {}).get("femoralThicknessMm"),
            "tibialThicknessMm": (comp.get(side) or {}).get("tibialThicknessMm"),
            "sumThicknessMm": (comp.get(side) or {}).get("sumThicknessMm"),
            "volumeCm3": round(((comp.get(side) or {}).get("volumeMm3") or 0) / 1000.0, 2),
        } for side in ("medial", "lateral") if comp.get(side)
    }
    if comp.get("asymmetryPct") is not None:
        compartments["asymmetryPct"] = comp["asymmetryPct"]
    if comp.get("medialLateralRatio") is not None:
        compartments["medialLateralRatio"] = comp["medialLateralRatio"]

    context: dict[str, Any] = {}
    if "ageBand" in patient_context:
        context["ageBand"] = age_band(age)
    if "sex" in patient_context:
        context["sex"] = {"male": "男", "female": "女"}.get(sex_key or "", None)
    if "laterality" in patient_context:
        context["laterality"] = {"L": "左膝", "R": "右膝"}.get(laterality or "", None)

    payload = {
        "patientContext": {k: v for k, v in context.items() if v is not None},
        "acquisition": {
            "planeZh": "矢状位",
            "dimensionality": "2D",
            "sliceThicknessMm": 3.0,
            "inPlaneMm": 0.2917,
        },
        "compartments": compartments,
        "plates": plates,
        "subregions": subregions,
        "quality": {
            "reliableAreaPct": round(100 * (qc.get("reliableAreaFraction") or 0), 0),
            "effectiveResolutionMm": qc.get("effectiveResolutionMm"),
            "escapedPct": round(100 * (qc.get("escapedFraction") or 0), 1),
            "subregionConfidence": qc.get("subregionConfidence") or {},
        },
        # The thresholds a report may legitimately cite ("有效分辨率优于 0.6 mm").
        "thresholds": refs.get("thresholds") or {},
        "limitations": {
            "zh": (refs.get("measurement_caveat") or {}).get("zh"),
            "implicationsZh": (refs.get("measurement_caveat") or {}).get("implications_zh") or [],
        },
        "scopeNote": (
            "本次分析仅基于股骨、胫骨、髌骨及其软骨的分割标签，"
            "不包含半月板、韧带、骨髓、滑液等结构，不得对这些结构作任何推断。"),
    }
    assert_deidentified(payload)
    return payload


def assert_deidentified(payload: Any, _path: str = "payload") -> None:
    """Raise if anything identifying survived into the payload.

    Runs before the socket is opened. Deliberately paranoid: a false positive
    costs a developer five minutes, a false negative sends patient data to a
    third party.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _FORBIDDEN_KEYS:
                raise DeidentificationError(
                    "forbidden key %r at %s" % (key, _path))
            assert_deidentified(value, "%s.%s" % (_path, key))
    elif isinstance(payload, (list, tuple)):
        for i, value in enumerate(payload):
            assert_deidentified(value, "%s[%d]" % (_path, i))
    elif isinstance(payload, str):
        for pattern, what in _FORBIDDEN:
            m = pattern.search(payload)
            if m:
                raise DeidentificationError(
                    "%s-like text at %s: %r" % (what, _path, m.group(0)[:40]))
