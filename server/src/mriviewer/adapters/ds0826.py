"""The 0826 knee cartilage set - 157 cases, 24 of them annotated.

Layout::

    0826/dicom/<N>/pd_fse_sag_fs_ACS_<time>_<series>/*.dcm     N = 1..157
    0826/segmentation label/<N>_all_labels-label-label.nii     24 of them
    0826/patient list.xlsx                                     序号 / 姓名 / 年龄

The join is on the case index ``N`` and nothing else, so it is validated hard:
the labelmap must permute to exactly the series dimensions or the association
is refused (see ``seg.nifti_io.derive_transform``). Case 24 has 32 slices and
case 28 has 33, which makes that check a real discriminator against an
off-by-one in ``N``.

Two consecutive 序号 usually share a patient name - the left and right knee of
one person. They are kept as **separate** patients (laterality is per-scan and
we record it), linked by ``group_name``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

from .base import BaseAdapter, SegmentationCandidate, SeriesCandidate

LABEL_RE = re.compile(r"^(\d+)_all_labels-label-label\.nii(\.gz)?$", re.IGNORECASE)


class Ds0826Adapter(BaseAdapter):
    key = "ds0826"

    @property
    def dicom_root(self) -> Path:
        d = self.root / "dicom"
        return d if d.is_dir() else self.root

    @property
    def label_root(self) -> Path:
        return self.root / "segmentation label"

    def scan_series(self) -> Iterator[SeriesCandidate]:
        meta = self.patient_metadata()
        for case_dir in self._subdirs(self.dicom_root):
            if not case_dir.name.isdigit():
                continue
            n = case_dir.name
            info = meta.get(n, {})
            for series_dir in self._subdirs(case_dir):
                yield SeriesCandidate(
                    path=series_dir,
                    patient_external_id=n,
                    patient_name=info.get("name"),
                    patient_extra=info,
                    study_key=n,
                    study_description=series_dir.name,
                )

    def scan_segmentations(self) -> Iterator[SegmentationCandidate]:
        if not self.label_root.is_dir():
            return
        for entry in sorted(os.scandir(self.label_root), key=lambda e: e.name):
            if not entry.is_file():
                continue
            m = LABEL_RE.match(entry.name)
            if not m:
                continue
            yield SegmentationCandidate(
                path=Path(entry.path),
                patient_external_id=m.group(1),
                kind="nifti",
                origin="ground_truth",
                display_name="原始标注",
                label_set=self.cfg.label_set or "knee_cartilage_0826_v1",
                association_rule="ds0826.case_index",
            )

    def patient_metadata(self) -> dict[str, dict[str, Any]]:
        xlsx = self.cfg.metadata_xlsx or (self.root / "patient list.xlsx")
        p = Path(xlsx)
        if not p.is_file():
            return {}
        try:
            import openpyxl
        except ImportError:
            return {}
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(rows, ())]

        def col(*names: str) -> int | None:
            for nm in names:
                if nm in header:
                    return header.index(nm)
            return None

        i_idx = col("序号", "index", "No")
        i_name = col("DICOM患者姓名", "姓名", "name")
        i_age = col("年龄", "age")
        if i_idx is None:
            return {}

        out: dict[str, dict[str, Any]] = {}
        name_counts: dict[str, list[str]] = {}
        for row in rows:
            if row is None or i_idx >= len(row) or row[i_idx] is None:
                continue
            key = str(row[i_idx]).strip()
            if key.endswith(".0"):
                key = key[:-2]
            name = str(row[i_name]).strip() if i_name is not None and i_name < len(row) \
                and row[i_name] is not None else None
            age = str(row[i_age]).strip() if i_age is not None and i_age < len(row) \
                and row[i_age] is not None else None
            if age and age.endswith(".0"):
                age = age[:-2]
            out[key] = {"name": name, "age": age, "group_name": name}
            if name:
                name_counts.setdefault(name, []).append(key)
        wb.close()

        # A person with two cases: label them so the UI can say "case 3 / case 4".
        for name, keys in name_counts.items():
            if len(keys) > 1:
                for pos, k in enumerate(sorted(keys, key=int)):
                    out[k]["sibling_cases"] = sorted(keys, key=int)
                    out[k]["sibling_index"] = pos
        return out
