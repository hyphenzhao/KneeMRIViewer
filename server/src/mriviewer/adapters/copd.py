"""COPD chest CT - NIfTI-native, with two independent segmentation layers.

Layout::

    IMAGE/<hospital>-FoU_anonymize_NII/<patient>/lung image.nii.gz
    IMAGE/<hospital>-FoU_anonymize_NII/<patient>/lung image_*_seg/*.nii.gz   per-structure binaries
    IMAGE/<hospital>_lung_masks/<patient>/*_merged_mask.nii.gz               multi-label (0/1/2)

The per-structure binary masks are merged into a single multi-value labelmap at
ingest: Cornerstone's labelmap is one scalar volume, so thirty files have to
become thirty *values*, not thirty segmentations.

These volumes are large (768x768x320 int16 is 377 MB), which is why the dataset
carries a ``delivery_profile`` - the scan runner downsamples before caching.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from .base import BaseAdapter, SegmentationCandidate, SeriesCandidate

IMAGE_NAME = "lung image.nii.gz"
SEG_DIR_RE = re.compile(r"^lung image_(.+)_seg$")


class CopdAdapter(BaseAdapter):
    key = "copd_nifti"

    def _image_dirs(self) -> Iterator[tuple[str, Path]]:
        for hosp in self._subdirs(self.root):
            if not hosp.name.endswith("_NII"):
                continue
            hospital = hosp.name.split("-")[0].strip()
            for patient_dir in self._subdirs(hosp):
                if (patient_dir / IMAGE_NAME).is_file():
                    yield hospital, patient_dir

    def scan_series(self) -> Iterator[SeriesCandidate]:
        for hospital, patient_dir in self._image_dirs():
            yield SeriesCandidate(
                path=patient_dir / IMAGE_NAME,
                patient_external_id=hospital + "/" + patient_dir.name,
                patient_name=patient_dir.name,
                study_key=hospital + "/" + patient_dir.name,
                study_description="chest CT",
                kind="nifti_file",
                hints={"hospital": hospital, "modality": "CT"},
            )

    def scan_segmentations(self) -> Iterator[SegmentationCandidate]:
        for hospital, patient_dir in self._image_dirs():
            pid = hospital + "/" + patient_dir.name

            # 1. the merged two-label lung mask, in a sibling *_lung_masks tree
            mask_dir = self.root / (hospital + "_lung_masks") / patient_dir.name
            if mask_dir.is_dir():
                for f in sorted(mask_dir.glob("*_merged_mask.nii.gz")):
                    yield SegmentationCandidate(
                        path=f, patient_external_id=pid, kind="nifti",
                        origin="ground_truth", display_name="双肺 mask",
                        label_set=self.cfg.label_set or "lung_lobes_v1",
                        association_rule="copd.sibling_lung_masks",
                    )

            # 2. per-structure binaries, merged into one multi-value labelmap
            components: dict[str, Path] = {}
            for sub in self._subdirs(patient_dir):
                if not SEG_DIR_RE.match(sub.name):
                    continue
                for f in sorted(sub.glob("*.nii.gz")):
                    components[f.name[: -len(".nii.gz")]] = f
            if components:
                names = sorted(components)
                yield SegmentationCandidate(
                    path=patient_dir,
                    patient_external_id=pid,
                    kind="nifti_binary_set",
                    origin="ground_truth",
                    display_name="TotalSegmentator",
                    label_set=None,          # placeholder set built from the names
                    association_rule="copd.binary_mask_dirs",
                    components={i + 1: components[n] for i, n in enumerate(names)},
                )
