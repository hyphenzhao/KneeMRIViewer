"""Stage-0 probe: how much of this data can actually carry a thickness number?

Read-only. Computes, per cartilage plate, the effective sampling resolution
along the bone-cartilage interface normal and reports its distribution. Run it
before building the rest of the pipeline: it decides how fine the subregion
parcellation can honestly go, using measurements rather than assumptions.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

from .frame import KneeFrame, knee_frame, native_spacing_along
from .geometry import dense_surface, iso_fields, normals_to_lps, vertex_areas

# A vertex is on the interior bone-cartilage interface when the bone level set
# says it is within this distance of bone. 1.5 isotropic voxels.
TAU_BONE_MM = 0.75
# Below this effective resolution the thickness measurement is trustworthy.
RELIABLE_RES_MM = 0.6

# cartilage label -> (parent bone label, human name)
PLATES: dict[int, tuple[int, str]] = {
    4: (1, "股骨内侧软骨 MFC"),
    5: (1, "股骨外侧软骨 LFC"),
    6: (2, "胫骨内侧软骨 MTC"),
    7: (2, "胫骨外侧软骨 LTC"),
    8: (3, "髌骨软骨 PC"),
}


@dataclass
class PlateProbe:
    label: int
    name: str
    n_slices: int                 # native sagittal slices the plate spans
    n_vertices: int
    interface_area_mm2: float
    res_p10: float
    res_p50: float
    res_p90: float
    in_plane_area_fraction: float   # area fraction with res_eff <= RELIABLE_RES_MM
    mean_normal_ml_abs: float       # |n . e_ML| area-weighted; 0 = ideal, 1 = hopeless

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def probe_segmentation(canonical: np.ndarray, geom,
                       header_laterality: str | None = None
                       ) -> tuple[KneeFrame, list[PlateProbe]]:
    """Measure interface resolution for every cartilage plate present."""
    masks = {int(v): (canonical == v) for v in np.unique(canonical) if v != 0}
    frame = knee_frame(masks, geom, header_laterality)

    out: list[PlateProbe] = []
    for cart, (bone, name) in PLATES.items():
        if cart not in masks or bone not in masks:
            continue

        fields, grid = iso_fields(masks, geom, [cart, bone])
        surf = dense_surface(fields[cart])
        if surf is None:
            continue
        verts, faces, normals_kji = surf

        # Sample the bone level set at the cartilage surface vertices. Positive
        # means inside bone, so >= -TAU means "at the bone interface".
        phi_bone = fields[bone]
        d_bone = _sample(phi_bone, verts)
        tau_iso = TAU_BONE_MM / grid.iso_mm
        on_bone = d_bone > -tau_iso

        if not on_bone.any():
            continue

        areas = vertex_areas(verts, faces) * (grid.iso_mm ** 2)
        normals_lps = normals_to_lps(normals_kji, geom)
        res = native_spacing_along(frame, geom, normals_lps)

        w = areas[on_bone]
        r = res[on_bone]
        order = np.argsort(r)
        r_sorted, w_sorted = r[order], w[order]
        cw = np.cumsum(w_sorted) / w_sorted.sum()

        def wq(q: float) -> float:
            return float(r_sorted[np.searchsorted(cw, q, side="left").clip(0, len(r_sorted) - 1)])

        e_ML = np.asarray(frame.e_ML, float)
        ml_abs = float((np.abs(normals_lps[on_bone] @ e_ML) * w).sum() / w.sum())

        idx = np.argwhere(masks[cart])
        out.append(PlateProbe(
            label=cart, name=name,
            n_slices=int(idx[:, 0].max() - idx[:, 0].min() + 1),
            n_vertices=int(on_bone.sum()),
            interface_area_mm2=round(float(w.sum()), 1),
            res_p10=round(wq(0.10), 3), res_p50=round(wq(0.50), 3),
            res_p90=round(wq(0.90), 3),
            in_plane_area_fraction=round(float(w[r <= RELIABLE_RES_MM].sum() / w.sum()), 3),
            mean_normal_ml_abs=round(ml_abs, 3),
        ))
    return frame, out


def _sample(field: np.ndarray, verts_kji: np.ndarray) -> np.ndarray:
    """Trilinear sample of a (k, j, i) field at float voxel coordinates."""
    from scipy.ndimage import map_coordinates
    return map_coordinates(field, verts_kji.T, order=1, mode="nearest")


def probe_dataset(conn: sqlite3.Connection, cfg: Any, dataset_key: str,
                  limit: int | None = None) -> list[dict[str, Any]]:
    """Probe every ready segmentation in a dataset."""
    from ..seg.ingest import series_geometry
    from ..volume.cache import VolumeCache

    rows = conn.execute(
        "SELECT sg.id, sg.seg_key, sg.series_id, p.external_id, p.name, p.sex"
        " FROM segmentation sg JOIN dataset d ON d.id=sg.dataset_id"
        " JOIN series s ON s.id=sg.series_id JOIN study st ON st.id=s.study_id"
        " JOIN patient p ON p.id=st.patient_id"
        " WHERE d.key=? AND sg.seg_state='ready' ORDER BY CAST(p.external_id AS INTEGER)",
        (dataset_key,)).fetchall()
    if limit:
        rows = rows[:limit]

    cache = VolumeCache(cfg.cache_dir)
    results: list[dict[str, Any]] = []
    for r in rows:
        entry = cache.segmentation(r["seg_key"])
        if not entry.exists:
            continue
        meta = entry.read_meta()
        d = meta["dimensions"]
        canonical = entry.read_array((d[2], d[1], d[0]), "uint8")
        geom, _ = series_geometry(conn, int(r["series_id"]))

        lat = conn.execute(
            "SELECT laterality FROM patient p JOIN study st ON st.patient_id=p.id"
            " JOIN series s ON s.study_id=st.id WHERE s.id=?", (r["series_id"],)
        ).fetchone()
        frame, plates = probe_segmentation(
            canonical, geom, lat["laterality"] if lat else None)

        results.append({
            "segId": int(r["id"]), "case": r["external_id"], "patient": r["name"],
            "sex": r["sex"], "frame": frame.to_json(),
            "plates": [p.to_json() for p in plates],
        })
    return results
