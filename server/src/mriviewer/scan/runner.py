"""Tiered, resumable scanning.

The drive holds roughly 170 000 DICOM files across five datasets on a USB/exFAT
volume. Reading every header would take hours, so the index is built in tiers:

* **tier 1 (probe)** - one ``scandir`` per series directory plus header reads of
  the *first, middle and last* file only. Enough to build the whole
  patient -> study -> series browse tree. ~3 reads per series.
* **tier 2 (materialize)** - lazy, on first open (or via a prefetch queue):
  read every file, order the slices, assemble the volume, cache it gzipped.

Both tiers are resumable: progress is keyed on ``(path, dir_mtime, entry_count,
total_size)`` in ``scan_dir``, so a re-run skips what has not changed and a
``kill -9`` loses at most one series.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from ..adapters import SeriesCandidate, make_adapter
from ..db.session import upsert
from ..dicomio.probe import InstanceHeader, is_dicom_file, probe
from ..dicomio.series import geometry_from_headers, sort_headers
from ..labels import ensure_labelset_for_values
from ..volume.build import build_volume, choose_downsample, downsample, wire_meta
from ..volume.cache import VolumeCache, hash_files, hash_inputs

ProgressFn = Callable[[str, int, int], None]


def _noop(_stage: str, _done: int, _total: int) -> None:
    pass


@dataclass
class DirStat:
    entry_count: int
    total_size: int
    mtime: float
    files: list[Path]


# Junk this drive is full of. macOS wrote an AppleDouble sidecar next to every
# single DICOM ("._000001.dcm", 4096 bytes) - they are hidden from `ls`, they
# sort *before* the real files because '.' < '0', and they exactly double every
# file count. Left in, they make a 19-slice series report 38 instances and send
# the first/middle/last sampler into the sidecars instead of the images.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini", ".spotlight-v100",
              ".trashes", ".fseventsd"}


def is_junk(name: str) -> bool:
    return name.startswith("._") or name.lower() in JUNK_NAMES


def stat_dir(path: Path) -> DirStat:
    files: list[Path] = []
    total = 0
    try:
        for e in os.scandir(path):
            if not e.is_file(follow_symlinks=False) or is_junk(e.name):
                continue
            files.append(Path(e.path))
            try:
                total += e.stat().st_size
            except OSError:
                pass
        mtime = path.stat().st_mtime
    except OSError:
        return DirStat(0, 0, 0.0, [])
    files.sort(key=lambda p: p.name)
    return DirStat(len(files), total, mtime, files)


def _sample_indices(n: int) -> list[int]:
    """First, middle and last - enough to detect orientation and uniformity."""
    if n <= 3:
        return list(range(n))
    return [0, n // 2, n - 1]


# ---------------------------------------------------------------- tier 1

def _probe_slice_spacing(ordered: list[InstanceHeader], geom) -> float:
    """Slice spacing from a *sample* of a series (first / middle / last).

    ``geometry_from_headers`` averages the gaps between the headers it is given.
    At tier 1 those are many slices apart, so it would report a 3 mm knee series
    as having 42 mm slices. Trust, in order:

    1. ``SpacingBetweenSlices`` - DICOM states it outright.
    2. The projected span divided by the *InstanceNumber* range, which counts
       the slices actually between the samples. Dividing by the file count
       instead is wrong: a folder can hold non-image objects (a Philips Raw
       Data blob) and, on this drive, a macOS AppleDouble per file.
    3. ``SliceThickness``.
    """
    ref = ordered[0]
    if ref.spacing_between and ref.spacing_between > 0:
        return float(ref.spacing_between)

    if len(ordered) > 1 and all(h.ipp is not None for h in ordered):
        normal = np.asarray(geom.normal, float)
        proj = [float(np.dot(np.asarray(h.ipp, float), normal)) for h in ordered]
        span = max(proj) - min(proj)
        nums = [h.instance_number for h in ordered if h.instance_number is not None]
        if span > 1e-6 and len(nums) == len(ordered):
            steps = max(nums) - min(nums)
            if steps > 0:
                return span / steps

    if ref.slice_thickness and ref.slice_thickness > 0:
        return float(ref.slice_thickness)
    return float(geom.spacing[2])


def probe_series(cand: SeriesCandidate) -> dict[str, Any] | None:
    """Header-probe one candidate. Returns a record, or None if it holds no images."""
    if cand.kind == "nifti_file":
        return _probe_nifti(cand)

    st = stat_dir(cand.path)
    if not st.files:
        return None

    # Find a real DICOM to probe: the first file may be a stray .txt or a
    # non-image object, so walk forward a little before giving up.
    headers: list[InstanceHeader] = []
    for idx in _sample_indices(len(st.files)):
        for off in range(0, min(5, len(st.files))):
            f = st.files[(idx + off) % len(st.files)]
            if not is_dicom_file(f):
                continue
            h = probe(f)
            if h and h.is_image:
                headers.append(h)
                break
    if not headers:
        return None

    ordered, warn = sort_headers(headers, complete=False)
    geom = geometry_from_headers(ordered)
    spacing = (geom.spacing[0], geom.spacing[1], _probe_slice_spacing(ordered, geom))
    # anisotropy was derived from the pre-correction spacing, so recompute it -
    # otherwise every 3 mm knee series reports 149:1 instead of 10.3:1.
    aniso = max(spacing) / min(spacing) if min(spacing) > 0 else 1.0
    geom = replace(geom, spacing=spacing, anisotropy=aniso)
    ref = ordered[0]
    return {
        "candidate": cand,
        "dir_stat": st,
        "ref": ref,
        "geom": geom,
        "warning": warn or geom.warning,
        # exact slice count needs tier 2; the file count is the upper bound
        "n_instances": len(st.files),
    }


def _probe_nifti(cand: SeriesCandidate) -> dict[str, Any] | None:
    import nibabel as nib
    try:
        img = nib.load(str(cand.path))
    except Exception:
        return None
    shape = tuple(int(x) for x in img.shape[:3])
    zooms = tuple(float(x) for x in img.header.get_zooms()[:3])
    return {
        "candidate": cand,
        "dir_stat": DirStat(1, cand.path.stat().st_size, cand.path.stat().st_mtime, [cand.path]),
        "ref": None,
        "geom": None,
        "nifti": {"shape": shape, "zooms": zooms,
                  "affine": [[float(x) for x in r] for r in img.affine],
                  "dtype": str(img.get_data_dtype())},
        "warning": None,
        "n_instances": shape[2] if len(shape) > 2 else 1,
    }


def scan_dataset(conn: sqlite3.Connection, cfg: Any, ds_cfg: Any, *,
                 limit: int | None = None, force: bool = False,
                 workers: int = 12, progress: ProgressFn = _noop,
                 job_id: int | None = None) -> dict[str, Any]:
    """Tier-1 scan of one dataset. Idempotent and resumable."""
    adapter = make_adapter(ds_cfg)
    ds_id = upsert(conn, "dataset", ["key"], {
        "key": ds_cfg.key, "name": ds_cfg.name, "root_path": str(ds_cfg.root),
        "adapter": ds_cfg.adapter, "viewer": ds_cfg.viewer,
        "delivery_profile": ds_cfg.delivery_profile,
    })
    conn.commit()

    cands = list(adapter.scan_series())
    if limit:
        cands = cands[:limit]
    total = len(cands)
    progress("enumerate", total, total)

    known = {
        r["path_rel"]: r
        for r in conn.execute(
            "SELECT path_rel, dir_mtime, entry_count, total_size, status"
            " FROM scan_dir WHERE dataset_id=?", (ds_id,)).fetchall()
    }

    stats = {"total": total, "indexed": 0, "skipped": 0, "empty": 0, "errors": 0}
    results: list[dict[str, Any]] = []

    def work(c: SeriesCandidate):
        rel = adapter.rel(c.path)
        if not force:
            prev = known.get(rel)
            if prev and prev["status"] == "done":
                if c.kind == "nifti_file":
                    return ("skip", c, None)
                st = stat_dir(c.path)
                if (st.entry_count == prev["entry_count"]
                        and st.total_size == prev["total_size"]):
                    return ("skip", c, None)
        try:
            rec = probe_series(c)
        except Exception as exc:  # noqa: BLE001
            return ("error", c, "%s: %s" % (type(exc).__name__, exc))
        return ("ok", c, rec)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(work, c) for c in cands]
        for fut in as_completed(futures):
            kind, cand, payload = fut.result()
            done += 1
            if done % 25 == 0 or done == total:
                progress("probe", done, total)
            if kind == "skip":
                stats["skipped"] += 1
            elif kind == "error":
                stats["errors"] += 1
                conn.execute(
                    "INSERT INTO scan_error(job_id,dataset_id,path_rel,kind,message)"
                    " VALUES(?,?,?,?,?)",
                    (job_id, ds_id, adapter.rel(cand.path), "probe", str(payload)))
            elif payload is None:
                stats["empty"] += 1
            else:
                results.append(payload)

    meta = adapter.patient_metadata()
    for rec in results:
        try:
            _persist_series(conn, adapter, ds_id, rec, meta)
            stats["indexed"] += 1
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            conn.execute(
                "INSERT INTO scan_error(job_id,dataset_id,path_rel,kind,message)"
                " VALUES(?,?,?,?,?)",
                (job_id, ds_id, adapter.rel(rec["candidate"].path), "persist",
                 "%s: %s" % (type(exc).__name__, exc)))
    conn.commit()

    # A full re-index must also forget what is no longer there. Without this,
    # changing how an adapter groups patients leaves the old rows behind and the
    # counts silently double - regrouping Bone Scan turned 27 886 series into
    # 55 772. Only safe for a complete scan: with --limit we did not look at
    # everything, and with an incremental run the skipped series are still real.
    if not limit and force:
        stats["pruned_series"], stats["pruned_patients"] = _prune_missing(
            conn, ds_id, {adapter.rel(c.path) for c in cands})
        conn.commit()

    stats["segmentations"] = ingest_segmentations(conn, cfg, ds_cfg, ds_id, job_id=job_id)
    stats["reports"] = _ingest_reports(conn, ds_cfg, ds_id)
    progress("done", total, total)
    return stats


def _prune_missing(conn: sqlite3.Connection, ds_id: int,
                   seen_paths: set[str]) -> tuple[int, int]:
    """Delete series the adapter no longer yields, then empty studies/patients.

    A dataset can hold tens of thousands of series, far past SQLite's limit on
    bound variables, so the surviving paths go into a temp table rather than an
    ``IN (?, ?, ...)`` list.
    """
    # Remove duplicates left by an older build that keyed series on study_id:
    # same dataset, same path, more than one row. Keep the most recent.
    conn.execute(
        "DELETE FROM series WHERE id IN ("
        "  SELECT s.id FROM series s JOIN study st ON st.id=s.study_id"
        "   JOIN patient p ON p.id=st.patient_id WHERE p.dataset_id=? AND s.id NOT IN ("
        "     SELECT MAX(s2.id) FROM series s2 JOIN study st2 ON st2.id=s2.study_id"
        "      JOIN patient p2 ON p2.id=st2.patient_id WHERE p2.dataset_id=?"
        "      GROUP BY s2.path_rel))", (ds_id, ds_id))

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen(path_rel TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _seen")
    conn.executemany("INSERT OR IGNORE INTO _seen(path_rel) VALUES(?)",
                     ((p,) for p in seen_paths))

    cur = conn.execute(
        "DELETE FROM series WHERE id IN ("
        "  SELECT s.id FROM series s JOIN study st ON st.id=s.study_id"
        "   JOIN patient p ON p.id=st.patient_id"
        "   WHERE p.dataset_id=? AND s.path_rel NOT IN (SELECT path_rel FROM _seen))",
        (ds_id,))
    pruned_series = int(cur.rowcount or 0)

    conn.execute(
        "DELETE FROM study WHERE patient_id IN (SELECT id FROM patient WHERE dataset_id=?)"
        " AND id NOT IN (SELECT DISTINCT study_id FROM series)", (ds_id,))
    cur = conn.execute(
        "DELETE FROM patient WHERE dataset_id=?"
        " AND id NOT IN (SELECT DISTINCT patient_id FROM study)", (ds_id,))
    pruned_patients = int(cur.rowcount or 0)

    conn.execute(
        "DELETE FROM scan_dir WHERE dataset_id=?"
        " AND path_rel NOT IN (SELECT path_rel FROM _seen)", (ds_id,))
    conn.execute("DROP TABLE IF EXISTS _seen")
    return pruned_series, pruned_patients


def _ingest_reports(conn: sqlite3.Connection, ds_cfg: Any, ds_id: int) -> int:
    """Load the dataset's de-identified report CSV, if it declares one."""
    if not getattr(ds_cfg, "reports_csv", None):
        return 0
    from ..reports import load_reports
    path = Path(ds_cfg.reports_csv)
    if not path.is_absolute():
        path = Path(ds_cfg.root) / path
    try:
        return load_reports(conn, ds_id, path)["rows"]
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "INSERT INTO scan_error(dataset_id,path_rel,kind,message) VALUES(?,?,?,?)",
            (ds_id, str(path), "reports", "%s: %s" % (type(exc).__name__, exc)))
        conn.commit()
        return 0


