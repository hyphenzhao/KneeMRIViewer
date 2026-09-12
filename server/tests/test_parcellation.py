"""Parcellation invariants that must hold on any knee.

These are not accuracy tests - there is no ground-truth partition to compare
against. They pin the properties whose violation silently mirrors or drops a
subregion, which is the failure mode that would reach a clinician looking
entirely plausible.
"""
from __future__ import annotations

import numpy as np
import pytest

from mriviewer.morph.parcellation import (SUBREGION_NAMES, _fit_circle,
                                          parcellate_femur, parcellate_patella,
                                          parcellate_tibia)


def test_circle_fit_recovers_a_known_circle() -> None:
    t = np.linspace(0.3, 2.9, 200)          # a partial arc, as a condyle is
    cx, cy, r = _fit_circle(20.0 + 13.0 * np.cos(t), -5.0 + 13.0 * np.sin(t))
    assert (cx, cy, r) == pytest.approx((20.0, -5.0, 13.0), abs=0.01)


def _condyle(n: int = 4000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """A synthetic condyle: an arc swept across a medio-lateral width."""
    rng = np.random.default_rng(seed)
    theta = rng.uniform(np.deg2rad(20.0), np.deg2rad(200.0), n)
    ml = rng.uniform(-12.0, 12.0, n)
    points = np.stack([ml, 18.0 * np.cos(theta), -18.0 * np.sin(theta)], axis=1)
    slice_index = np.round(ml / 3.0).astype(int)      # 3 mm slices
    return points, slice_index


def test_femur_produces_all_five_subregions() -> None:
    points, slices = _condyle()
    parc = parcellate_femur(points, slices, medial=True)
    assert set(c for c in parc.codes.tolist() if c) == {
        "MFT", "ecMF", "ccMF", "icMF", "pMF"}


def test_femur_every_vertex_is_assigned() -> None:
    points, slices = _condyle()
    parc = parcellate_femur(points, slices, medial=True)
    assert all(parc.codes.tolist()), "an unassigned vertex would vanish from every mean"


def test_femur_central_thirds_are_ordered_lateral_to_medial() -> None:
    """+ML is medial, so icMF must sit medial to ccMF, which sits medial to ecMF.

    Getting this backwards mirrors the report without changing any other
    number, so nothing else would catch it.
    """
    points, slices = _condyle()
    parc = parcellate_femur(points, slices, medial=True)
    ml = points[:, 0]
    centres = [ml[parc.codes == c].mean() for c in ("ecMF", "ccMF", "icMF")]
    assert centres[0] < centres[1] < centres[2]


def test_femur_boundaries_follow_the_condyle_not_absolute_angles() -> None:
    """A condyle covering a different angular range must still get all five.

    Fixed degree thresholds failed exactly here: the medial and lateral
    condyles span different ranges on real data, and one of them ended up with
    a near-empty trochlear sector.
    """
    rng = np.random.default_rng(1)
    theta = rng.uniform(np.deg2rad(5.0), np.deg2rad(150.0), 4000)
    ml = rng.uniform(-12.0, 12.0, 4000)
    points = np.stack([ml, 18 * np.cos(theta), -18 * np.sin(theta)], axis=1)
    parc = parcellate_femur(points, np.round(ml / 3.0).astype(int), medial=False)

    counts = {c: int((parc.codes == c).sum()) for c in
              ("LFT", "ecLF", "ccLF", "icLF", "pLF")}
    assert min(counts.values()) > 0.02 * len(points), counts


def _plateau(n: int = 4000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    ml = rng.uniform(-14.0, 14.0, n)
    ap = rng.uniform(-18.0, 18.0, n)
    return (np.stack([ml, ap, rng.normal(0.0, 0.4, n)], axis=1),
            np.round(ml / 3.0).astype(int))


def test_tibia_central_roi_is_about_the_configured_area_fraction() -> None:
    points, slices = _plateau()
    parc = parcellate_tibia(points, slices, medial=True)
    fraction = (parc.codes == "cMT").mean()
    assert fraction == pytest.approx(0.20, abs=0.02)


def test_tibia_quadrants_lie_where_their_names_say() -> None:
    points, slices = _plateau()
    parc = parcellate_tibia(points, slices, medial=True)
    ml, ap = points[:, 0], points[:, 1]
    assert ap[parc.codes == "aMT"].mean() > ap[parc.codes == "pMT"].mean()
    assert ml[parc.codes == "iMT"].mean() > ml[parc.codes == "eMT"].mean()


def test_patella_facets_split_medial_from_lateral() -> None:
    points, slices = _plateau(seed=3)
    parc = parcellate_patella(points, slices)
    ml = points[:, 0]
    assert ml[parc.codes == "MP"].mean() > ml[parc.codes == "LP"].mean()


def test_slice_support_counts_distinct_native_slices() -> None:
    points, slices = _condyle()
    parc = parcellate_femur(points, slices, medial=True)
    for code, support in parc.slice_support.items():
        assert support == len(np.unique(slices[parc.codes == code])), code


def test_every_emitted_code_has_a_chinese_label() -> None:
    """A code missing from the table would reach the dashboard as a bare slug."""
    for points, slices, fn in (
        (*_condyle(), lambda p, s: parcellate_femur(p, s, medial=True)),
        (*_condyle(), lambda p, s: parcellate_femur(p, s, medial=False)),
        (*_plateau(), lambda p, s: parcellate_tibia(p, s, medial=True)),
        (*_plateau(), lambda p, s: parcellate_tibia(p, s, medial=False)),
        (*_plateau(), parcellate_patella),
    ):
        for code in {c for c in fn(points, slices).codes.tolist() if c}:
            assert code in SUBREGION_NAMES, code
