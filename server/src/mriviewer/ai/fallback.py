"""The report that needs no model at all.

Written first and used whenever the LLM is off, unreachable, slow, or rejected
by the guardrail. An air-gapped install runs on this path permanently and still
produces the same four sections from the same numbers.

This is not a degraded mode to apologise for - it is the floor under the
feature. The LLM only ever improves the prose.
"""
from __future__ import annotations

from typing import Any

# A subregion this far below the reference mean, in SDs, is called out by name.
Z_NOTABLE = -1.5
# Compartment asymmetry beyond this is worth a sentence.
ASYMMETRY_PCT = 10.0


def render_template_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Deterministically compose the four sections from the payload."""
    ctx = payload.get("patientContext") or {}
    comp = payload.get("compartments") or {}
    quality = payload.get("quality") or {}
    subs = payload.get("subregions") or []

    who = "、".join(
        v for v in (ctx.get("ageBand") and ctx["ageBand"] + " 岁",
                    ctx.get("sex"), ctx.get("laterality")) if v)

    findings = [
        "%s膝关节软骨分割共测量 %d 个亚区。" % (who + "，" if who else "", len(subs)),
        "采集为矢状位 2D 序列、层厚 3.0 mm、面内分辨率 0.29 mm；"
        "骨-软骨界面中有效分辨率优于 0.6 mm 的面积占 %.0f%%。"
        % (quality.get("reliableAreaPct") or 0),
    ]

    low = [s for s in subs if s.get("confidence") == "low"]
    if low:
        findings.append(
            "其中 %d 个亚区因跨越原始层数过少或可靠面积不足，测量可信度偏低："
            "%s。" % (len(low), "、".join(s["labelZh"] for s in low[:6])))

    quant: list[str] = []
    for side, name in (("medial", "内侧"), ("lateral", "外侧")):
        c = comp.get(side)
        if not c:
            continue
        parts = []
        if c.get("femoralThicknessMm") is not None:
            parts.append("股骨软骨 %.2f mm" % c["femoralThicknessMm"])
        if c.get("tibialThicknessMm") is not None:
            parts.append("胫骨软骨 %.2f mm" % c["tibialThicknessMm"])
        if c.get("sumThicknessMm") is not None:
            parts.append("股胫合计 %.2f mm" % c["sumThicknessMm"])
        if c.get("volumeCm3") is not None:
            parts.append("软骨体积 %.2f cm³" % c["volumeCm3"])
        if parts:
            quant.append("%s间室：%s。" % (name, "，".join(parts)))

    asym = comp.get("asymmetryPct")
    if asym is not None:
        quant.append("内外侧不对称度 %+.1f%%。" % asym)

    graded = [g for g in (payload.get("grades") or [])
              if str(g.get("grade")) in ("II", "III", "IV")]
    if graded:
        quant.append(
            "厚度推导的 Outerbridge 样分级（II 级及以上）：%s。"
            % "、".join("%s %s 级" % (g.get("labelZh") or g.get("code"), g["grade"])
                        for g in graded[:6]))
    note = payload.get("gradingNote")
    if note and payload.get("grades"):
        quant.append(str(note).strip())

    thin = _notable_thin(subs)
    if thin:
        quant.append(
            "相对参考值偏薄的亚区：%s。"
            % "、".join("%s %.2f mm（参考 %.2f ± %.2f mm）"
                        % (s["labelZh"], s["meanThicknessMm"],
                           s["referenceMeanMm"], s["referenceSdMm"])
                        for s in thin[:4]))

    impression: list[str] = []
    if asym is not None and abs(asym) > ASYMMETRY_PCT:
        impression.append(
            "%s间室软骨相对偏薄（不对称度 %+.1f%%），提示该间室负荷或退变程度较对侧明显。"
            % ("内侧" if asym < 0 else "外侧", asym))
    elif asym is not None:
        impression.append("内外侧软骨厚度大致对称（不对称度 %+.1f%%）。" % asym)
    if thin:
        impression.append(
            "共 %d 个亚区平均厚度低于同性别参考均值 1.5 个标准差以上。" % len(thin))
    else:
        impression.append("各亚区平均厚度未见明显低于参考范围。")
    # Quote the limitations text rather than restating it. The wording and the
    # percentage live in the reference YAML that doctors edit; duplicating them
    # here would let the two drift, and the guardrail would rightly flag a
    # number that appears in no payload.
    caveat = (payload.get("limitations") or {}).get("zh")
    if caveat:
        impression.append(caveat)

    advice = [
        "本结果为算法自动测量，须由医师结合原始图像复核后方可用于临床判断。",
        "如需可靠的软骨厚度绝对值，建议加扫各向同性 3D 序列（如 DESS/CUBE/VISTA）。",
        "同一患者随访时请使用相同扫描协议，纵向变化的可靠性显著高于单次绝对值。",
    ]
    if low:
        advice.append("可信度偏低的亚区建议以原始图像目视核对为准。")

    return {
        "findings": "\n".join(findings),
        "quant": "\n".join(quant),
        "impression": "\n".join(impression),
        "advice": "\n".join(advice),
        "metricRefs": [s["code"] for s in thin][:8],
    }


def _notable_thin(subs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Subregions well below the reference mean, worst first."""
    out = []
    for s in subs:
        mean, ref, sd = (s.get("meanThicknessMm"), s.get("referenceMeanMm"),
                         s.get("referenceSdMm"))
        if mean is None or not ref or not sd:
            continue
        if (mean - ref) / sd <= Z_NOTABLE:
            out.append({**s, "_z": (mean - ref) / sd})
    return sorted(out, key=lambda s: s["_z"])