def _persist_series(conn: sqlite3.Connection, adapter, ds_id: int,
                    rec: dict[str, Any], meta: dict[str, dict[str, Any]]) -> int:
    cand: SeriesCandidate = rec["candidate"]
    info = meta.get(cand.patient_external_id, {})
    ref: InstanceHeader | None = rec["ref"]

    patient_id = upsert(conn, "patient", ["dataset_id", "external_id"], {
        "dataset_id": ds_id,
        "external_id": cand.patient_external_id,
        "name": info.get("name") or cand.patient_name or (ref.patient_name if ref else None),
        "sex": info.get("sex") or (ref.patient_sex if ref else None),
        "age": info.get("age") or (ref.patient_age if ref else None),
        "group_name": info.get("group_name"),
        "extra_json": json.dumps({**cand.patient_extra, **info}, ensure_ascii=False)
        if (cand.patient_extra or info) else None,
    })

    study_key = cand.study_key or cand.patient_external_id
    study_uid = ref.study_uid if ref else None
    existing = conn.execute(
        "SELECT id FROM study WHERE patient_id=? AND path_rel=?",
        (patient_id, study_key)).fetchone()
    if existing:
        study_id = int(existing["id"])
        conn.execute("UPDATE study SET study_uid=COALESCE(?, study_uid),"
                     " study_date=COALESCE(?, study_date),"
                     " description=COALESCE(?, description) WHERE id=?",
                     (study_uid, ref.study_date if ref else None,
                      cand.study_description, study_id))
    else:
        cur = conn.execute(
            "INSERT INTO study(patient_id,study_uid,study_date,study_time,description,"
            "accession,path_rel) VALUES(?,?,?,?,?,?,?)",
            (patient_id, study_uid, ref.study_date if ref else None,
             ref.study_time if ref else None, cand.study_description,
             ref.accession if ref else None, study_key))
        study_id = int(cur.lastrowid)

    path_rel = adapter.rel(cand.path)
    # Identify a series by (dataset, path), not (study, path). A directory is
    # one series no matter which patient the adapter currently groups it under,
    # so re-grouping must UPDATE the existing row. Keying on study_id instead
    # inserts a second copy and silently doubles the dataset.
    row = conn.execute(
        "SELECT s.id FROM series s JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id"
        " WHERE p.dataset_id=? AND s.path_rel=?", (ds_id, path_rel)).fetchone()

    if rec.get("nifti") is not None:
        n = rec["nifti"]
        shape, zooms = n["shape"], n["zooms"]
        values = {
            "study_id": study_id, "path_rel": path_rel,
            "series_uid": None, "modality": cand.hints.get("modality", "CT"),
            "description": cand.study_description or cand.path.name,
            "rows": shape[1], "cols": shape[0], "n_slices": shape[2],
            "n_instances": shape[2], "dtype": n["dtype"],
            "pixel_spacing_json": json.dumps([zooms[1], zooms[0]]),
            "slice_spacing": zooms[2],
            "acquisition_plane": "axial",
            "anisotropy_ratio": max(zooms) / min(zooms) if min(zooms) > 0 else 1.0,
            "geometry_warning": None, "is_viewable": 1,
        }
    else:
        geom = rec["geom"]
        assert ref is not None
        values = {
            "study_id": study_id, "path_rel": path_rel,
            "series_uid": ref.series_uid, "series_number": ref.series_number,
            "modality": ref.modality, "description": ref.series_description or cand.path.name,
            "sop_class_uid": ref.sop_class_uid, "transfer_syntax_uid": ref.transfer_syntax_uid,
            "manufacturer": ref.manufacturer, "model": ref.model,
            "rows": geom.dims[1], "cols": geom.dims[0],
            "n_slices": None, "n_instances": rec["n_instances"],
            "bits_allocated": ref.bits_allocated,
            "pixel_representation": ref.pixel_representation,
            "dtype": ref.numpy_dtype,
            "pixel_spacing_json": json.dumps([geom.spacing[1], geom.spacing[0]]),
            "slice_spacing": geom.spacing[2], "slice_thickness": ref.slice_thickness,
            "origin_json": json.dumps(list(geom.origin)),
            "direction_json": json.dumps(list(geom.direction)),
            "frame_of_reference_uid": ref.frame_of_reference_uid,
            "window_center": ref.window_center, "window_width": ref.window_width,
            "rescale_slope": ref.rescale_slope, "rescale_intercept": ref.rescale_intercept,
            "acquisition_plane": geom.plane, "anisotropy_ratio": geom.anisotropy,
            "geometry_warning": rec.get("warning"), "is_viewable": 1,
        }

    if row:
        series_id = int(row["id"])
        # values already carries study_id, so a re-grouped series moves rather
        # than duplicating.
        cols = ",".join(k + "=?" for k in values)
        conn.execute("UPDATE series SET " + cols + " WHERE id=?",
                     list(values.values()) + [series_id])
    else:
        cols = ",".join(values)
        ph = ",".join("?" for _ in values)
        cur = conn.execute("INSERT INTO series(" + cols + ") VALUES(" + ph + ")",
                           list(values.values()))
        series_id = int(cur.lastrowid)

    st: DirStat = rec["dir_stat"]
    upsert(conn, "scan_dir", ["dataset_id", "path_rel"], {
        "dataset_id": ds_id, "path_rel": path_rel, "dir_mtime": st.mtime,
        "entry_count": st.entry_count, "total_size": st.total_size, "status": "done",
    })
    return series_id


