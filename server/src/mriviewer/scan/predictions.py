"""Model outputs dropped into ``predictions_roots``.

The platform does not run models. A model runs wherever it likes - its own
virtualenv, a GPU box, another building - writes a labelmap NIfTI on the
series' own grid, and drops it here:

    <root>/<model>/<version>/<series_uid>/<labelset>.nii.gz

This module is the missing half of that promise: it walks the roots and
registers each file as a segmentation of the series whose SeriesInstanceUID
names the directory. Everything downstream already treats a model's labelmap
exactly like ground truth - the grid check in ``seg.ingest.derive_transform``,
the cache, the meshes, the viewer, the report - except that ``origin`` says
where it came from and ``model_name`` / ``model_version`` say which model.

Two deliberate choices:

* **Matching is by SeriesInstanceUID and nothing else.** Dataset ground truth
  is associated by "the patient's first series", which is fine for a folder of
  one-series knees and wrong the moment a study holds a coronal and a sagittal
  acquisition - exactly the case a model is most likely to be run on. An
  unmatched UID is recorded as a scan error, never guessed at.
* **The label set must already exist.** A typo in the file name would
  otherwise auto-create a label set of grey "Label 9" entries that looks like
  a working import. Better to refuse and say which key was missing.

Nothing here imports a machine-learning framework, and nothing here runs one.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..db.session import upsert

SUFFIXES = (".nii.gz", ".nii")


@dataclass
class PredictionFile:
    path: Path
    model: str
    version: str
    series_uid: str
    label_set: str

    @property
    def display_name(self) -> str:
        return "%s %s" % (self.model, self.version)


def _stem(name: str) -> str | None:
    for suffix in SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


def iter_prediction_files(roots: list[Path]) -> Iterator[PredictionFile]:
    """Walk the documented four-level layout. Anything else is ignored."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for model_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for version_dir in sorted(p for p in model_dir.iterdir() if p.is_dir()):
                for series_dir in sorted(p for p in version_dir.iterdir() if p.is_dir()):
                    for f in sorted(series_dir.iterdir()):
                        stem = _stem(f.name) if f.is_file() else None
                        if stem:
                            yield PredictionFile(
                                path=f.resolve(), model=model_dir.name,
                                version=version_dir.name, series_uid=series_dir.name,
                                label_set=stem)


def ingest_predictions(conn: sqlite3.Connection, cfg: Any, *,
                       job_id: int | None = None) -> dict[str, int]:
    """Register every prediction file as a segmentation of its series.

    Idempotent: the natural key is the file's absolute path, so re-running
    after dropping new files only adds the new ones, and re-running after a
    model rewrote a file leaves the row in place for ``materialize_segmentation``
    to notice (the cache key hashes the file size).

    Returns counts, so a caller can tell "nothing to do" from "nothing worked".
    """
    stats = {"found": 0, "ingested": 0, "unmatched": 0, "no_label_set": 0}
    roots = list(getattr(cfg, "predictions_roots", []) or [])
    if not roots:
        return stats

    def fail(path: Path, ds_id: int | None, message: str) -> None:
        conn.execute(
            "INSERT INTO scan_error(job_id,dataset_id,path_rel,kind,message)"
            " VALUES(?,?,?,?,?)", (job_id, ds_id, str(path), "prediction", message))

    for pred in iter_prediction_files(roots):
        stats["found"] += 1
        try:
            series = conn.execute(
                "SELECT s.id, p.dataset_id FROM series s"
                " JOIN study st ON st.id=s.study_id"
                " JOIN patient p ON p.id=st.patient_id"
                " WHERE s.series_uid=? ORDER BY s.id LIMIT 1",
                (pred.series_uid,)).fetchone()
            if series is None:
                stats["unmatched"] += 1
                fail(pred.path, None, "no series with SeriesInstanceUID %s" % pred.series_uid)
                continue
            ls = conn.execute("SELECT id FROM label_set WHERE key=?",
                              (pred.label_set,)).fetchone()
            if ls is None:
                stats["no_label_set"] += 1
                fail(pred.path, int(series["dataset_id"]),
                     "no label set named %r (the file name is the label set key)"
                     % pred.label_set)
                continue

            upsert(conn, "segmentation", ["source_path_rel", "dataset_id"], {
                "series_id": int(series["id"]),
                "dataset_id": int(series["dataset_id"]),
                "label_set_id": int(ls["id"]),
                "kind": "nifti",
                # Absolute: predictions live outside every dataset root, and
                # seg.ingest resolves an absolute source_path_rel as-is.
                "source_path_rel": str(pred.path),
                "source_root": "predictions",
                "origin": "model",
                "display_name": pred.display_name,
                "model_name": pred.model,
                "model_version": pred.version,
                "association_rule": "predictions.series_uid",
                "seg_state": "pending",
            })
            stats["ingested"] += 1
        except Exception as exc:                  # noqa: BLE001 - one bad file, not the run
            fail(pred.path, None, "%s: %s" % (type(exc).__name__, exc))
    conn.commit()
    return stats


def model_segmentations(conn: sqlite3.Connection, series_id: int) -> list[sqlite3.Row]:
    """Model-produced segmentations of one series, newest first.

    The report reads its extra structures through this: the cartilage
    segmentation stays the document's subject, and whatever a model added to
    the *same series* rides alongside it.
    """
    return conn.execute(
        "SELECT sg.*, ls.key AS label_set_key FROM segmentation sg"
        " LEFT JOIN label_set ls ON ls.id=sg.label_set_id"
        " WHERE sg.series_id=? AND sg.origin='model' AND sg.is_active=1"
        " ORDER BY sg.id DESC", (series_id,)).fetchall()
