"""Dataset adapters: how one folder tree maps to patients / studies / series,
and how a segmentation file gets joined to the series it belongs to.

Every adapter yields plain records; the scan runner does all the DB work. Each
segmentation carries an ``association_rule`` string so a wrong join can be
audited later instead of silently pairing a label set with the wrong knee.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol


@dataclass
class SeriesCandidate:
    """One directory that looks like a DICOM series, or one NIfTI image file."""
    path: Path                       # absolute
    patient_external_id: str
    patient_name: str | None = None
    patient_extra: dict[str, Any] = field(default_factory=dict)
    study_key: str | None = None     # groups series into a study when UIDs are absent
    study_description: str | None = None
    kind: str = "dicom_dir"          # dicom_dir | nifti_file
    hints: dict[str, Any] = field(default_factory=dict)


@dataclass
class SegmentationCandidate:
    path: Path                       # absolute
    patient_external_id: str
    kind: str = "nifti"              # nifti | nifti_binary_set | dicom_seg
    origin: str = "ground_truth"     # ground_truth | model | manual
    display_name: str = "标注"
    label_set: str | None = None
    association_rule: str = ""
    series_hint: str | None = None   # match a specific series path when ambiguous
    components: dict[int, Path] = field(default_factory=dict)  # for nifti_binary_set
    model_name: str | None = None
    model_version: str | None = None


class Adapter(Protocol):
    key: str

    def scan_series(self) -> Iterator[SeriesCandidate]:
        ...

    def scan_segmentations(self) -> Iterator[SegmentationCandidate]:
        ...

    def patient_metadata(self) -> dict[str, dict[str, Any]]:
        """external_id -> {name, age, sex, group_name, ...}"""
        ...


class BaseAdapter:
    """Shared helpers. Subclasses override the three protocol methods."""

    key = "base"

    def __init__(self, root: Path, cfg: Any):
        self.root = Path(root)
        self.cfg = cfg

    # -- helpers ---------------------------------------------------------
    def rel(self, p: Path) -> str:
        """Path relative to the dataset root, POSIX-style.

        Only ``dataset.root_path`` is absolute in the DB, so the whole index
        survives the drive being mounted somewhere else on another machine.
        """
        return Path(os.path.relpath(Path(p), self.root)).as_posix()

    def patient_metadata(self) -> dict[str, dict[str, Any]]:
        return {}

    def scan_segmentations(self) -> Iterator[SegmentationCandidate]:
        return iter(())

    def _subdirs(self, path: Path) -> list[Path]:
        try:
            return sorted((Path(e.path) for e in os.scandir(path) if e.is_dir()),
                          key=lambda p: p.name)
        except OSError:
            return []