# ---------------------------------------------------------------- tier 2

class SourceUnreadable(Exception):
    """The series' source files cannot be decoded - corrupt, truncated, or not
    the format the index believed.

    Distinct from a bug in our code: retrying will not help, so the series is
    marked and skipped rather than re-read on every request. One such file does
    exist on the current drive (a 207 MB COPD ``lung image.nii.gz`` with a valid
    gzip header and a corrupt body), and before this existed it produced an
    opaque 500 on every hit and re-read the whole file each time.
    """


def materialize_series(conn: sqlite3.Connection, cfg: Any, series_id: int,
                       *, force: bool = False) -> dict[str, Any]:
    """Read every slice, assemble the volume, cache it. Idempotent."""
    row = conn.execute(
        "SELECT s.*, d.root_path, d.key AS ds_key, d.delivery_profile"
        " FROM series s JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id JOIN dataset d ON d.id=p.dataset_id"
        " WHERE s.id=?", (series_id,)).fetchone()
    if row is None:
        raise KeyError("series %d" % series_id)

    cache = VolumeCache(cfg.cache_dir)
    abs_path = Path(row["root_path"]) / row["path_rel"]

    try:
        if abs_path.is_file():                  # NIfTI-native series
            key, meta = _materialize_nifti(cfg, cache, row, abs_path, force=force)
        else:
            key, meta = _materialize_dicom(cfg, cache, row, abs_path, force=force)
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        # Bad bytes on disk, not a bug here. Record it so the UI can say so and
        # so we stop re-reading a large broken file on every request.
        message = "%s: %s" % (type(exc).__name__, exc)
        conn.execute(
            "UPDATE series SET volume_state='error', volume_error=? WHERE id=?",
            (message[:500], series_id))
        conn.commit()
        raise SourceUnreadable(message) from exc

    conn.execute(
        "UPDATE series SET volume_state='ready', volume_key=?, volume_bytes=?,"
        " n_slices=?, dtype=?, origin_json=?, direction_json=?,"
        " pixel_spacing_json=?, slice_spacing=?, volume_error=NULL WHERE id=?",
        (key, meta["byteLength"], meta["dimensions"][2], meta["numpyDtype"],
         json.dumps(meta["origin"]), json.dumps(meta["direction"]),
         json.dumps([meta["spacing"][1], meta["spacing"][0]]), meta["spacing"][2],
         series_id))
    conn.commit()
    return meta


