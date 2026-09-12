"""Splitting the hospital's free-text report into chapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mriviewer.report.radiologist import (classify, is_copy_paste, laterality_of,
                                          prefill, split_sentences)
from mriviewer.report.template import load_template

REPO_REPORTS = Path(__file__).resolve().parents[1] / "reports"


@dataclass
class Cfg:
    reports_dir: Path


TPL = load_template(Cfg(REPO_REPORTS), "knee_zh_v1")

# The sample's boilerplate findings, with a cyst sentence spliced in.
FINDINGS = ("左侧膝关节各组成骨皮质光整，骨质信号未见异常改变。所示关节软骨面光整，未见明显缺损。"
            "髌上囊及关节腔内见少量长T长T2信号积液，滑膜未见明显增厚。"
            "外侧半月板体部异常信号伴周围囊性灶，膝关节内侧半月板形态可，信号未见明显异常。"
            "髌韧带、股四头肌腱、前后交叉韧带、内外侧副韧带形态信号无异常。"
            "腘肌旁滑膜囊肿。膝关节周围软组织信号无异常。")
IMPRESSION = "左膝外侧半月板体部撕裂伴半月板囊肿形成，膝关节腔及髌上囊少量积液，请结合临床其他检查，随访。"


def test_sentences_split_on_full_stop() -> None:
    s = split_sentences(FINDINGS)
    assert len(s) == 7
    assert all(x.endswith("。") for x in s)


def test_sentences_land_in_the_right_chapters() -> None:
    buckets = classify(split_sentences(FINDINGS), TPL)
    assert any("骨皮质" in s for s in buckets["bone"])
    assert any("软骨" in s for s in buckets["cartilage"])
    assert any("积液" in s for s in buckets["effusion_synovium"])
    assert any("半月板" in s for s in buckets["meniscus"])
    assert any("韧带" in s for s in buckets["ligament_tendon"])
    assert any("软组织" in s for s in buckets["soft_tissue"])


def test_first_matching_chapter_wins() -> None:
    """腘肌旁滑膜囊肿 matches both 滑膜 (effusion) and 囊肿 (cyst); chapter order decides."""
    buckets = classify(["腘肌旁滑膜囊肿。"], TPL)
    assert "腘肌旁滑膜囊肿。" in buckets["effusion_synovium"]


def test_unmatched_sentences_are_kept_not_dropped() -> None:
    buckets = classify(["火星上的天气很好。"], TPL)
    assert buckets["other"] == ["火星上的天气很好。"]


def test_copy_paste_detection() -> None:
    assert is_copy_paste(IMPRESSION, IMPRESSION)
    assert not is_copy_paste(FINDINGS, IMPRESSION)
    assert not is_copy_paste(None, IMPRESSION)


def test_laterality_from_text() -> None:
    assert laterality_of(FINDINGS) == "L"
    assert laterality_of("右膝髌骨软化症") == "R"
    assert laterality_of("侧膝关节各组成骨皮质光整") is None       # sample row 121


class Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


def test_prefill_uses_impression_only_for_copy_paste_rows() -> None:
    p = prefill(Row(findings=IMPRESSION, diagnosis=IMPRESSION), TPL)
    assert p["copyPaste"] is True
    assert p["byChapter"] == {}
    assert p["impressionSentences"]
    assert not any("请结合临床" in s for s in p["impressionSentences"])


def test_prefill_full_row() -> None:
    p = prefill(Row(findings=FINDINGS, diagnosis=IMPRESSION), TPL)
    assert p["copyPaste"] is False
    assert "meniscus" in p["byChapter"]
    assert p["laterality"] == "L"
    assert p["labelZh"] == "放射科报告"
