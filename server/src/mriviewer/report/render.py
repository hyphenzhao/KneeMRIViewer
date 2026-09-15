"""Apply doctor overrides and produce what is shown and printed.

The web page and the PDF both render this output, so they cannot disagree.
Every override is visible: the effective value sits next to the value the
system generated, with who changed it and when. An override made before the
report was last regenerated is additionally flagged for re-review.
"""
from __future__ import annotations

import copy
from typing import Any

STALE_NOTE_ZH = "修改于上次生成之前，请复核"


def _mark(ov: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ov["id"], "original": ov.get("original"), "editor": ov.get("editor"),
        "editedAt": ov.get("editedAt"), "reason": ov.get("reason"),
        "stale": bool(ov.get("stale")),
        "noteZh": STALE_NOTE_ZH if ov.get("stale") else None,
    }


def render(document: dict[str, Any]) -> dict[str, Any]:
    """``document`` is the stored row as JSON (chapters + overrides)."""
    chapters = copy.deepcopy(document.get("chapters") or [])
    active = [o for o in (document.get("overrides") or []) if not o.get("revokedAt")]
    by_target: dict[tuple[str, str, str], dict[str, Any]] = {}
    for o in active:
        t = o["target"]
        by_target[(t["chapter"], t["kind"], t.get("key") or "")] = o

    for ch in chapters:
        cid = ch["id"]
        ov = by_target.get((cid, "prose", ""))
        ch["proseEffective"] = ov["value"] if ov else ch.get("prose")
        ch["proseOverride"] = _mark(ov) if ov else None

        for f in ch.get("facts") or []:
            ov = by_target.get((cid, "fact", f["key"]))
            f["valueEffective"] = ov["value"] if ov else f.get("value")
            f["override"] = _mark(ov) if ov else None
        for g in ch.get("grades") or []:
            ov = by_target.get((cid, "grade", g["key"]))
            g["gradeEffective"] = ov["value"] if ov else g.get("grade")
            g["override"] = _mark(ov) if ov else None
        for it in ch.get("items") or []:
            ov = by_target.get((cid, "item", it["id"]))
            it["textEffective"] = ov["value"] if ov else it.get("text")
            it["override"] = _mark(ov) if ov else None

    return {
        "header": document.get("header") or {},
        "chapters": chapters,
        "overrideCount": len(active),
        "staleOverrideCount": sum(1 for o in active if o.get("stale")),
        "reviewState": document.get("reviewState"),
        "reviewedBy": document.get("reviewedBy"),
        "reviewedAt": document.get("reviewedAt"),
        "generation": document.get("generation"),
        "generatedAt": document.get("generatedAt"),
        "status": document.get("status"),
        "disclaimerZh": document.get("disclaimerZh"),
        "titleZh": document.get("titleZh"),
    }


def find_target(chapters: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, Any] | None:
    """The element an override points at, or None if it does not exist.

    Overrides are keyed, never indexed, so a regenerated chapter with a
    different element order still resolves the same target.
    """
    cid, kind, key = target.get("chapter"), target.get("kind"), target.get("key") or ""
    for ch in chapters:
        if ch["id"] != cid:
            continue
        if kind == "prose":
            return {"value": ch.get("prose"), "exists": ch.get("prose") is not None
                    or ch.get("source") in ("computed", "composed", "model")}
        coll = {"fact": "facts", "grade": "grades", "item": "items"}.get(kind)
        if not coll:
            return None
        idkey = "id" if kind == "item" else "key"
        for el in ch.get(coll) or []:
            if el.get(idkey) == key:
                vkey = {"fact": "value", "grade": "grade", "item": "text"}[kind]
                return {"value": el.get(vkey), "exists": True}
        return None
    return None