def _materialize_dicom(cfg, cache: VolumeCache, row, abs_path: Path, *, force: bool):
    files = [p for p in sorted(abs_path.iterdir())
             if p.is_file() and not is_junk(p.name) and is_dicom_file(p)]
    if not files:
        raise ValueError("no DICOM files in %s" % abs_path)
    key = hash_inputs(row["ds_key"], row["path_rel"], hash_files(files))
    entry = cache.volume(key)
    if entry.exists and not force:
        return key, entry.read_meta()

    headers = [h for h in (probe(f) for f in files) if h and h.is_image]
    if not headers:
        raise ValueError("no image instances in %s" % abs_path)
    ordered, _ = sort_headers(headers)
    geom = geometry_from_headers(ordered)
    volume_id = "mriv:series:" + key
    entry, meta = build_volume(ordered, geom, cache, key, volume_id)
    _maybe_downsample(cfg, cache, key, volume_id, meta, entry)
    return key, meta


def _materialize_nifti(cfg, cache: VolumeCache, row, abs_path: Path, *, force: bool):
    import nibabel as nib
    from ..dicomio.series import SeriesGeometry

    st = abs_path.stat()
    key = hash_inputs(row["ds_key"], row["path_rel"], st.st_size, round(st.st_mtime, 3))
    entry = cache.volume(key)
    if entry.exists and not force:
        return key, entry.read_meta()

    img = nib.load(str(abs_path))
    arr = np.asanyarray(img.dataobj)
    if arr.ndim == 4 and arr.shape[3] == 1:
        arr = arr[..., 0]
    geom = geometry_from_nifti(img.affine, arr.shape)
    canonical = np.ascontiguousarray(arr.transpose(2, 0, 1))
    if canonical.dtype not in (np.uint8, np.int16, np.uint16, np.float32):
        canonical = canonical.astype(np.int16)

    from ..dicomio.probe import InstanceHeader
    ref = InstanceHeader(
        path=abs_path, sop_class_uid=None, sop_instance_uid=None,
        transfer_syntax_uid=None, modality=row["modality"] or "CT",
        rows=geom.dims[1], cols=geom.dims[0],
        bits_allocated=canonical.dtype.itemsize * 8,
        pixel_representation=0 if canonical.dtype.kind == "u" else 1,
        samples_per_pixel=1, photometric="MONOCHROME2",
        pixel_spacing=(geom.spacing[1], geom.spacing[0]),
        slice_thickness=geom.spacing[2], spacing_between=geom.spacing[2],
        ipp=geom.origin, iop=tuple(geom.row_cosine) + tuple(geom.col_cosine),
        study_uid=None, series_uid=None, frame_of_reference_uid="1.2.3.4",
        series_number=None, instance_number=None, series_description=row["description"],
        study_description=None, study_date=None, study_time=None, accession=None,
        patient_name=None, patient_id=None, patient_sex=None, patient_age=None,
        manufacturer=None, model=None, laterality=None,
        window_center=None, window_width=None,
        rescale_slope=1.0, rescale_intercept=0.0, n_frames=1,
    )
    from ..volume.build import window_from_percentiles
    wc, ww = window_from_percentiles(canonical)
    volume_id = "mriv:series:" + key
    meta = wire_meta(volume_id, geom, canonical, ref, wc, ww)
    entry.write(canonical, meta)
    meta = entry.read_meta()
    _maybe_downsample(cfg, cache, key, volume_id, meta, entry)
    return key, meta


