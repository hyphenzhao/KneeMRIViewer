"""The report's fixed skeleton, read from YAML.

A report is a list of chapters in a fixed order. Each chapter declares where
its content comes from; the code refuses to invent content for a chapter whose
source is ``pending``. Doctors edit the YAML to change structure or wording -
never the code.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SOURCES = ("computed", "model", "radiologist", "manual", "pending", "composed")
# What a grade may be, per scale. A chapter names one with `grade_scale`; the
# doctor's edit dropdown and the override validator both read it, so a new
# structure cannot end up with its grades silently validated against
# Outerbridge's vocabulary.
GRADE_SCALES: dict[str, tuple[str, ...]] = {
    "outerbridge_mri_thickness": ("0", "II", "III", "IV", "未评估"),
    "stoller": ("0", "I", "II", "III", "未评估"),
    "presence": ("有", "无", "未评估"),
}
DEFAULT_GRADE_SCALE = "outerbridge_mri_thickness"

SECTIONS = ("findings", "impression", "advice", "appendix")


@dataclass
class ChapterSpec:
    id: str
    order: int
    section: str
    title_zh: str
    source: str
    ai_enabled: bool = False
    ai_prompt_file: str | None = None
    model_key: str | None = None
    data_keys: list[str] = field(default_factory=list)
    template_zh: str | None = None
    placeholder_zh: str | None = None
    prefill_regex: str | None = None
    banned_terms: list[str] = field(default_factory=list)
    allowed_terms: list[str] = field(default_factory=list)
    caveats_zh: list[str] = field(default_factory=list)
    subitems_zh: list[str] = field(default_factory=list)
    severity_order: list[str] = field(default_factory=list)
    fixed_items_zh: list[str] = field(default_factory=list)
    grade_scale: str | None = None
    # Which label set a `source: model` chapter is fed by. The model writes a
    # labelmap under that key (see scan/predictions.py); this is the join.
    model_label_set: str | None = None

    @property
    def grade_values(self) -> tuple[str, ...]:
        """The grades a doctor may choose for this chapter.

        Outerbridge unless the chapter says otherwise, so every existing
        chapter keeps the vocabulary it already had.
        """
        return GRADE_SCALES[self.grade_scale or DEFAULT_GRADE_SCALE]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "order": self.order, "section": self.section,
            "titleZh": self.title_zh, "source": self.source,
            "aiEnabled": self.ai_enabled, "modelKey": self.model_key,
            "modelLabelSet": self.model_label_set, "gradeValues": list(self.grade_values),
            "placeholderZh": self.placeholder_zh, "subitemsZh": self.subitems_zh,
            "caveatsZh": self.caveats_zh, "gradeScale": self.grade_scale,
        }


@dataclass
class ReportTemplate:
    key: str
    version: int
    title_zh: str
    tail_sentence_zh: str
    disclaimer_zh: str
    global_banned_terms: list[str]
    radiologist_prefill: dict[str, Any]
    chapters: list[ChapterSpec]
    path: Path
    hash: str

    def chapter(self, cid: str) -> ChapterSpec:
        for c in self.chapters:
            if c.id == cid:
                return c
        raise KeyError(cid)

    def banned_terms_for(self, cid: str) -> list[str]:
        return list(self.global_banned_terms) + list(self.chapter(cid).banned_terms)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key, "version": self.version, "titleZh": self.title_zh,
            "tailSentenceZh": self.tail_sentence_zh, "disclaimerZh": self.disclaimer_zh,
            "hash": self.hash,
            "radiologistPrefill": self.radiologist_prefill,
            "chapters": [c.to_json() for c in self.chapters],
        }


def template_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def load_template(cfg: Any, key: str = "knee_zh_v1") -> ReportTemplate:
    """Read and validate a template. Raises ValueError on a malformed file -
    a broken template must fail loudly, not degrade into an empty report."""
    import yaml

    if not key.replace("_", "").replace("-", "").isalnum():
        raise ValueError("bad template key %r" % key)
    path = Path(cfg.reports_dir) / (key + ".yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    chapters: list[ChapterSpec] = []
    seen: set[str] = set()
    for c in raw.get("chapters") or []:
        spec = ChapterSpec(
            id=str(c["id"]), order=int(c["order"]), section=str(c["section"]),
            title_zh=str(c["title_zh"]), source=str(c["source"]),
            ai_enabled=bool(c.get("ai_enabled", False)),
            ai_prompt_file=c.get("ai_prompt_file"), model_key=c.get("model_key"),
            model_label_set=c.get("model_label_set"),
            data_keys=list(c.get("data_keys") or []),
            template_zh=c.get("template_zh"), placeholder_zh=c.get("placeholder_zh"),
            prefill_regex=c.get("prefill_regex"),
            banned_terms=list(c.get("banned_terms") or []),
            allowed_terms=list(c.get("allowed_terms") or []),
            caveats_zh=list(c.get("caveats_zh") or []),
            subitems_zh=list(c.get("subitems_zh") or []),
            severity_order=list(c.get("severity_order") or []),
            fixed_items_zh=list(c.get("fixed_items_zh") or []),
            grade_scale=c.get("grade_scale"),
        )
        if spec.id in seen:
            raise ValueError("duplicate chapter id %r" % spec.id)
        if spec.source not in SOURCES:
            raise ValueError("chapter %s: unknown source %r" % (spec.id, spec.source))
        if spec.section not in SECTIONS:
            raise ValueError("chapter %s: unknown section %r" % (spec.id, spec.section))
        if spec.ai_enabled and not spec.ai_prompt_file:
            raise ValueError("chapter %s: ai_enabled needs ai_prompt_file" % spec.id)
        if spec.grade_scale and spec.grade_scale not in GRADE_SCALES:
            raise ValueError("chapter %r: unknown grade_scale %r (known: %s)"
                             % (spec.id, spec.grade_scale, ", ".join(sorted(GRADE_SCALES))))
        if spec.source == "model" and not spec.model_label_set:
            raise ValueError("chapter %r: source 'model' needs model_label_set" % spec.id)
        if spec.source == "pending" and not spec.placeholder_zh:
            raise ValueError("chapter %s: pending chapters need placeholder_zh" % spec.id)
        seen.add(spec.id)
        chapters.append(spec)
    chapters.sort(key=lambda c: c.order)
    if not chapters:
        raise ValueError("template %s has no chapters" % key)

    return ReportTemplate(
        key=str(raw.get("key") or key), version=int(raw.get("version") or 1),
        title_zh=str(raw.get("title_zh") or ""),
        tail_sentence_zh=str(raw.get("tail_sentence_zh") or ""),
        disclaimer_zh=str(raw.get("disclaimer_zh") or ""),
        global_banned_terms=list(raw.get("global_banned_terms") or []),
        radiologist_prefill=dict(raw.get("radiologist_prefill") or {}),
        chapters=chapters, path=path, hash=template_hash(path),
    )
