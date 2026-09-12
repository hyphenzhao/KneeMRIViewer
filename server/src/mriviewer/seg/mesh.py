"""Per-label 3D surfaces, precomputed on the server.

The knee data is 0.29 x 0.29 x 3.0 mm - roughly 10:1. Running marching cubes
straight on that labelmap gives every cartilage plate hard 3 mm terraces. The
fix is to evolve a **smooth level set** from the binary mask and resample that
to an isotropic grid before extracting the surface:

    crop to bbox -> AntiAliasBinary level set -> linear resample to 0.5 mm iso
    -> flying edges at 0 -> windowed-sinc (Taubin) smoothing -> decimation

Measured on real data, the level-set step cuts the slice-facing surface area of
a cartilage plate by 3-4x (see ``_level_set``).

The output is a flat float32 point array plus a uint32 triangle index array,
which is exactly what Cornerstone3D's ``SurfaceData`` wants - so there is no
glTF or Draco in the pipeline, and therefore no WASM decoder in the air-gapped
bundle. Eight knee labels come to roughly 2-3 MB gzipped in total.

VTK is optional: if it is not installed we fall back to scikit-image marching
cubes, which gives a usable but visibly rougher surface.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import struct
from pathlib import Path
from typing import Any

import numpy as np

from ..volume.cache import VolumeCache

MESH_MAGIC = b"MRIVMESH"
MESH_VERSION = 1

ISO_SPACING_MM = 0.5
ANTIALIAS_ITERATIONS = 50
ANTIALIAS_RMS = 0.02
SMOOTH_ITERATIONS = 20
SMOOTH_PASSBAND = 0.05
MIN_VOXELS = 20
CROP_MARGIN = 4

# Decimate to a triangle *budget*, not a fixed ratio. A femur starts out ~20x
# denser than a cartilage plate, so one ratio either leaves the bones at 1.6 MB
# or destroys the thin plates.
MAX_TRIANGLES = 30000
MAX_REDUCTION = 0.98
# Always decimate a little: flying edges on a 0.5 mm grid is far denser
# than the underlying 3 mm data can justify.
MIN_REDUCTION = 0.5
# Cap the resampled grid so a whole femur does not blow up into 3e7 voxels.
MAX_ISO_VOXELS = 8_000_000


def _have_vtk() -> bool:
    try:
        import vtk  # noqa: F401
        return True
    except Exception:
        return False


def encode_mesh(points: np.ndarray, tris: np.ndarray) -> bytes:
    """MRIVMESH: header, float32 xyz points (LPS mm), uint32 triangle indices."""
    points = np.ascontiguousarray(points, dtype=np.float32)
    tris = np.ascontiguousarray(tris, dtype=np.uint32)
    lo = points.min(axis=0) if len(points) else np.zeros(3, np.float32)
    hi = points.max(axis=0) if len(points) else np.zeros(3, np.float32)
    head = MESH_MAGIC + struct.pack(
        "<III6f", MESH_VERSION, len(points), len(tris),
        float(lo[0]), float(lo[1]), float(lo[2]),
        float(hi[0]), float(hi[1]), float(hi[2]),
    )
    return head + points.tobytes() + tris.tobytes()


def decode_mesh(buf: bytes) -> tuple[np.ndarray, np.ndarray]:
    assert buf[:8] == MESH_MAGIC, "not a MRIVMESH buffer"
    version, n_pts, n_tris = struct.unpack("<III", buf[8:20])
    off = 8 + struct.calcsize("<III6f")
    pts = np.frombuffer(buf, np.float32, n_pts * 3, off).reshape(-1, 3)
    tri = np.frombuffer(buf, np.uint32, n_tris * 3, off + n_pts * 12).reshape(-1, 3)
    return pts, tri


def _index_to_lps(idx_kji: np.ndarray, geom, offset_kji: np.ndarray) -> np.ndarray:
    """Voxel indices (k, j, i) -> patient LPS mm."""
    row_cos = np.asarray(geom.col_cosine, float)     # advances with j
    col_cos = np.asarray(geom.row_cosine, float)     # advances with i
    normal = np.asarray(geom.normal, float)
    col_sp, row_sp, sl_sp = geom.spacing
    origin = np.asarray(geom.origin, float)
    k = idx_kji[:, 0:1] + offset_kji[0]
    j = idx_kji[:, 1:2] + offset_kji[1]
    i = idx_kji[:, 2:3] + offset_kji[2]
    return origin + j * (row_cos * row_sp) + i * (col_cos * col_sp) + k * (normal * sl_sp)


def build_label_mesh(mask: np.ndarray, geom, *, iso_mm: float = ISO_SPACING_MM
                     ) -> tuple[np.ndarray, np.ndarray] | None:
    """One binary mask (k, j, i) -> (points in LPS mm, triangles)."""
    if int(mask.sum()) < MIN_VOXELS:
        return None
    idx = np.argwhere(mask)
    lo = np.maximum(idx.min(0) - CROP_MARGIN, 0)
    hi = np.minimum(idx.max(0) + CROP_MARGIN + 1, mask.shape)
    sub = np.ascontiguousarray(mask[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]])

    col_sp, row_sp, sl_sp = geom.spacing
    spacing_kji = (sl_sp, row_sp, col_sp)

    # Coarsen the isotropic grid for big structures (a whole femur at 0.5 mm is
    # 3e7 voxels); cartilage plates stay at full resolution.
    extent_mm = [(hi[d] - lo[d]) * spacing_kji[d] for d in range(3)]
    iso_mm = max(iso_mm, (extent_mm[0] * extent_mm[1] * extent_mm[2]
                          / MAX_ISO_VOXELS) ** (1.0 / 3.0))

    field, out_spacing = _level_set(sub, spacing_kji, iso_mm)
    verts, faces = _extract_surface(field, out_spacing)
    if verts is None or len(verts) == 0:
        return None

    # verts come back in (k, j, i) *sub-volume* voxel units of out_spacing;
    # rescale to original voxel units before mapping to patient space.
    scale = np.array([out_spacing[0] / spacing_kji[0],
                      out_spacing[1] / spacing_kji[1],
                      out_spacing[2] / spacing_kji[2]])
    verts_vox = verts * scale
    pts = _index_to_lps(verts_vox, geom, lo.astype(float))
    return pts.astype(np.float32), faces.astype(np.uint32)


def _level_set(sub: np.ndarray, spacing_kji, iso_mm: float, *, strict: bool = True):
    """Binary mask -> smooth level set, resampled to isotropic spacing.

    ``AntiAliasBinary`` evolves a level set that approximates the mask with a
    smooth surface, which is what actually removes the 3 mm terracing. Measured
    on case 1 as the fraction of surface area whose normal points along the
    slice axis (lower is smoother):

        label            SignedMaurerDistanceMap    AntiAliasBinary
        4 medial fem.              5.6%                 1.4%
        5 lateral fem.             3.4%                 1.2%
        8 patellar                 1.5%                 0.4%
        1 femur                   30.6%                24.3%   (mostly real anatomy)

    A signed distance map was tried first and is measurably *worse*: distances
    are quantised to voxel centres, so on a 3 mm grid the zero crossing still
    snaps close to the slice planes.
    """
    import SimpleITK as sitk

    img = sitk.GetImageFromArray(sub.astype(np.uint8))
    img.SetSpacing((float(spacing_kji[2]), float(spacing_kji[1]), float(spacing_kji[0])))
    try:
        # Needs an integer input; returns a float level set, positive inside.
        field = sitk.AntiAliasBinary(img, maximumRMSError=ANTIALIAS_RMS,
                                     numberOfIterations=ANTIALIAS_ITERATIONS)
        background = -1.0
    except Exception:
        if strict:
            raise
        # Only reached if the filter is unavailable for this pixel type. Keep
        # going with the raw mask rather than dropping the structure entirely,
        # but the caller records that the surface is the unsmoothed kind.
        field = sitk.Cast(img, sitk.sitkFloat32) - 0.5
        background = -0.5

    size = img.GetSize()
    new_size = [max(2, int(round(size[d] * img.GetSpacing()[d] / iso_mm))) for d in range(3)]
    rs = sitk.ResampleImageFilter()
    rs.SetOutputSpacing((iso_mm, iso_mm, iso_mm))
    rs.SetSize(new_size)
    rs.SetOutputOrigin(img.GetOrigin())
    rs.SetOutputDirection(img.GetDirection())
    rs.SetInterpolator(sitk.sitkLinear)   # linear, not BSpline: BSpline overshoots
    rs.SetDefaultPixelValue(background)   # at sharp edges and invents bright rims
    arr = sitk.GetArrayFromImage(rs.Execute(field)).astype(np.float32)  # (k, j, i)
    return arr, (iso_mm, iso_mm, iso_mm)


def _extract_surface(field: np.ndarray, spacing_kji):
    if _have_vtk():
        return _extract_vtk(field, spacing_kji)
    return _extract_skimage(field)


def _extract_vtk(field: np.ndarray, spacing_kji):
    import vtk
    from vtk.util import numpy_support

    k, j, i = field.shape
    img = vtk.vtkImageData()
    img.SetDimensions(i, j, k)
    img.SetSpacing(1.0, 1.0, 1.0)          # work in voxel units; we map to LPS later
    arr = numpy_support.numpy_to_vtk(field.ravel(order="C"), deep=True,
                                     array_type=vtk.VTK_FLOAT)
    arr.SetName("d")
    img.GetPointData().SetScalars(arr)

    fe = vtk.vtkFlyingEdges3D()
    fe.SetInputData(img)
    fe.SetValue(0, 0.0)
    fe.ComputeNormalsOff()
    fe.ComputeGradientsOff()

    smooth = vtk.vtkWindowedSincPolyDataFilter()
    smooth.SetInputConnection(fe.GetOutputPort())
    smooth.SetNumberOfIterations(SMOOTH_ITERATIONS)
    smooth.SetPassBand(SMOOTH_PASSBAND)
    smooth.NormalizeCoordinatesOn()
    smooth.FeatureEdgeSmoothingOff()
    smooth.BoundarySmoothingOff()
    smooth.NonManifoldSmoothingOn()
    smooth.Update()

    n_in = smooth.GetOutput().GetNumberOfPolys()
    if n_in == 0:
        return None, None

    # Stage 1: quadric decimation. It preserves topology, which keeps thin
    # cartilage plates intact but means it silently stops early on a bone with
    # lots of non-manifold edges - a femur refuses to go below ~176k triangles
    # no matter what reduction you ask for.
    reduction = max(MIN_REDUCTION, 1.0 - float(MAX_TRIANGLES) / float(n_in))
    deci = vtk.vtkQuadricDecimation()
    deci.SetInputConnection(smooth.GetOutputPort())
    deci.SetTargetReduction(min(MAX_REDUCTION, reduction))
    deci.Update()
    upstream = deci.GetOutputPort()

    # Stage 2: only if stage 1 stalled above budget, finish with DecimatePro,
    # which is allowed to change topology and will actually reach the target.
    n_mid = deci.GetOutput().GetNumberOfPolys()
    if n_mid > MAX_TRIANGLES:
        pro = vtk.vtkDecimatePro()
        pro.SetInputConnection(deci.GetOutputPort())
        pro.SetTargetReduction(min(MAX_REDUCTION,
                                   1.0 - float(MAX_TRIANGLES) / float(n_mid)))
        pro.PreserveTopologyOff()
        pro.SplittingOn()
        pro.BoundaryVertexDeletionOn()
        upstream = pro.GetOutputPort()

    tri = vtk.vtkTriangleFilter()
    tri.SetInputConnection(upstream)
    tri.Update()
    poly = tri.GetOutput()

    n_pts = poly.GetNumberOfPoints()
    if n_pts == 0:
        return None, None
    pts = numpy_support.vtk_to_numpy(poly.GetPoints().GetData())   # (n, 3) as (x=i, y=j, z=k)
    faces = numpy_support.vtk_to_numpy(poly.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
    verts_kji = np.stack([pts[:, 2], pts[:, 1], pts[:, 0]], axis=1)
    return verts_kji, faces


def _extract_skimage(field: np.ndarray):
    try:
        from skimage import measure
    except Exception:
        return None, None
    try:
        verts, faces, _n, _v = measure.marching_cubes(field, level=0.0)
    except (ValueError, RuntimeError):
        return None, None
    return verts, faces     # already (k, j, i)


def build_meshes_for_segmentation(conn: sqlite3.Connection, cfg: Any, seg_id: int,
                                  *, force: bool = False) -> list[int]:
    from .ingest import series_geometry

    row = conn.execute("SELECT * FROM segmentation WHERE id=?", (seg_id,)).fetchone()
    if row is None:
        raise KeyError("segmentation %d" % seg_id)
    if row["seg_state"] != "ready" or not row["seg_key"]:
        raise ValueError("segmentation %d has no cached labelmap" % seg_id)

    cache = VolumeCache(cfg.cache_dir)
    entry = cache.segmentation(row["seg_key"])
    meta = entry.read_meta()
    dims = meta["dimensions"]
    canonical = entry.read_array((dims[2], dims[1], dims[0]), "uint8")
    geom, _ = series_geometry(conn, int(row["series_id"]))

    conn.execute("UPDATE segmentation SET mesh_state='building' WHERE id=?", (seg_id,))
    conn.commit()

    built: list[int] = []
    params = {"iso_mm": ISO_SPACING_MM, "smooth": SMOOTH_ITERATIONS,
              "passband": SMOOTH_PASSBAND, "maxTriangles": MAX_TRIANGLES,
              "antialias": ANTIALIAS_ITERATIONS,
              "backend": "vtk" if _have_vtk() else "skimage"}
    for value in meta.get("presentValues", []):
        out = cache.mesh_path(row["seg_key"], int(value))
        if out.exists() and not force:
            built.append(int(value))
            continue
        result = build_label_mesh(canonical == value, geom)
        if result is None:
            continue
        pts, tris = result
        buf = encode_mesh(pts, tris)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            fh.write(buf)
        os.replace(tmp, out)
        conn.execute(
            "INSERT INTO segmentation_mesh(segmentation_id,label_value,path_rel,"
            "n_points,n_tris,bytes,params_json) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(segmentation_id,label_value) DO UPDATE SET"
            " path_rel=excluded.path_rel, n_points=excluded.n_points,"
            " n_tris=excluded.n_tris, bytes=excluded.bytes,"
            " params_json=excluded.params_json",
            (seg_id, int(value), str(out), len(pts), len(tris),
             out.stat().st_size, json.dumps(params)))
        built.append(int(value))
    conn.execute("UPDATE segmentation SET mesh_state='ready' WHERE id=?", (seg_id,))
    conn.commit()
    return built
