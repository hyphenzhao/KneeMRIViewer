"""Cheap DICOM identification and header probing.

Two rules learned from this drive and encoded here:

1. **Never trust the file extension.** ``Bone Scan/`` keeps Interfile
   companions (``.A00``/``.I00``) plus ``.dat`` and ``.jpg`` in the very same
   UID-named folders as the ``.dcm`` slices. Only the ``DICM`` magic at byte
   offset 128 separates them, and it costs one 132-byte read.

2. **Never use a SOP-class allowlist to find images.** A series folder in
   ``Knee_MR_Anonymized_CHANGZHENG`` mixes a Philips Raw Data Storage object
   (``1.2.840.10008.5.1.4.1.1.66``) in among the slices. The universal
   invariant is that a real image instance carries ``Rows``, ``Columns`` and
   ``BitsAllocated``; that object carries none of them. A denylist on top
   catches things that *do* have Rows but are not primary images.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pydicom
from pydicom.tag import Tag

DICM_OFFSET = 128
DICM_MAGIC = b"DICM"

# Carries Rows/Columns but is not a primary image we want in an MPR volume.
NON_IMAGE_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.11.1",   # Grayscale Softcopy Presentation State
    "1.2.840.10008.5.1.4.1.1.11.2",
    "1.2.840.10008.5.1.4.1.1.11.3",
    "1.2.840.10008.5.1.4.1.1.11.4",
    "1.2.840.10008.5.1.4.1.1.88.11",  # Basic Text SR
    "1.2.840.10008.5.1.4.1.1.88.22",
    "1.2.840.10008.5.1.4.1.1.88.33",
    "1.2.840.10008.5.1.4.1.1.88.59",  # Key Object Selection
    "1.2.840.10008.5.1.4.1.1.66",     # Raw Data Storage (Philips private blob)
}
# Routed to the segmentation pipeline, not the image pipeline.
SEGMENTATION_SOP_CLASSES = {
    "1.2.840.10008.5.1.4.1.1.66.4",   # Segmentation Storage
    "1.2.840.10008.5.1.4.1.1.481.3",  # RT Structure Set
}

PROBE_TAGS = [
    Tag(0x0008, 0x0016), Tag(0x0008, 0x0018), Tag(0x0008, 0x0020), Tag(0x0008, 0x0030),
    Tag(0x0008, 0x0060), Tag(0x0008, 0x0070), Tag(0x0008, 0x1030), Tag(0x0008, 0x103E),
    Tag(0x0008, 0x1090), Tag(0x0008, 0x0050),
    Tag(0x0010, 0x0010), Tag(0x0010, 0x0020), Tag(0x0010, 0x0030), Tag(0x0010, 0x0040),
    Tag(0x0018, 0x0050), Tag(0x0018, 0x0088),
    Tag(0x0020, 0x000D), Tag(0x0020, 0x000E), Tag(0x0020, 0x0011), Tag(0x0020, 0x0013),
    Tag(0x0020, 0x0032), Tag(0x0020, 0x0037), Tag(0x0020, 0x0052), Tag(0x0020, 0x0060),
    Tag(0x0028, 0x0002), Tag(0x0028, 0x0004), Tag(0x0028, 0x0010), Tag(0x0028, 0x0011),
    Tag(0x0028, 0x0030), Tag(0x0028, 0x0100), Tag(0x0028, 0x0101), Tag(0x0028, 0x0102),
    Tag(0x0028, 0x0103), Tag(0x0028, 0x1050), Tag(0x0028, 0x1051),
    Tag(0x0028, 0x1052), Tag(0x0028, 0x1053),
]


def is_dicom_file(path: str | os.PathLike) -> bool:
    """True if the file has the DICM preamble magic. One 132-byte read."""
    try:
        with open(path, "rb") as fh:
            fh.seek(DICM_OFFSET)
            return fh.read(4) == DICM_MAGIC
    except OSError:
        return False


@dataclass
class InstanceHeader:
    path: Path
    sop_class_uid: str | None
    sop_instance_uid: str | None
    transfer_syntax_uid: str | None
    modality: str | None
    rows: int | None
    cols: int | None
    bits_allocated: int | None
    pixel_representation: int | None
    samples_per_pixel: int | None
    photometric: str | None
    pixel_spacing: tuple[float, float] | None
    slice_thickness: float | None
    spacing_between: float | None
    ipp: tuple[float, float, float] | None
    iop: tuple[float, ...] | None
    study_uid: str | None
    series_uid: str | None
    frame_of_reference_uid: str | None
    series_number: int | None
    instance_number: int | None
    series_description: str | None
    study_description: str | None
    study_date: str | None
    study_time: str | None
    accession: str | None
    patient_name: str | None
    patient_id: str | None
    patient_sex: str | None
    patient_age: str | None
    manufacturer: str | None
    model: str | None
    laterality: str | None
    window_center: float | None
    window_width: float | None
    rescale_slope: float
    rescale_intercept: float
    n_frames: int

    @property
    def is_image(self) -> bool:
        """A primary image instance carries Rows, Columns and BitsAllocated."""
        if self.rows is None or self.cols is None or self.bits_allocated is None:
            return False
        return self.sop_class_uid not in NON_IMAGE_SOP_CLASSES

    @property
    def is_segmentation(self) -> bool:
        return self.sop_class_uid in SEGMENTATION_SOP_CLASSES

    @property
    def numpy_dtype(self) -> str:
        if self.bits_allocated == 8:
            return "int8" if self.pixel_representation else "uint8"
        if self.pixel_representation:
            return "int16"
        return "uint16"


def _first(v: Any, cast=float):
    if v is None:
        return None
    try:
        if isinstance(v, (list, tuple)) or hasattr(v, "__len__") and not isinstance(v, (str, bytes)):
            return cast(v[0]) if len(v) else None
        return cast(v)
    except (TypeError, ValueError):
        return None


def _tuple(v: Any, n: int) -> tuple[float, ...] | None:
    if v is None:
        return None
    try:
        t = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        return None
    return t if len(t) == n else None


def probe(path: str | os.PathLike) -> InstanceHeader | None:
    """Read only the header tags we need. Never touches PixelData."""
    p = Path(path)
    try:
        ds = pydicom.dcmread(p, stop_before_pixels=True, specific_tags=PROBE_TAGS,
                             force=False)
    except Exception:
        return None
    g = ds.get
    ts = None
    try:
        ts = str(ds.file_meta.TransferSyntaxUID)
    except Exception:
        pass
    return InstanceHeader(
        path=p,
        sop_class_uid=_s(g("SOPClassUID")),
        sop_instance_uid=_s(g("SOPInstanceUID")),
        transfer_syntax_uid=ts,
        modality=_s(g("Modality")),
        rows=_first(g("Rows"), int),
        cols=_first(g("Columns"), int),
        bits_allocated=_first(g("BitsAllocated"), int),
        pixel_representation=_first(g("PixelRepresentation"), int),
        samples_per_pixel=_first(g("SamplesPerPixel"), int),
        photometric=_s(g("PhotometricInterpretation")),
        pixel_spacing=_tuple(g("PixelSpacing"), 2),
        slice_thickness=_first(g("SliceThickness")),
        spacing_between=_first(g("SpacingBetweenSlices")),
        ipp=_tuple(g("ImagePositionPatient"), 3),
        iop=_tuple(g("ImageOrientationPatient"), 6),
        study_uid=_s(g("StudyInstanceUID")),
        series_uid=_s(g("SeriesInstanceUID")),
        frame_of_reference_uid=_s(g("FrameOfReferenceUID")),
        series_number=_first(g("SeriesNumber"), int),
        instance_number=_first(g("InstanceNumber"), int),
        series_description=_s(g("SeriesDescription")),
        study_description=_s(g("StudyDescription")),
        study_date=_s(g("StudyDate")),
        study_time=_s(g("StudyTime")),
        accession=_s(g("AccessionNumber")),
        patient_name=_s(g("PatientName")),
        patient_id=_s(g("PatientID")),
        patient_sex=_s(g("PatientSex")),
        patient_age=_s(g("PatientAge")),
        manufacturer=_s(g("Manufacturer")),
        model=_s(g("ManufacturerModelName")),
        laterality=_s(g("Laterality")) or _s(g("ImageLaterality")),
        window_center=_first(g("WindowCenter")),
        window_width=_first(g("WindowWidth")),
        rescale_slope=_first(g("RescaleSlope")) or 1.0,
        rescale_intercept=_first(g("RescaleIntercept")) or 0.0,
        n_frames=_first(g("NumberOfFrames"), int) or 1,
    )


def _s(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def iter_dicom_files(directory: str | os.PathLike, *, limit: int | None = None) -> Iterator[Path]:
    """Yield files in one directory that carry the DICM magic."""
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        return
    n = 0
    for e in entries:
        if not e.is_file(follow_symlinks=False):
            continue
        # Skip macOS AppleDouble sidecars and similar junk before paying for a
        # read: this drive has one "._<name>" per DICOM.
        if e.name.startswith("._") or e.name.lower() in {".ds_store", "thumbs.db"}:
            continue
        if is_dicom_file(e.path):
            yield Path(e.path)
            n += 1
            if limit and n >= limit:
                return


def looks_like_series_dir(directory: str | os.PathLike) -> bool:
    """A candidate series directory holds at least one DICM-magic file."""
    for _ in iter_dicom_files(directory, limit=1):
        return True
    return False