def geometry_from_nifti(affine, shape):
    """Build a SeriesGeometry from a NIfTI affine (RAS -> LPS)."""
    from ..dicomio.series import SeriesGeometry, classify_plane
    a = np.asarray(affine, float)
    lps = a.copy()
    lps[0, :] *= -1.0
    lps[1, :] *= -1.0
    dirs, spacing = [], []
    for ax in range(3):
        v = lps[:3, ax]
        n = float(np.linalg.norm(v))
        spacing.append(n if n > 1e-9 else 1.0)
        dirs.append(v / n if n > 1e-9 else np.eye(3)[ax])
    # NIfTI axis order is (i, j, k) here; canonical wants (col, row, slice)
    row_cos, col_cos, normal = dirs[0], dirs[1], dirs[2]
    origin = tuple(float(x) for x in lps[:3, 3])
    dims = (int(shape[0]), int(shape[1]), int(shape[2]))
    sp = (float(spacing[0]), float(spacing[1]), float(spacing[2]))
    return SeriesGeometry(
        dims=dims, spacing=sp, origin=origin,  # type: ignore[arg-type]
        direction=tuple(float(x) for x in (*row_cos, *col_cos, *normal)),
        row_cosine=tuple(float(x) for x in row_cos),   # type: ignore[arg-type]
        col_cosine=tuple(float(x) for x in col_cos),   # type: ignore[arg-type]
        normal=tuple(float(x) for x in normal),        # type: ignore[arg-type]
        plane=classify_plane(normal),
        anisotropy=max(sp) / min(sp) if min(sp) > 0 else 1.0,
    )


