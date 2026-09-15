"""One resolver per chapter source. Adding a model to the report is one entry.

Every resolver returns a chapter dict of the same shape:

    id, order, section, titleZh, source, status, prose, proseOrigin,
    facts[], grades[], items[], caveatsZh[], placeholderZh, radiologist, sourceRef

``status`` is ``ok`` | ``pending`` | ``failed``. A failed computed chapter is
reported as failed - never quietly rendered as if nothing were wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .template import ChapterSpec, ReportTemplate


@dataclass
class GenContext:
    """Everything a resolver may look at. Deliberately no database handle."""
    template: ReportTemplate
    morph: dict[str, Any]                 # segmentation_morphometry row as JSON
    ai_report: dict[str, Any] | None      # cartilage ai_report row as JSON
    payload: dict[str, Any] | None        # de-identified payload the AI saw
    radiologist: dict[str, Any]           # radiologist.prefill() output
    laterality: str | None
    sex_zh: str | None
    age_band: str | None
    errors: dict[str, str] = field(default_factory=dict)
    # chapter id -> what a model left behind for this series, if anything.
    # See scan/predictions.py and document.py::_structures.
    structures: dict[str, Any] = field(default_factory=dict)
    # Chapters already resolved this run, by id. Chapters are resolved in
    # template order, so `composed` ones (impression, advice, order 100+) can
    # collect from the findings chapters above them.
    resolved: dict[str, Any] = field(default_factory=dict)


def _base(spec: ChapterSpec, status: str = "ok") -> dict[str, Any]:
    return {
        "id": spec.id, "order": spec.order, "section": spec.section,
        "titleZh": spec.title_zh, "source": spec.source, "status": status,
        "prose": None, "proseOrigin": "none",
        "facts": [], "grades": [], "items": [],
        "caveatsZh": list(spec.caveats_zh), "placeholderZh": None,
        "radiologist": None, "sourceRef": None,
        "subitemsZh": list(spec.subitems_zh),
    }


def _fact(key: str, label: str, value: Any, unit: str = "", fmt: str = ".2f",
          provenance: str = "morph") -> dict[str, Any]:
    return {"key": key, "labelZh": label, "value": value, "unit": unit,
            "fmt": fmt, "provenance": provenance}


def _radiologist_block(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any] | None:
    sentences = (ctx.radiologist.get("byChapter") or {}).get(spec.id) or []
    if not sentences:
        return None
    return {"labelZh": ctx.radiologist["labelZh"], "sentences": sentences,
            "note": ctx.radiologist["noteZh"]}


# --------------------------------------------------------------- resolvers

def computed_overview(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    ch = _base(spec)
    p = ctx.payload or {}
    acq = p.get("acquisition") or {}
    q = p.get("quality") or {}
    values = {
        "laterality": {"L": "左膝", "R": "右膝"}.get(ctx.laterality or "", "膝关节"),
        "sliceThicknessMm": acq.get("sliceThicknessMm", "?"),
        "inPlaneMm": acq.get("inPlaneMm", "?"),
        "reliableAreaPct": ("%.0f" % q["reliableAreaPct"]) if q.get("reliableAreaPct") is not None else "?",
        "effectiveResolutionMm": ("%.2f" % q["effectiveResolutionMm"]) if q.get("effectiveResolutionMm") is not None else "?",
    }
    ch["prose"] = (spec.template_zh or "").format(**values).replace("\n", "")
    ch["proseOrigin"] = "template"
    ch["facts"] = [
        _fact("quality.reliableAreaPct", "可靠区面积占比", q.get("reliableAreaPct"), "%", ".0f"),
        _fact("quality.effectiveResolutionMm", "平均有效分辨率", q.get("effectiveResolutionMm"), "mm"),
        _fact("acquisition.sliceThicknessMm", "层厚", acq.get("sliceThicknessMm"), "mm", ".1f"),
    ]
    return ch


def computed_cartilage(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    ch = _base(spec)
    rep = ctx.ai_report
    p = ctx.payload or {}
    if rep is None:
        ch["status"] = "failed"
        ch["prose"] = None
        ch["caveatsZh"].insert(0, ctx.errors.get(spec.id, "软骨章生成失败"))
        return ch

    ch["prose"] = "\n".join(t for t in (rep.get("findings"), rep.get("quant")) if t)
    ch["proseOrigin"] = "llm" if rep.get("status") == "ok" else "template"
    ch["sourceRef"] = {"kind": "ai_report", "id": rep.get("id"), "status": rep.get("status")}

    comp = p.get("compartments") or {}
    facts: list[dict[str, Any]] = []
    for side, zh in (("medial", "内侧"), ("lateral", "外侧")):
        c = comp.get(side) or {}
        facts += [
            _fact("compartments.%s.femoralThicknessMm" % side, zh + "股骨软骨平均厚度", c.get("femoralThicknessMm"), "mm"),
            _fact("compartments.%s.tibialThicknessMm" % side, zh + "胫骨软骨平均厚度", c.get("tibialThicknessMm"), "mm"),
            _fact("compartments.%s.sumThicknessMm" % side, zh + "股胫合计", c.get("sumThicknessMm"), "mm"),
            _fact("compartments.%s.volumeCm3" % side, zh + "软骨体积", c.get("volumeCm3"), "cm³"),
        ]
    if comp.get("asymmetryPct") is not None:
        facts.append(_fact("compartments.asymmetryPct", "内外侧不对称度", comp["asymmetryPct"], "%", "+.1f"))
    for s in p.get("subregions") or []:
        facts.append(_fact("sub.%s.meanThicknessMm" % s["code"], s.get("labelZh") or s["code"],
                           s.get("meanThicknessMm"), "mm"))
    ch["facts"] = [f for f in facts if f["value"] is not None]

    ch["grades"] = [
        {"key": g["code"], "labelZh": g.get("labelZh"), "grade": g.get("grade"),
         "confidence": g.get("confidence"), "lesionAreaMm2": g.get("lesionAreaMm2"),
         "deficitPct": g.get("deficitPct"), "baselineMm": g.get("baselineMm"),
         "provenance": "morph"}
        for g in (p.get("grades") or [])
    ]
    ch["radiologist"] = _radiologist_block(spec, ctx)
    return ch


def model_structures(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    """A chapter fed by a segmentation model's output.

    The platform does not run the model. It reads what the model left for this
    series (``scan/predictions.py``), and says only what a labelmap can
    support: which structures were segmented, how large each is, how many
    native slices it spans. No tear, no grade, no signal - those need either a
    different model or a rule we have validated, and until then this chapter
    must not imply them.

    No output for this chapter's label set means the chapter stays **pending**.
    An empty section would read as "normal", which is the one thing it must
    never read as.
    """
    found = (ctx.structures or {}).get(spec.id)
    if not found or not found.get("labels"):
        return pending(spec, ctx)

    ch = _base(spec)
    labels = found["labels"]
    for lab in labels:
        code = lab.get("nameEn") or str(lab["value"])
        name = lab.get("nameZh") or code
        ch["facts"].append(_fact("model.%s.volumeCm3" % code, name + "体积",
                                 lab.get("volumeCm3"), "cm³", ".2f", "model"))
        ch["facts"].append(_fact("model.%s.slices" % code, name + "跨越层数",
                                 lab.get("nSlices"), "层", ".0f", "model"))

    names = "、".join(lab.get("nameZh") or str(lab["value"]) for lab in labels)
    sizes = "；".join(
        "%s %.2f cm³、跨 %d 个原始层" % (lab.get("nameZh") or lab["value"],
                                        lab.get("volumeCm3") or 0.0, lab.get("nSlices") or 0)
        for lab in labels)
    tpl = spec.template_zh or "本次由模型分割出{names}。{sizes}。本章为分割所得的形态学测量，未评估信号、撕裂或退变。"
    ch["prose"] = tpl.format(names=names, sizes=sizes,
                             model=found.get("model") or "", version=found.get("version") or "")
    ch["proseOrigin"] = "template"
    ch["caveatsZh"] = list(ch["caveatsZh"]) + [
        "本章结构由模型「%s %s」自动分割，未在本院本序列上验证，须由医师对照原始图像复核。"
        % (found.get("model") or "?", found.get("version") or "?")]
    ch["sourceRef"] = {"kind": "segmentation", "id": found.get("segmentationId"),
                       "status": found.get("state")}
    ch["radiologist"] = _radiologist_block(spec, ctx)
    return ch


def pending(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    """No data source yet. Placeholder only - never a sentence about the anatomy."""
    ch = _base(spec, status="pending")
    ch["placeholderZh"] = spec.placeholder_zh
    ch["radiologist"] = _radiologist_block(spec, ctx)
    return ch


def _severity_of(text: str) -> int:
    # A torn structure outranks a thin one. Without this line a meniscal tear
    # scored 1 and sorted below "软骨变薄" in the impression.
    if any(w in text for w in ("撕裂", "断裂", "骨折")):
        return 5
    if any(w in text for w in ("IV 级", "IV级", "裸露")):
        return 5
    if any(w in text for w in ("III 级", "III级")):
        return 4
    if any(w in text for w in ("II 级", "II级")):
        return 3
    if any(w in text for w in ("不对称", "偏薄", "变薄", "偏低")):
        return 2
    if any(w in text for w in ("低估", "局限", "参考", "复核")):
        return 0
    return 1


def composed_impression(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    ch = _base(spec)
    items: list[dict[str, Any]] = []
    rep = ctx.ai_report or {}
    for i, line in enumerate(t.strip() for t in (rep.get("impression") or "").split("\n")):
        if line:
            items.append({"id": "imp:cartilage:%d" % i, "text": line,
                          "severity": _severity_of(line), "origin": "computed"})
    for i, s in enumerate(ctx.radiologist.get("impressionSentences") or []):
        items.append({"id": "imp:radiologist:%d" % i, "text": s, "severity": 1,
                      "origin": "radiologist"})
    items.sort(key=lambda it: -it["severity"])
    if ctx.template.tail_sentence_zh:
        items.append({"id": "imp:tail", "text": ctx.template.tail_sentence_zh,
                      "severity": -1, "origin": "template"})
    ch["items"] = items
    return ch


def composed_advice(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    ch = _base(spec)
    items: list[dict[str, Any]] = []
    rep = ctx.ai_report or {}
    seen: set[str] = set()
    for i, line in enumerate(t.strip() for t in (rep.get("advice") or "").split("\n")):
        if line and line not in seen:
            seen.add(line)
            items.append({"id": "adv:cartilage:%d" % i, "text": line, "severity": 1,
                          "origin": "computed"})
    for i, line in enumerate(spec.fixed_items_zh):
        if line not in seen:
            seen.add(line)
            items.append({"id": "adv:fixed:%d" % i, "text": line, "severity": 0,
                          "origin": "template"})
    ch["items"] = items
    return ch


def computed_appendix(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    ch = _base(spec)
    p = ctx.payload or {}
    ch["facts"] = [
        _fact("sub.%s.meanThicknessMm" % s["code"], s.get("labelZh") or s["code"],
              s.get("meanThicknessMm"), "mm")
        for s in (p.get("subregions") or []) if s.get("meanThicknessMm") is not None
    ]
    ch["grades"] = [
        {"key": g["code"], "labelZh": g.get("labelZh"), "grade": g.get("grade"),
         "confidence": g.get("confidence"), "provenance": "morph"}
        for g in (p.get("grades") or [])
    ]
    ch["appendix"] = {
        "subregions": p.get("subregions") or [],
        "plates": p.get("plates") or [],
        "coverage": p.get("coverage") or {},
        "quality": p.get("quality") or {},
    }
    return ch


SOURCES: dict[str, Callable[[ChapterSpec, GenContext], dict[str, Any]]] = {
    "computed:overview": computed_overview,
    "computed:cartilage": computed_cartilage,
    "computed:quant_appendix": computed_appendix,
    "composed:impression": composed_impression,
    "composed:advice": composed_advice,
    "model": model_structures,
    "pending": pending,
}


def resolve(spec: ChapterSpec, ctx: GenContext) -> dict[str, Any]:
    fn = SOURCES.get("%s:%s" % (spec.source, spec.id)) or SOURCES.get(spec.source)
    if fn is None:
        # A declared source nobody implemented is a template bug, and the
        # honest rendering of that is "pending", not silence.
        ch = _base(spec, status="pending")
        ch["placeholderZh"] = spec.placeholder_zh or "本次未评估（尚未接入相应模型）"
        ch["radiologist"] = _radiologist_block(spec, ctx)
        return ch
    return fn(spec, ctx)
