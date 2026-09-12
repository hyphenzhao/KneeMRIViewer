"""Regression tests for the things this drive actually does to a scanner.

Every case here is a bug that shipped and was caught by looking at real data,
not a hypothetical:

* macOS wrote an AppleDouble (``._000001.dcm``, 4096 bytes) beside every DICOM.
  They are hidden from ``ls``, they sort *before* the real files, and they
  exactly double the file count - which made a 19-slice series report 38
  instances and sent the first/middle/last sampler into the sidecars.
* A Philips Raw Data Storage object sits inside a normal image series and has
  no Rows/Columns at all.
* Bone Scan mixes DICOM with Interfile (``.A00``/``.I00``) and ``.dat``/``.jpg``
  in the same folders, so file names cannot decide what is an image.
* Tier 1 only reads three headers per series, so any spacing or uniformity
  conclusion drawn from adjacent-slice assumptions is wrong.

The cases that need real data read their paths from the environment and skip
when it is absent. They are never hard-coded: folder names on the source drive
carry patient name, sex and age, and this repository must not contain those.

    MRIV_TEST_SERIES_DIR   one DICOM series directory that also holds a
                           non-image object (Philips Raw Data Storage)
    MRIV_TEST_BONE_DIR     one Bone Scan patient directory, DICOM mixed with
                           Interfile .A00/.I00 companions

Run directly, or under pytest.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mriviewer.adapters.base import SeriesCandidate                    # noqa: E402
from mriviewer.dicomio.probe import (                                   # noqa: E402
    NON_IMAGE_SOP_CLASSES, is_dicom_file, iter_dicom_files, looks_like_series_dir, probe,
)
from mriviewer.scan.runner import is_junk, probe_series, stat_dir       # noqa: E402

DEVDATA = os.environ.get(
    "MRIVIEWER_TESTDATA",
    str(Path(__file__).resolve().parents[3] / "devdata" / "0826"),
)


def _real_series() -> Path | None:
    import glob
    hits = glob.glob(os.path.join(DEVDATA, "dicom", "1", "*"))
    return Path(hits[0]) if hits else None



def _env_dir(name: str) -> Path | None:
    """A real-data directory named by the environment, or None to skip.

    Deliberately not a default path: the directory names on the source drive
    include patient identifiers, so they live in the operator's environment and
    never in version control.
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def test_junk_names() -> None:
    assert is_junk("._000001.dcm")
    assert is_junk(".DS_Store")
    assert is_junk("Thumbs.db")
    assert not is_junk("000001.dcm")
    assert not is_junk("LUOGUIYINGM59Y15_231400.I00")     # non-DICOM, but not junk
    print("  junk filter: AppleDouble and friends rejected, real names kept")


def test_appledouble_sidecars_ignored() -> None:
    """A sidecar per DICOM must not change the file count or the geometry."""
    src = _real_series()
    if src is None:
        print("  sidecars: no dev data - skipped")
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "series"
        shutil.copytree(src, d)
        clean = stat_dir(d)
        clean_rec = probe_series(SeriesCandidate(path=d, patient_external_id="x"))

        # Now do to it what macOS did to the drive.
        for f in sorted(d.iterdir()):
            if f.is_file() and not f.name.startswith("._"):
                (d / ("._" + f.name)).write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 4092)
        assert len(list(d.iterdir())) == 2 * clean.entry_count

        dirty = stat_dir(d)
        dirty_rec = probe_series(SeriesCandidate(path=d, patient_external_id="x"))

        assert dirty.entry_count == clean.entry_count, (
            "%d files with sidecars vs %d without" % (dirty.entry_count, clean.entry_count))
        assert dirty_rec["n_instances"] == clean_rec["n_instances"]
        assert dirty_rec["geom"].spacing == clean_rec["geom"].spacing
        assert dirty_rec["geom"].dims == clean_rec["geom"].dims
        print("  sidecars: %d added, counts and geometry unchanged (%d files, %.3f mm)"
              % (clean.entry_count, dirty.entry_count, dirty_rec["geom"].spacing[2]))


