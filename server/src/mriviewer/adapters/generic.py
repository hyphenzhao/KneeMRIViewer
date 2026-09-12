"""Adapters for the folder-shaped DICOM datasets.

* ``changzheng`` - ``P####/<series dir>/*.dcm`` plus a de-identified report CSV
* ``fspdw``      - ``<name>/<YYYYMMDDHHMMSS>/<series no>/*.dcm``
* ``bonescan``   - arbitrarily nested DICOM in UID-named folders, mixed with
  non-DICOM Interfile companions; found by sniffing the DICM magic
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from ..dicomio.probe import looks_like_series_dir
from .base import BaseAdapter, SeriesCandidate

# Depth guard for the sniffing walker: deep enough for Bone Scan's
# <tree>/<patient>/<study>/<uid-series>, shallow enough not to crawl forever.
MAX_SNIFF_DEPTH = 6


class ChangzhengAdapter(BaseAdapter):
    """2485 knee MR cases; series folders mix in non-image DICOM objects."""

    key = "changzheng"

    def scan_series(self) -> Iterator[SeriesCandidate]:
        for patient_dir in self._subdirs(self.root):
            if not patient_dir.name.upper().startswith("P"):
                continue
            for series_dir in self._subdirs(patient_dir):
                yield SeriesCandidate(
                    path=series_dir,
                    patient_external_id=patient_dir.name,
                    study_key=patient_dir.name,
                    study_description=series_dir.name,
                )


class FsPdwAdapter(BaseAdapter):
    """893 patients keyed by name, each with one or more dated studies."""

    key = "fspdw"

    def scan_series(self) -> Iterator[SeriesCandidate]:
        for patient_dir in self._subdirs(self.root):
            for study_dir in self._subdirs(patient_dir):
                subs = self._subdirs(study_dir)
                if subs:
                    for series_dir in subs:
                        yield SeriesCandidate(
                            path=series_dir,
                            patient_external_id=patient_dir.name,
                            patient_name=patient_dir.name,
                            study_key=patient_dir.name + "/" + study_dir.name,
                            study_description=study_dir.name,
                        )
                elif looks_like_series_dir(study_dir):
                    yield SeriesCandidate(
                        path=study_dir,
                        patient_external_id=patient_dir.name,
                        patient_name=patient_dir.name,
                        study_key=patient_dir.name + "/" + study_dir.name,
                        study_description=study_dir.name,
                    )


class SniffAdapter(BaseAdapter):
    """Find series by DICM magic wherever they are.

    Bone Scan nests DICOM in UID-named folders several levels down, mixes
    SPECT/CT volumes with planar NM images, and keeps non-DICOM Interfile
    companions (.A00/.I00) beside them.

    The patient level has to be stated, not guessed. This tree is::

        Bone Scan/Bone(HD)/Bone/<patient dir>/<uid dirs>/
        Bone Scan/Bone(CZ)/<batch>/<hash>/<uid dirs>/

    i.e. dataset root -> collection -> batch -> **patient**, so
    ``patient_depth = 3``. Guessing "the first level below the root" gives 7
    patients holding 27 886 series between them, which is useless in a patient
    list. Set ``patient_depth`` in the dataset config.
    """

    key = "sniff"
    DEFAULT_PATIENT_DEPTH = 2

    @property
    def patient_depth(self) -> int:
        extra = getattr(self.cfg, "extra", None) or {}
        return int(extra.get("patient_depth", self.DEFAULT_PATIENT_DEPTH))

    def _dirs_at_depth(self, depth: int) -> list[Path]:
        level = [self.root]
        for _ in range(depth):
            nxt: list[Path] = []
            for d in level:
                nxt.extend(self._subdirs(d))
            level = nxt
        return level

    def scan_series(self) -> Iterator[SeriesCandidate]:
        for patient_dir in self._dirs_at_depth(self.patient_depth):
            external_id = self.rel(patient_dir)
            for series_dir in self._walk_series(patient_dir, 0):
                rel = os.path.relpath(series_dir, patient_dir)
                parts = Path(rel).parts
                yield SeriesCandidate(
                    path=series_dir,
                    patient_external_id=external_id,
                    patient_name=patient_dir.name,
                    # One study per intermediate folder, so a patient's SPECT/CT
                    # and planar acquisitions do not collapse into one study.
                    study_key=external_id + "/" + (parts[0] if len(parts) > 1 else ""),
                    study_description=parts[0] if len(parts) > 1 else None,
                )

    def _walk_series(self, base: Path, depth: int) -> Iterator[Path]:
        if depth > MAX_SNIFF_DEPTH:
            return
        if looks_like_series_dir(base):
            yield base
            return                                    # do not descend into a series
        for sub in self._subdirs(base):
            yield from self._walk_series(sub, depth + 1)
