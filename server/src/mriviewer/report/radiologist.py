"""Split the hospital's free-text report into the template's chapters.

The sample reports are boilerplate sentences separated by "。", so a sentence
split plus a per-chapter regex assigns almost everything. Whatever no chapter
claims goes to ``other`` rather than being dropped - a sentence nobody
expected is exactly the one a doctor wants to see.
"""
from __future__ import annotations

import re
from typing import Any

from .template import ReportTemplate

_SPLIT = re.compile(r"[。；;]\s*")


def split_sentences(text: str | None) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in _SPLIT.split(text)]
    return [p + "。" for p in parts if p]


def is_copy_paste(findings: str | None, impression: str | None) -> bool:
    """The findings cell is just the impression again (88 of 157 sample rows)."""
    if not findings or not impression:
        return False
    return findings.strip() == impression.strip()


def classify(sentences: list[str], template: ReportTemplate) -> dict[str, list[str]]:
    """Assign each sentence to the first chapter whose regex matches."""
    rules = [(c.id, re.compile(c.prefill_regex)) for c in template.chapters
             if c.prefill_regex]
    out: dict[str, list[str]] = {}
    for s in sentences:
        target = "other"
        for cid, rx in rules:
            if rx.search(s):
                target = cid
                break
        out.setdefault(target, []).append(s)
    return out


def laterality_of(text: str | None) -> str | None:
    """'左'/'右' from the first characters of a report, or None."""
    if not text:
        return None
    head = text.strip()[:8]
    if "左" in head:
        return "L"
    if "右" in head:
        return "R"
    return None


def prefill(report_row: Any, template: ReportTemplate) -> dict[str, Any]:
    """Everything the document needs from one `report` table row.

    Returns ``{"byChapter": {id: [sentences]}, "impressionSentences": [...],
    "copyPaste": bool, "laterality": "L"|"R"|None, "labelZh", "noteZh"}``.
    """
    findings = report_row["findings"] if report_row else None
    impression = report_row["diagnosis"] if report_row else None
    cp = is_copy_paste(findings, impression)
    by_chapter = {} if cp else classify(split_sentences(findings), template)
    # The fixed tail ("请结合临床其他检查，随访") is usually glued onto the last
    # impression sentence with a comma, so it is cut out of each sentence
    # rather than matched as a sentence of its own.
    tail = template.tail_sentence_zh.strip("。")
    impression_sentences = []
    for s in split_sentences(impression):
        if tail:
            s = s.replace("，" + tail, "").replace(tail, "")
        s = s.strip("，,。 ")
        if s:
            impression_sentences.append(s + "。")
    pf = template.radiologist_prefill
    return {
        "byChapter": by_chapter,
        "impressionSentences": impression_sentences,
        "copyPaste": cp,
        "laterality": laterality_of(findings) or laterality_of(impression),
        "labelZh": pf.get("label_zh", "放射科报告"),
        "noteZh": pf.get("note_zh", "来自放射科原始报告，非本平台计算结果"),
    }
