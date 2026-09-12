"""SQLite access. The API process opens read-only; the worker is the sole writer."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "3"
_SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_path: Path, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    db_path = Path(db_path)
    if readonly:
        conn = sqlite3.connect(
            "file:" + db_path.as_posix() + "?mode=ro",
            uri=True, timeout=timeout, check_same_thread=False,
        )
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=timeout, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    _migrate_report_fts(conn)
    _migrate_ai_report_single_row(conn)
    _migrate_add_columns(conn)
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT OR REPLACE INTO kv(key,value) VALUES('schema_version',?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    return conn


def upsert(conn: sqlite3.Connection, table: str, keys: Iterable[str],
           values: dict[str, Any]) -> int:
    """Insert-or-update on a unique key tuple; returns the row id."""
    keys = list(keys)
    cols = list(values)
    placeholders = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    conflict = ",".join(keys)
    updates = ",".join(c + "=excluded." + c for c in cols if c not in keys)
    if updates:
        sql = ("INSERT INTO " + table + "(" + collist + ") VALUES(" + placeholders + ")"
               " ON CONFLICT(" + conflict + ") DO UPDATE SET " + updates)
    else:
        sql = ("INSERT INTO " + table + "(" + collist + ") VALUES(" + placeholders + ")"
               " ON CONFLICT(" + conflict + ") DO NOTHING")
    conn.execute(sql, [values[c] for c in cols])
    where = " AND ".join(k + "=?" for k in keys)
    row = conn.execute("SELECT id FROM " + table + " WHERE " + where,
                       [values[k] for k in keys]).fetchone()
    return int(row["id"])


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def all_rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


# Columns added to existing tables after their first release. CREATE TABLE IF
# NOT EXISTS does nothing for a table that is already there, so each one is
# added here when missing. (table, column, declaration)
_ADDED_COLUMNS = (
    ("segmentation_morphometry", "figures_json", "TEXT"),
)


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                              (table,)).fetchone()
        if not exists:
            continue
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if column not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))
    conn.commit()


def _migrate_ai_report_single_row(conn: sqlite3.Connection) -> None:
    """Collapse historical multi-version AI reports to one row per segmentation.

    Reports used to be append-only, one row per generation. They are now a
    single replaceable row, which the schema enforces with a unique index - and
    that index cannot be created while duplicates exist. So this runs *before*
    the schema script and keeps the newest generation of each segmentation.

    Idempotent: on a database that already has at most one row per
    segmentation it does nothing.
    """
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ai_report'"
    ).fetchone()
    if not exists:
        return
    conn.execute(
        "DELETE FROM ai_report WHERE id NOT IN ("
        "  SELECT id FROM ("
        "    SELECT id, ROW_NUMBER() OVER ("
        "      PARTITION BY segmentation_id"
        "      ORDER BY version_int DESC, id DESC) AS rn"
        "    FROM ai_report) WHERE rn = 1)")
    conn.commit()


def _migrate_report_fts(conn: sqlite3.Connection) -> None:
    """Drop the FTS5 mirror if an older database still carries one.

    It was removed because unicode61 cannot tokenise Chinese usefully; report
    search is a LIKE scan over `report` now. Pure index, nothing is lost.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='report_fts'"
    ).fetchone()
    if row:
        conn.execute("DROP TABLE report_fts")
        conn.commit()