def test_tier1_spacing_matches_truth() -> None:
    """Tier 1 reads 3 headers; its spacing must still match a full read."""
    import numpy as np
    src = _real_series()
    if src is None:
        print("  tier-1 spacing: no dev data - skipped")
        return
    rec = probe_series(SeriesCandidate(path=src, patient_external_id="x"))
    tier1 = rec["geom"].spacing[2]

    headers = [h for h in (probe(f) for f in iter_dicom_files(src)) if h and h.is_image]
    iop = np.asarray(headers[0].iop, float)
    normal = np.cross(iop[:3], iop[3:])
    proj = sorted(float(np.dot(np.asarray(h.ipp, float), normal)) for h in headers)
    truth = float(np.mean(np.diff(proj)))

    assert abs(tier1 - truth) < 1e-3, "tier1 %.4f vs truth %.4f" % (tier1, truth)
    assert rec["warning"] is None, (
        "tier-1 sampling must not claim non-uniform spacing: %r" % rec["warning"])
    print("  tier-1 spacing: %.3f mm == full-read %.3f mm, no spurious warning"
          % (tier1, truth))


def test_non_image_objects_rejected() -> None:
    """Rows/Columns/BitsAllocated is the image test - not a SOP-class allowlist."""
    series_dir = _env_dir("MRIV_TEST_SERIES_DIR")
    if series_dir is None:
        print("  non-image filter: MRIV_TEST_SERIES_DIR not set - skipped")
        return
    headers = [h for h in (probe(f) for f in iter_dicom_files(series_dir)) if h]
    images = [h for h in headers if h.is_image]
    rejected = [h for h in headers if not h.is_image]
    assert rejected, "expected the Philips Raw Data object to be present"
    for h in rejected:
        assert h.rows is None or h.sop_class_uid in NON_IMAGE_SOP_CLASSES
    print("  non-image filter: %d files -> %d images, rejected %s"
          % (len(headers), len(images), [h.path.name for h in rejected]))


def test_magic_sniff_rejects_non_dicom() -> None:
    """Names cannot decide what is DICOM; the DICM magic can.

    Bone Scan keeps Interfile ``.A00``/``.I00`` companions (and ``.dat``,
    ``.jpg``) next to the ``.dcm`` slices, inside UID-named folders. Sniffing
    byte 128 accepts exactly the DICOM and rejects the rest.
    """
    bone = _env_dir("MRIV_TEST_BONE_DIR")
    if bone is None:
        print("  magic sniff: MRIV_TEST_BONE_DIR not set - skipped")
        return

    by_ext: dict[str, list[int]] = {}
    for f in bone.rglob("*"):
        if not f.is_file():
            continue
        row = by_ext.setdefault(f.suffix.lower() or "(none)", [0, 0])
        row[0] += 1
        if is_dicom_file(f):
            row[1] += 1

    assert by_ext.get(".dcm"), "expected .dcm slices"
    n, dicom = by_ext[".dcm"]
    assert dicom == n, "%d of %d .dcm files failed the DICM sniff" % (n - dicom, n)
    others = {e: v for e, v in by_ext.items() if e != ".dcm"}
    assert others, "expected non-DICOM companions in this tree"
    for ext, (count, dicom_count) in others.items():
        assert dicom_count == 0, "%s should not sniff as DICOM (%d did)" % (ext, dicom_count)

    found = [Path(r) for r, _d, _f in os.walk(bone) if looks_like_series_dir(r)]
    assert found, "no series found by DICM magic"
    print("  magic sniff: %d/%d .dcm accepted, %s rejected, %d series located"
          % (dicom, n, ", ".join("%s x%d" % (e, v[0]) for e, v in sorted(others.items())),
             len(found)))


if __name__ == "__main__":
    print("scanner regressions")
    test_junk_names()
    test_appledouble_sidecars_ignored()
    test_tier1_spacing_matches_truth()
    test_non_image_objects_rejected()
    test_magic_sniff_rejects_non_dicom()
    print("ALL PASS")