def _maybe_downsample(cfg, cache: VolumeCache, key: str, volume_id: str,
                      meta: dict[str, Any], entry) -> None:
    """Create a reduced variant when the native volume blows the browser budget."""
    itemsize = np.dtype(meta["numpyDtype"]).itemsize
    dims = tuple(meta["dimensions"])
    if dims[0] * dims[1] * dims[2] * itemsize <= cfg.browser_volume_budget:
        return
    factors = choose_downsample(dims, itemsize, cfg.browser_volume_budget)  # (fk, fj, fi)
    if factors == (1, 1, 1):
        return
    arr = entry.read_array((dims[2], dims[1], dims[0]), meta["numpyDtype"])
    small = downsample(arr, factors)
    fk, fj, fi = factors
    variant = "ds%d-%d-%d" % (fi, fj, fk)
    sub = dict(meta)
    sub["variant"] = variant
    sub["dimensions"] = [small.shape[2], small.shape[1], small.shape[0]]
    sub["spacing"] = [meta["spacing"][0] * fi, meta["spacing"][1] * fj,
                      meta["spacing"][2] * fk]
    sub["metadata"] = dict(meta["metadata"])
    sub["metadata"]["Rows"] = small.shape[1]
    sub["metadata"]["Columns"] = small.shape[2]
    sub["metadata"]["PixelSpacing"] = [sub["spacing"][1], sub["spacing"][0]]
    cache.volume(key, variant).write(small, sub)
    # Record the variant on the native meta without rewriting the payload.
    meta.setdefault("variants", []).append(variant)
    meta["defaultVariant"] = variant
    entry.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                               encoding="utf-8")


