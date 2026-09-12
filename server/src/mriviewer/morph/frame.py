"""The knee's anatomic coordinate frame, derived from the labels themselves.

Everything downstream - subregion boundaries, medial/lateral naming, the
anisotropy weighting - is expressed in this frame, so it has to be right and it
has to be checkable.

The medial direction is deliberately derived **from the label set** (medial
cartilage centroid minus lateral cartilage centroid) rather than trusted from
the DICOM. That gives a free consistency check against the header laterality,
and it is what catches the known 0826 case-1 medial/lateral swap: 23 of 24
annotated cases put {4,6} nearer the midline, case 1 puts them further. A
mirrored subregion report is worse than no report, so that disagreement is a
hard failure, not a warning.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

import numpy as np

# Labels are anatomy, not magic numbers - see labelsets/knee_cartilage_0826_v1.yaml
FEMUR, TIBIA, PATELLA = 1, 2, 3
MED_FEM_CART, LAT_FEM_CART = 4, 5
MED_TIB_CART, LAT_TIB_CART = 6, 7
PAT_CART = 8

# A knee this far off the midline can have its side inferred from position alone.
MIN_MIDLINE_OFFSET_MM = 20.0


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.zeros(3)


def _centroid_lps(mask: np.ndarray, geom) -> np.ndarray | None:
    """Centroid of a label in patient LPS mm."""
    idx = np.argwhere(mask)
    if not len(idx):
        return None
    row_cos = np.asarray(geom.col_cosine, float)   # advances with row index j
    col_cos = np.asarray(geom.row_cosine, float)   # advances with column index i
    normal = np.asarray(geom.normal, float)
    col_sp, row_sp, sl_sp = geom.spacing
    origin = np.asarray(geom.origin, float)
    mean = idx.mean(0)                              # (k, j, i)
    return (origin + mean[1] * row_cos * row_sp
            + mean[2] * col_cos * col_sp + mean[0] * normal * sl_sp)


@dataclass
class KneeFrame:
    """Right-handed anatomic axes in LPS mm, plus the QC that validates them."""
    e_ML: tuple[float, float, float]     # medial-lateral, +ve toward the patient's left
    e_AP: tuple[float, float, float]     # anterior-posterior, +ve anterior
    e_SI: tuple[float, float, float]     # superior-inferior, +ve superior (femur side)
    medial_sign: float                   # +1 if +e_ML points medially for THIS knee
    laterality: str                      # 'L' | 'R', the side we believe
    header_laterality: str | None        # what the DICOM claimed, if anything
    medial_lateral_consistent: bool
    notes: list[str]

    @property
    def axes(self) -> np.ndarray:
        """3x3 with rows [e_ML, e_AP, e_SI]; project a point by `axes @ p`."""
        return np.array([self.e_ML, self.e_AP, self.e_SI], float)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def knee_frame(masks: dict[int, np.ndarray], geom,
               header_laterality: str | None = None) -> KneeFrame:
    """Build the anatomic frame from bone and cartilage centroids.

    ``masks`` maps label value -> boolean array in canonical (k, j, i).
    """
    notes: list[str] = []
    c = {v: _centroid_lps(m, geom) for v, m in masks.items()}

    # Superior-inferior: femur sits above tibia. Robust for any knee, any pose.
    if c.get(FEMUR) is None or c.get(TIBIA) is None:
        raise ValueError("need both femur and tibia labels to build the knee frame")
    e_SI = _unit(c[FEMUR] - c[TIBIA])

    # The sagittal slice normal is medial-lateral to within a few degrees.
    e_ML = _unit(np.asarray(geom.normal, float) - np.dot(geom.normal, e_SI) * e_SI)

    e_AP = _unit(np.cross(e_SI, e_ML))
    # Point +e_AP anteriorly using the patella, which is the most anterior structure.
    if c.get(PATELLA) is not None:
        mid = 0.5 * (c[FEMUR] + c[TIBIA])
        if np.dot(c[PATELLA] - mid, e_AP) < 0:
            e_AP = -e_AP
            e_ML = _unit(np.cross(e_AP, e_SI))
    else:
        notes.append("no patella label: anterior direction assumed, not verified")

    # Medial direction from the labels themselves.
    med = [c[v] for v in (MED_FEM_CART, MED_TIB_CART) if c.get(v) is not None]
    lat = [c[v] for v in (LAT_FEM_CART, LAT_TIB_CART) if c.get(v) is not None]
    medial_sign = 0.0
    if med and lat:
        medial_sign = float(np.sign(np.dot(np.mean(med, 0) - np.mean(lat, 0), e_ML)))
    if medial_sign == 0.0:
        medial_sign = 1.0
        notes.append("medial direction undetermined from labels; assumed +e_ML")

    # In LPS, +x is the patient's LEFT. Medial on a left knee points right (-x);
    # medial on a right knee points left (+x).
    medial_lps = medial_sign * e_ML
    laterality = "L" if medial_lps[0] < 0 else "R"

    # Cross-check against a source that does NOT depend on the cartilage labels:
    # which side of the midline the joint physically sits on. A left knee is at
    # +x in LPS. The 0826 scanner writes no Laterality tag, so without this the
    # medial/lateral check has nothing to compare against and a swapped label
    # pair - which really does occur in this data - passes silently.
    joint_x = float(0.5 * (c[FEMUR] + c[TIBIA])[0])
    position_laterality: str | None = None
    if abs(joint_x) >= MIN_MIDLINE_OFFSET_MM:
        position_laterality = "L" if joint_x > 0 else "R"
    else:
        notes.append(
            "joint centre is only %.0f mm off the midline; too close to infer the "
            "side from position" % abs(joint_x))

    truth = header_laterality if header_laterality in ("L", "R") else position_laterality
    source = "DICOM header" if header_laterality in ("L", "R") else "joint position"

    consistent = True
    if truth is not None:
        consistent = truth == laterality
        if not consistent:
            notes.append(
                "the medial/lateral cartilage labels look swapped: their geometry "
                "implies a %s knee, but the %s says %s"
                % (laterality, source, truth))
            laterality = truth      # trust the independent source, not the labels
    else:
        notes.append("no independent laterality to cross-check the labels against")

    return KneeFrame(
        e_ML=tuple(float(x) for x in e_ML),     # type: ignore[arg-type]
        e_AP=tuple(float(x) for x in e_AP),     # type: ignore[arg-type]
        e_SI=tuple(float(x) for x in e_SI),     # type: ignore[arg-type]
        medial_sign=medial_sign,
        laterality=laterality,
        header_laterality=header_laterality,
        medial_lateral_consistent=consistent,
        notes=notes,
    )


def native_spacing_along(frame: KneeFrame, geom, normals: np.ndarray) -> np.ndarray:
    """Effective sampling interval of the ORIGINAL data along each normal, in mm.

    This is the honest anisotropy number. Thickness is measured along the
    surface normal, so the resolution that actually limits that measurement is
    the native voxel pitch projected onto the normal::

        res_eff = sqrt( (n.e_ML * s_ML)^2 + (n.e_AP * s_AP)^2 + (n.e_SI * s_SI)^2 )

    For the 0826 sagittal knees (0.2917 x 0.2917 x 3.0 mm) this is ~0.29 mm
    where the normal lies in the sagittal plane - the central condyles,
    trochlea, tibial plateaux and patella - and 3.0 mm where it points
    medio-laterally: the condylar rims, the tibial spine walls, and the first
    and last slice of every plate. Those are exactly the places the measurement
    must not be trusted, and this makes the distinction computed rather than
    asserted.
    """
    col_sp, row_sp, sl_sp = geom.spacing      # (i, j, k) = (column, row, slice)
    # Native axes in LPS: slice normal, row direction, column direction.
    axes = np.array([np.asarray(geom.normal, float),
                     np.asarray(geom.col_cosine, float),
                     np.asarray(geom.row_cosine, float)])
    pitch = np.array([sl_sp, row_sp, col_sp], float)
    proj = normals @ axes.T                    # (N, 3) cosines against native axes
    return np.sqrt(((proj * pitch) ** 2).sum(axis=1))
