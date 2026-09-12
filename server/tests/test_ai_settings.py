"""Runtime AI settings, and the one-report-per-segmentation rule.

The migration test matters most: the unique index cannot be created while a
database still holds the old append-only rows, so an upgrade of a live install
fails at startup if the collapse does not run first.
"""
from __future__ import annotations

import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mriviewer.ai.settings import (read_settings, resolved_ai, write_api_key,
                                   write_settings)
from mriviewer.config import AiConfig
from mriviewer.db.session import _migrate_ai_report_single_row, init_db


@dataclass
class FakeCfg:
    state_dir: Path
    ai: AiConfig = field(default_factory=AiConfig)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = init_db(tmp_path / "index.sqlite")
    yield c
    c.close()


# ------------------------------------------------------------- migration

def _legacy_db(path: Path) -> sqlite3.Connection:
    """A database as it looked when reports were append-only."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE ai_report(id INTEGER PRIMARY KEY, segmentation_id INTEGER,"
        " version_int INTEGER, status TEXT, findings TEXT);"
        "INSERT INTO ai_report VALUES (1, 18, 1, 'fallback', 'old text');"
        "INSERT INTO ai_report VALUES (2, 18, 2, 'ok', 'new text');"
        "INSERT INTO ai_report VALUES (3, 19, 1, 'ok', 'other case');")
    c.commit()
    return c


def test_migration_keeps_only_the_newest_report(tmp_path: Path) -> None:
    c = _legacy_db(tmp_path / "legacy.sqlite")
    _migrate_ai_report_single_row(c)
    rows = c.execute(
        "SELECT segmentation_id, version_int, findings FROM ai_report"
        " ORDER BY segmentation_id").fetchall()
    assert [tuple(r) for r in rows] == [(18, 2, "new text"), (19, 1, "other case")]
    c.close()


def test_unique_index_can_be_created_after_migration(tmp_path: Path) -> None:
    """This is the upgrade path that would otherwise fail at startup."""
    c = _legacy_db(tmp_path / "legacy2.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("CREATE UNIQUE INDEX ux ON ai_report(segmentation_id)")
    _migrate_ai_report_single_row(c)
    c.execute("CREATE UNIQUE INDEX ux ON ai_report(segmentation_id)")   # no raise
    c.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    c = _legacy_db(tmp_path / "legacy3.sqlite")
    _migrate_ai_report_single_row(c)
    before = c.execute("SELECT count(*) FROM ai_report").fetchone()[0]
    _migrate_ai_report_single_row(c)
    assert c.execute("SELECT count(*) FROM ai_report").fetchone()[0] == before
    c.close()


def test_migration_tolerates_a_database_without_the_table(tmp_path: Path) -> None:
    c = sqlite3.connect(tmp_path / "empty.sqlite")
    _migrate_ai_report_single_row(c)          # must not raise
    c.close()


def test_fresh_schema_has_the_unique_index(conn: sqlite3.Connection) -> None:
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "ux_ai_report_seg" in names


# -------------------------------------------------------------- settings

def test_no_overrides_means_the_file_wins(conn, tmp_path: Path) -> None:
    cfg = FakeCfg(state_dir=tmp_path)
    cfg.ai.model = "from-file"
    cfg.ai.base_url = "http://127.0.0.1:11434/v1"
    assert read_settings(conn) == {}
    assert resolved_ai(conn, cfg).model == "from-file"


def test_database_overrides_the_file(conn, tmp_path: Path) -> None:
    cfg = FakeCfg(state_dir=tmp_path)
    cfg.ai.model = "from-file"
    write_settings(conn, {"model": "from-db", "enabled": True})
    resolved = resolved_ai(conn, cfg)
    assert resolved.model == "from-db"
    assert resolved.enabled is True


def test_resolving_never_mutates_the_loaded_config(conn, tmp_path: Path) -> None:
    """A bad row must not permanently corrupt the process's own config."""
    cfg = FakeCfg(state_dir=tmp_path)
    cfg.ai.model = "from-file"
    write_settings(conn, {"model": "from-db"})
    resolved_ai(conn, cfg)
    assert cfg.ai.model == "from-file"


def test_partial_update_leaves_other_fields_alone(conn, tmp_path: Path) -> None:
    write_settings(conn, {"model": "m1", "base_url": "http://a/v1"})
    write_settings(conn, {"model": "m2"})
    stored = read_settings(conn)
    assert stored["model"] == "m2"
    assert stored["base_url"] == "http://a/v1"


def test_timeout_is_clamped(conn, tmp_path: Path) -> None:
    write_settings(conn, {"timeout_s": 99999})
    assert read_settings(conn)["timeout_s"] == 600.0
    write_settings(conn, {"timeout_s": 0})
    assert read_settings(conn)["timeout_s"] == 1.0


def test_trailing_slash_is_stripped_from_base_url(conn, tmp_path: Path) -> None:
    """Otherwise the client builds '…/v1//chat/completions'."""
    write_settings(conn, {"base_url": "https://api.example.com/v1/"})
    assert read_settings(conn)["base_url"] == "https://api.example.com/v1"


def test_settings_never_carry_the_api_key(conn, tmp_path: Path) -> None:
    write_settings(conn, {"model": "m"})
    assert "api_key" not in read_settings(conn)
    assert not any("key" in k for k in read_settings(conn))


def test_patient_context_is_not_editable_from_the_ui(conn, tmp_path: Path) -> None:
    """What leaves the building is a reviewed edit on disk, not a click."""
    cfg = FakeCfg(state_dir=tmp_path)
    write_settings(conn, {"patient_context": ["name", "ageBand"]})
    assert resolved_ai(conn, cfg).patient_context == ["ageBand", "sex", "laterality"]


# ------------------------------------------------------------- key file

def test_api_key_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "ai.key"
    write_api_key(path, "sk-secret\n")
    assert path.read_text(encoding="utf-8") == "sk-secret"
    mode = stat.S_IMODE(path.stat().st_mode)
    if hasattr(stat, "S_IRGRP"):
        assert not mode & (stat.S_IRGRP | stat.S_IROTH), oct(mode)


def test_rewriting_a_key_keeps_the_file_private(tmp_path: Path) -> None:
    """An existing world-readable file must be tightened, not left as found."""
    path = tmp_path / "ai.key"
    path.write_text("old", encoding="utf-8")
    path.chmod(0o644)
    write_api_key(path, "sk-new")
    mode = stat.S_IMODE(path.stat().st_mode)
    if hasattr(stat, "S_IRGRP"):
        assert not mode & (stat.S_IRGRP | stat.S_IROTH), oct(mode)


def test_key_file_defaults_into_the_state_dir(conn, tmp_path: Path) -> None:
    """A UI-set key needs somewhere to live when the file named no path."""
    cfg = FakeCfg(state_dir=tmp_path)
    cfg.ai.api_key_file = None
    assert resolved_ai(conn, cfg).api_key_file == tmp_path / "ai.key"


def test_a_configured_key_path_is_respected(conn, tmp_path: Path) -> None:
    cfg = FakeCfg(state_dir=tmp_path)
    cfg.ai.api_key_file = tmp_path / "custom.key"
    assert resolved_ai(conn, cfg).api_key_file == tmp_path / "custom.key"