# ------------------------------------------------------- segmentations

def ingest_segmentations(conn: sqlite3.Connection, cfg: Any, ds_cfg: Any,
                         ds_id: int, *, job_id: int | None = None) -> int:
    """Associate every segmentation the adapter finds with its series."""
    adapter = make_adapter(ds_cfg)
    n = 0
    for cand in adapter.scan_segmentations():
        try:
            series_row = conn.execute(
                "SELECT s.id FROM series s JOIN study st ON st.id=s.study_id"
                " JOIN patient p ON p.id=st.patient_id"
                " WHERE p.dataset_id=? AND p.external_id=?"
                " ORDER BY s.id LIMIT 1",
                (ds_id, cand.patient_external_id)).fetchone()
            if series_row is None:
                conn.execute(
                    "INSERT INTO scan_error(job_id,dataset_id,path_rel,kind,message)"
                    " VALUES(?,?,?,?,?)",
                    (job_id, ds_id, adapter.rel(cand.path), "segmentation",
                     "no series for patient %s" % cand.patient_external_id))
                continue
            ls_key = cand.label_set
            ls_id = None
            if ls_key:
                r = conn.execute("SELECT id FROM label_set WHERE key=?", (ls_key,)).fetchone()
                ls_id = int(r["id"]) if r else None
            upsert(conn, "segmentation", ["source_path_rel", "dataset_id"], {
                "series_id": int(series_row["id"]), "dataset_id": ds_id,
                "label_set_id": ls_id, "kind": cand.kind,
                "source_path_rel": adapter.rel(cand.path), "source_root": "dataset",
                "origin": cand.origin, "display_name": cand.display_name,
                "model_name": cand.model_name, "model_version": cand.model_version,
                "association_rule": cand.association_rule,
                "present_values_json": json.dumps(sorted(cand.components))
                if cand.components else None,
                "seg_state": "pending",
            })
            n += 1
        except Exception as exc:  # noqa: BLE001
            conn.execute(
                "INSERT INTO scan_error(job_id,dataset_id,path_rel,kind,message)"
                " VALUES(?,?,?,?,?)",
                (job_id, ds_id, str(cand.path), "segmentation",
                 "%s: %s" % (type(exc).__name__, exc)))
    conn.commit()
    return n
