"""Runtime AI settings: config file supplies defaults, the database overrides.

Why both. The TOML file is what an air-gapped install ships with and what an
operator controls on disk; the database row is what the web UI can change so
swapping in an in-house model does not mean editing TOML and restarting the
service. A NULL column means "not set here - use the file".

The API key never lives in either. It is written to a 0600 file, read at request
time, and never returned by any endpoint - so it cannot be recovered through the
API, and a database backup does not carry it.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..config import AiConfig

# Columns the UI may set. `patient_context` and `prompts_dir` are deliberately
# absent: they decide what leaves the building and which prompt is used, and
# changing those should be a reviewed edit on disk, not a click.
EDITABLE = ("enabled", "allow_egress", "base_url", "model", "timeout_s")


def read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    """The stored overrides, or an empty dict when nothing has been set."""
    try:
        row = conn.execute("SELECT * FROM ai_setting WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    return {k: row[k] for k in EDITABLE if row[k] is not None}


def resolved_ai(conn: sqlite3.Connection, cfg: Any) -> AiConfig:
    """The AI configuration actually in force, file merged with database.

    Returns a copy - never mutates ``cfg.ai``, so a bad database row cannot
    permanently corrupt the process's idea of its own configuration.
    """
    ai: AiConfig = cfg.ai
    stored = read_settings(conn)
    merged = replace(
        ai,
        enabled=bool(stored["enabled"]) if "enabled" in stored else ai.enabled,
        allow_egress=(bool(stored["allow_egress"]) if "allow_egress" in stored
                      else ai.allow_egress),
        base_url=stored.get("base_url") or ai.base_url,
        model=stored.get("model") or ai.model,
        timeout_s=float(stored["timeout_s"]) if "timeout_s" in stored else ai.timeout_s,
    )
    if merged.api_key_file is None:
        # Somewhere for a UI-provided key to live when the file did not name one.
        merged = replace(merged, api_key_file=Path(cfg.state_dir) / "ai.key")
    return merged


def write_settings(conn: sqlite3.Connection, patch: dict[str, Any],
                   *, updated_by: str = "") -> None:
    """Persist the editable subset. Keys absent from ``patch`` are left alone."""
    fields = {k: patch[k] for k in EDITABLE if k in patch}
    if not fields:
        return
    for flag in ("enabled", "allow_egress"):
        if flag in fields:
            fields[flag] = 1 if fields[flag] else 0
    if "timeout_s" in fields:
        fields["timeout_s"] = max(1.0, min(600.0, float(fields["timeout_s"])))
    if "base_url" in fields:
        fields["base_url"] = str(fields["base_url"]).strip().rstrip("/")

    cols = list(fields)
    conn.execute(
        "INSERT INTO ai_setting (id, %s, updated_at, updated_by)"
        " VALUES (1, %s, datetime('now'), ?)"
        " ON CONFLICT(id) DO UPDATE SET %s,"
        " updated_at=datetime('now'), updated_by=excluded.updated_by"
        % (", ".join(cols), ", ".join("?" for _ in cols),
           ", ".join(c + "=excluded." + c for c in cols)),
        [*fields.values(), updated_by[:120]])
    conn.commit()


def write_api_key(path: Path, key: str) -> None:
    """Write the key with 0600, created private before anything is written.

    Opening with the mode in ``os.open`` matters: creating the file first and
    chmod-ing afterwards leaves a window in which the secret is world-readable.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.strip().encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)       # in case the file already existed


def clear_api_key(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
