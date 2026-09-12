"""Shared isotropic grids, dense surfaces, and per-vertex area weights.

Two things here matter more than they look:

* **Bone and cartilage must land on the same grid.** ``seg/mesh.py`` crops each
  label to its own bounding box, which is right for display and wrong for
  measurement: two independently cropped grids do not share an origin, so the
  bone-cartilage interface cannot be found by sampling one field at the other's
  vertices. ``iso_fields`` crops the pair together.
* **Measure on an undecimated surface.** ``build_label_mesh`` decimates to 30k
  triangles for the browser. Decimation moves vertices, and every thickness and
  area number here is a per-vertex quantity. Decimate for display only.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..seg.mesh import _have_vtk, _level_set

# Cap the shared grid: a whole femur plus its cartilage at 0.5 mm is ~3e7 voxels.
MAX_ISO_VOXELS = 12_000_000
DEFAULT_ISO_MM = 0.5
CROP_MARGIN_VOX = 6


@dataclass
class IsoGrid:
    """A crop window in native voxels plus the isotropic grid it maps onto."""
    lo: np.ndarray                 # (k, j, i) native index of the crop origin
    hi: np.ndarray
    iso_mm: float
    spacing_kji: tuple[float, float, float]   # native spacing in (k, j, i) order

    @property
    def scale(self) -> np.ndarray:
        """Isotropic voxel units -> native voxel units, per axis."""
        return np.array([self.iso_mm / self.spacing_kji[0],
                         self.iso_mm / self.spacing_kji[1],
                         self.iso_mm / self.spacing_kji[2]])


def iso_fields(masks: dict[int, np.ndarray], geom, labels: list[int],
               iso_mm: float = DEFAULT_ISO_MM,
               margin: int = CROP_MARGIN_VOX) -> tuple[dict[int, np.ndarray], IsoGrid]:
    """Level sets for several labels on one shared isotropic grid.

    Returns ``{label: phi}`` where ``phi > 0`` inside the structure, plus the
    grid that relates those arrays back to patient space.
    """
    present = [l for l in labels if l in masks and masks[l].any()]
    if not present:
        raise ValueError("none of the requested labels are present")

    shape = masks[present[0]].shape
    lo = np.array(shape)
    hi = np.zeros(3, int)
    for l in present:
        idx = np.argwhere(masks[l])
        lo = np.minimum(lo, idx.min(0))
        hi = np.maximum(hi, idx.max(0) + 1)
    lo = np.maximum(lo - margin, 0)
    hi = np.minimum(hi + margin, shape)

    col_sp, row_sp, sl_sp = geom.spacing
    spacing_kji = (sl_sp, row_sp, col_sp)

    extent_mm = [(hi[d] - lo[d]) * spacing_kji[d] for d in range(3)]
    iso_mm = max(iso_mm, float(np.prod(extent_mm) / MAX_ISO_VOXELS) ** (1.0 / 3.0))

    fields: dict[int, np.ndarray] = {}
    for l in present:
        sub = np.ascontiguousarray(masks[l][lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]])
        fields[l], _ = _level_set(sub, spacing_kji, iso_mm)
    return fields, IsoGrid(lo=lo, hi=hi, iso_mm=iso_mm, spacing_kji=spacing_kji)


def dense_surface(field: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """Undecimated isosurface of a level set.

    Returns ``(vertices_kji, triangles, normals_kji)`` in isotropic voxel units.
    Normals are outward-facing, area-weighted and consistently oriented.
    """
    if not _have_vtk():
        raise RuntimeError("VTK is required for morphometry (mrictl doctor)")
    import vtk
    from vtk.util import numpy_support

    k, j, i = field.shape
    img = vtk.vtkImageData()
    img.SetDimensions(i, j, k)
    img.SetSpacing(1.0, 1.0, 1.0)
    arr = numpy_support.numpy_to_vtk(field.ravel(order="C"), deep=True,
                                     array_type=vtk.VTK_FLOAT)
    arr.SetName("phi")
    img.GetPointData().SetScalars(arr)

    fe = vtk.vtkFlyingEdges3D()
    fe.SetInputData(img)
    fe.SetValue(0, 0.0)
    fe.ComputeNormalsOff()
    fe.ComputeGradientsOff()

    # No decimation: every number downstream is per-vertex.
    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(fe.GetOutputPort())

    nrm = vtk.vtkPolyDataNormals()
    nrm.SetInputConnection(tri.GetOutputPort())
    nrm.SplittingOff()              # splitting would duplicate vertices
    nrm.ConsistencyOn()
    nrm.AutoOrientNormalsOn()
    nrm.ComputePointNormalsOn()
    nrm.ComputeCellNormalsOff()
    nrm.Update()

    poly = nrm.GetOutput()
    if poly.GetNumberOfPoints() == 0:
        return None

    pts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData())     # (n,3) as (x=i,y=j,z=k)
    faces = numpy_support.vtk_to_numpy(poly.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
    nor = numpy_support.vtk_to_numpy(poly.GetPointData().GetNormals())

    # VTK works in (x, y, z) = (i, j, k); everything else here is (k, j, i).
    verts_kji = np.stack([pts[:, 2], pts[:, 1], pts[:, 0]], axis=1)
    normals_kji = np.stack([nor[:, 2], nor[:, 1], nor[:, 0]], axis=1)
    return verts_kji, faces.astype(np.int64), normals_kji


def vertex_areas(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Barycentric area weight per vertex: a third of each incident triangle.

    Aggregation must be area-weighted, not a plain mean over vertices: marching
    cubes puts far more vertices per mm^2 on a curved rim than on a flat
    plateau, so a vertex mean silently over-weights exactly the regions this
    data resolves worst.
    """
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    tri_area = 0.5 * np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    out = np.zeros(len(verts))
    np.add.at(out, faces[:, 0], tri_area / 3.0)
    np.add.at(out, faces[:, 1], tri_area / 3.0)
    np.add.at(out, faces[:, 2], tri_area / 3.0)
    return out


def to_lps(verts_kji_iso: np.ndarray, grid: IsoGrid, geom) -> np.ndarray:
    """Isotropic-grid vertices -> patient LPS mm."""
    from ..seg.mesh import _index_to_lps
    return _index_to_lps(verts_kji_iso * grid.scale, geom, grid.lo.astype(float))


def normals_to_lps(normals_kji: np.ndarray, geom) -> np.ndarray:
    """Unit normals from (k, j, i) voxel space into LPS.

    The isotropic grid is isotropic, so no metric correction is needed - only
    the change of basis into the patient frame.
    """
    basis = np.array([np.asarray(geom.normal, float),      # k
                      np.asarray(geom.col_cosine, float),  # j
                      np.asarray(geom.row_cosine, float)]) # i
    out = normals_kji @ basis
    n = np.linalg.norm(out, axis=1, keepdims=True)
    return np.divide(out, n, out=np.zeros_like(out), where=n > 1e-9)
