"""The chapter template is data doctors edit; loading it must be strict."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from mriviewer.report.template import load_template

REPO_REPORTS = Path(__file__).resolve().parents[1] / "reports"


@dataclass
class Cfg:
    reports_dir: Path


def test_shipped_template_loads_in_hospital_order() -> None:
    tpl = load_template(Cfg(REPO_REPORTS), "knee_zh_v1")
    ids = [c.id for c in tpl.chapters]
    assert ids == ["overview", "bone", "cartilage", "effusion_synovium", "meniscus",
                   "ligament_tendon", "cyst", "soft_tissue", "impression", "advice",
                   "quant_appendix"]
    assert [c.order for c in tpl.chapters] == sorted(c.order for c in tpl.chapters)


def test_banned_terms_are_per_chapter() -> None:
    tpl = load_template(Cfg(REPO_REPORTS), "knee_zh_v1")
    assert "半月板" in tpl.banned_terms_for("cartilage")
    assert "半月板" not in tpl.chapter("meniscus").banned_terms
    assert "确诊" in tpl.banned_terms_for("meniscus")          # global still applies


def test_pending_chapters_carry_a_placeholder_and_no_prose_source() -> None:
    tpl = load_template(Cfg(REPO_REPORTS), "knee_zh_v1")
    for c in tpl.chapters:
        if c.source == "pending":
            assert c.placeholder_zh and "未评估" in c.placeholder_zh, c.id
            assert not c.ai_enabled, c.id


def test_only_cartilage_uses_the_model_for_now() -> None:
    tpl = load_template(Cfg(REPO_REPORTS), "knee_zh_v1")
    assert [c.id for c in tpl.chapters if c.ai_enabled] == ["cartilage"]


def _write(tmp_path: Path, body: str) -> Cfg:
    (tmp_path / "t.yaml").write_text(body, encoding="utf-8")
    return Cfg(tmp_path)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
key: t
chapters:
  - {id: a, order: 1, section: findings, title_zh: A, source: pending, placeholder_zh: x}
  - {id: a, order: 2, section: findings, title_zh: B, source: pending, placeholder_zh: x}
""")
    with pytest.raises(ValueError, match="duplicate"):
        load_template(cfg, "t")


def test_pending_without_placeholder_is_rejected(tmp_path: Path) -> None:
    """A pending chapter with nothing to show would render as silence."""
    cfg = _write(tmp_path, """
key: t
chapters:
  - {id: a, order: 1, section: findings, title_zh: A, source: pending}
""")
    with pytest.raises(ValueError, match="placeholder"):
        load_template(cfg, "t")


def test_unknown_source_is_rejected(tmp_path: Path) -> None:
    cfg = _write(tmp_path, """
key: t
chapters:
  - {id: a, order: 1, section: findings, title_zh: A, source: magic}
""")
    with pytest.raises(ValueError, match="source"):
        load_template(cfg, "t")


def test_bad_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_template(Cfg(tmp_path), "../etc/passwd")
