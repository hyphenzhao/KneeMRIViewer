"""Label-set registry.

YAML on disk is the seed; the database is the runtime source of truth so a
rename in the UI persists. ``export_labelset`` writes the DB state back to YAML.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .db.session import upsert

# Distinct, colour-blind-tolerant fallback palette for label sets that arrive
# without one (e.g. a model's output we have never seen before).
FALLBACK_PALETTE = [
    "#E6194B", "#3CB44B", "#FFE119", "#4363D8", "#F58231", "#911EB4",
    "#42D4F4", "#F032E6", "#BFEF45", "#FABED4", "#469990", "#DCBEFF",
    "#9A6324", "#FFFAC8", "#800000", "#AAFFC3", "#808000", "#FFD8B1",
    "#000075", "#A9A9A9",
]


def load_labelset_files(conn: sqlite3.Connection, labelsets_dir: Path) -> list[str]:
    """Seed the DB from every YAML in the directory. Existing rows are updated
    only for fields the YAML still owns; UI renames survive because we only
    insert labels that are missing."""
    seeded: list[str] = []
    for path in sorted(Path(labelsets_dir).glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not doc or "key" not in doc:
            continue
        ls_id = upsert(conn, "label_set", ["key"], {
            "key": doc["key"],
            "name": doc.get("name", doc["key"]),
            "name_en": doc.get("name_en"),
            "source_path": str(path),
        })
        for order, lab in enumerate(doc.get("labels", [])):
            exists = conn.execute(
                "SELECT id FROM label_def WHERE label_set_id=? AND value=?",
                (ls_id, int(lab["value"])),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO label_def(label_set_id,value,name,name_zh,color_hex,"
                "opacity,structure_group,sort_order) VALUES(?,?,?,?,?,?,?,?)",
                (ls_id, int(lab["value"]), lab.get("name", "Label %d" % lab["value"]),
                 lab.get("name_zh"), lab.get("color", FALLBACK_PALETTE[order % 20]),
                 float(lab.get("opacity", 0.7)), lab.get("group"), order),
            )
        seeded.append(doc["key"])
    conn.commit()
    return seeded


def ensure_labelset_for_values(conn: sqlite3.Connection, key: str, name: str,
                               values: list[int]) -> int:
    """Create a placeholder label set for an unseen segmentation.

    Used for model output whose label meanings we do not know yet: the viewer
    still renders it, with ``Label N`` names the user can rename in place.
    """
    ls_id = upsert(conn, "label_set", ["key"], {"key": key, "name": name})
    for order, v in enumerate(sorted(set(int(x) for x in values) - {0})):
        exists = conn.execute(
            "SELECT id FROM label_def WHERE label_set_id=? AND value=?", (ls_id, v)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO label_def(label_set_id,value,name,color_hex,opacity,sort_order)"
                " VALUES(?,?,?,?,?,?)",
                (ls_id, v, "Label %d" % v, FALLBACK_PALETTE[order % 20], 0.7, order),
            )
    conn.commit()
    return ls_id


def get_labelset(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM label_set WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    labs = conn.execute(
        "SELECT value,name,name_zh,color_hex,opacity,structure_group FROM label_def"
        " WHERE label_set_id=? ORDER BY COALESCE(sort_order, value)", (row["id"],)
    ).fetchall()
    return {
        "key": row["key"],
        "name": row["name"],
        "nameEn": row["name_en"],
        "labels": [
            {
                "value": r["value"],
                "name": r["name"],
                "nameZh": r["name_zh"],
                "color": r["color_hex"],
                "opacity": r["opacity"],
                "group": r["structure_group"],
            }
            for r in labs
        ],
    }


def list_labelsets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT key FROM label_set ORDER BY key").fetchall()
    return [get_labelset(conn, r["key"]) for r in rows]  # type: ignore[misc]


def update_label(conn: sqlite3.Connection, key: str, value: int,
                 name: str | None = None, name_zh: str | None = None,
                 color: str | None = None, opacity: float | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT id FROM label_set WHERE key=?", (key,)).fetchone()
    if not row:
        raise KeyError(key)
    sets, params = [], []
    for col, val in (("name", name), ("name_zh", name_zh),
                     ("color_hex", color), ("opacity", opacity)):
        if val is not None:
            sets.append(col + "=?")
            params.append(val)
    if sets:
        params += [row["id"], int(value)]
        conn.execute("UPDATE label_def SET " + ",".join(sets) +
                     " WHERE label_set_id=? AND value=?", params)
        conn.commit()
    return get_labelset(conn, key)  # type: ignore[return-value]


def export_labelset(conn: sqlite3.Connection, key: str, out_path: Path) -> Path:
    ls = get_labelset(conn, key)
    if ls is None:
        raise KeyError(key)
    doc = {
        "key": ls["key"], "name": ls["name"], "name_en": ls["nameEn"],
        "labels": [
            {"value": l["value"], "name": l["name"], "name_zh": l["nameZh"],
             "color": l["color"], "opacity": l["opacity"], "group": l["group"]}
            for l in ls["labels"]
        ],
    }
    out_path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    return out_path
