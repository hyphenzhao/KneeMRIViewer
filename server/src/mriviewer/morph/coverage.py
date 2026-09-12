"""Bone coverage: which part of the articular bone surface has cartilage on it.

OARSI names: ``cAB`` is the bone area covered by cartilage, ``dAB`` the
denuded area inside the articular surface, ``tAB = cAB + dAB``. Without an
atlas there is no way to know how far the articular surface *should* extend,
so ``tAB`` is taken as the geodesic closing of the covered region: a bare
patch fully surrounded by cartilage counts, a rim that has receded does not.
That is why the number is called ``dABInteriorPct`` everywhere and is never
compared to CartiMorph's FCL.

A denuded patch is what an Outerbridge IV lesion looks like from the bone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import sparse
from scipy.ndimage import map_coordinates
from scipy.sparse.csgraph import connected_components, dijkstra

from .geometry import dense_surface, to_lps, vertex_areas
from .probe import TAU_BONE_MM


@dataclass
class DenudedPatch:
    area_mm2: float
    centroid_lps: list[float]
    cart_label: int            # the plate this patch belongs to
    code: str                  # subregion code by majority, "" if unknown
    n_slices: int
    # Fraction of the 2 mm ring around the patch that carries cartilage. A hole
    # in the plate is ~1.0; a bare strip where the cartilage label simply stops
    # short of the bone label is far lower. Only the former is a lesion.
    enclosure: float
    vertex_index: np.ndarray = field(repr=False)


@dataclass
class Coverage:
    bone: int
    cab_mm2: float
    tab_mm2: float
    dab_mm2: float
    denuded: list[DenudedPatch]

    @property
    def dab_interior_pct(self) -> float:
        return 100.0 * self.dab_mm2 / self.tab_mm2 if self.tab_mm2 > 0 else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "cABMm2": round(self.cab_mm2, 1), "tABMm2": round(self.tab_mm2, 1),
            "dABMm2": round(self.dab_mm2, 1), "dABInteriorPct": round(self.dab_interior_pct, 2),
            "denudedPatches": [
                {"areaMm2": round(p.area_mm2, 1), "cartLabel": p.cart_label,
                 "code": p.code, "nSlices": p.n_slices, "enclosure": round(p.enclosure, 2),
                 "centroidLps": [round(x, 1) for x in p.centroid_lps]}
                for p in self.denuded],
        }


def vertex_graph(verts: np.ndarray, faces: np.ndarray, scale_mm: float) -> sparse.csr_matrix:
    """Symmetric sparse adjacency weighted by edge length in mm.

    Edges are made unique first. Every interior edge belongs to two faces, and
    building the matrix straight from face edges makes ``coo_matrix`` *sum*
    the duplicates - doubling (or quadrupling) every weight, so a 6 mm
    geodesic radius silently became ~2 mm and the closing stopped closing.
    """
    i = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 2]])
    j = np.concatenate([faces[:, 1], faces[:, 2], faces[:, 0]])
    e = np.unique(np.sort(np.stack([i, j], axis=1), axis=1), axis=0)
    e = e[e[:, 0] != e[:, 1]]
    w = np.maximum(np.linalg.norm(verts[e[:, 0]] - verts[e[:, 1]], axis=1) * scale_mm, 1e-6)
    n = len(verts)
    return sparse.coo_matrix(
        (np.concatenate([w, w]), (np.concatenate([e[:, 0], e[:, 1]]), np.concatenate([e[:, 1], e[:, 0]]))),
        shape=(n, n)).tocsr()


def geodesic_dilate(graph: sparse.csr_matrix, seeds: np.ndarray, radius_mm: float) -> np.ndarray:
    """Vertices within ``radius_mm`` (along the surface) of any seed vertex."""
    idx = np.flatnonzero(seeds)
    if not len(idx):
        return np.zeros(graph.shape[0], bool)
    d = dijkstra(graph, directed=False, indices=idx, limit=radius_mm, min_only=True)
    return np.isfinite(d)


def geodesic_close(graph: sparse.csr_matrix, mask: np.ndarray, radius_mm: float) -> np.ndarray:
    """Dilate then erode: fills holes and bays narrower than ~2*radius."""
    dil = geodesic_dilate(graph, mask, radius_mm)
    # erosion = complement of the dilation of the complement
    ero = ~geodesic_dilate(graph, ~dil, radius_mm)
    return ero | mask


def bone_coverage(fields: dict[int, np.ndarray], grid, geom, frame, bone: int,
                  carts: list[int], *, closing_radius_mm: float,
                  min_denuded_area_mm2: float, min_denuded_slices: int = 2) -> Coverage | None:
    """Coverage of one bone by its cartilage plates, on the shared iso grid.

    The femur is done with both its plates at once: dilating one plate's
    covered region alone would march across the trochlea into the other.
    """
    from .parcellation import parcellate

    surf = dense_surface(fields[bone])
    if surf is None:
        return None
    verts, faces, _ = surf
    iso = float(grid.iso_mm)
    areas = vertex_areas(verts, faces) * iso * iso
    tau = TAU_BONE_MM / iso

    # Covered by any plate, and which plate is closest at each vertex.
    best = np.full(len(verts), -np.inf)
    owner = np.zeros(len(verts), int)
    for c in carts:
        if c not in fields:
            continue
        v = map_coordinates(fields[c], verts.T, order=1, mode="nearest")
        take = v > best
        best[take] = v[take]
        owner[take] = c
    covered = best > -tau
    if not covered.any():
        return Coverage(bone, 0.0, 0.0, 0.0, [])

    graph = vertex_graph(verts, faces, iso)
    tab = geodesic_close(graph, covered, closing_radius_mm)
    dab = tab & ~covered

    patches: list[DenudedPatch] = []
    if dab.any():
        sub_idx = np.flatnonzero(dab)
        sub = graph[sub_idx][:, sub_idx]
        n, labels = connected_components(sub, directed=False)
        slice_index = np.round(verts[:, 0] * grid.scale[0] + grid.lo[0]).astype(int)
        for k in range(n):
            members = sub_idx[labels == k]
            area = float(areas[members].sum())
            n_sl = int(len(np.unique(slice_index[members])))
            if area < min_denuded_area_mm2 or n_sl < min_denuded_slices:
                continue
            # Plate: the nearest cartilage at the patch. Since the patch is bare,
            # take the plate that owns the surrounding covered vertices.
            in_patch = np.zeros(len(verts), bool)
            in_patch[members] = True
            band = geodesic_dilate(graph, in_patch, 2.0) & ~in_patch
            enclosure = float(covered[band].sum() / band.sum()) if band.any() else 0.0
            ring = geodesic_dilate(graph, in_patch, closing_radius_mm) & covered
            cart = int(np.bincount(owner[ring]).argmax()) if ring.any() else int(carts[0])
            pts = to_lps(verts[members], grid, geom)
            code = ""
            try:
                parc = parcellate(cart, pts, slice_index[members], frame)
                codes = [c for c in parc.codes.tolist() if c]
                if codes:
                    code = max(set(codes), key=codes.count)
            except Exception:              # noqa: BLE001 - a patch without a code is still a patch
                code = ""
            patches.append(DenudedPatch(
                area_mm2=area, centroid_lps=pts.mean(0).tolist(), cart_label=cart,
                code=code, n_slices=n_sl, enclosure=enclosure, vertex_index=members))
        # Patches too small to count are not denuded area either.
        keep = np.zeros(len(verts), bool)
        for p in patches:
            keep[p.vertex_index] = True
        dab = keep
        tab = covered | dab

    return Coverage(
        bone=bone,
        cab_mm2=float(areas[covered].sum()),
        tab_mm2=float(areas[tab].sum()),
        dab_mm2=float(areas[dab].sum()),
        denuded=patches,
    )
