"""Pure numeric checks for the axis-aligned fuel proxy; not flight evidence."""

from __future__ import annotations

import math

import numpy as np
import pytest

from wsorb.flight.axis_avoidance import (
    AxisManeuver,
    axis_aligned_accelerations,
    component_count,
    diagonal_extra_fraction,
    rank_maneuvers,
)


def test_axis_candidates_are_six_unit_body_axis_burns() -> None:
    candidates = axis_aligned_accelerations(0.5)
    assert len(candidates) == 6
    for candidate in candidates:
        assert math.hypot(*candidate) == pytest.approx(0.5)
        assert component_count(candidate) == 1


def test_diagonal_extra_fraction_is_geometry_not_a_fuel_measurement() -> None:
    assert diagonal_extra_fraction(math.pi / 4.0) == pytest.approx(math.sqrt(2.0) - 1.0)
    assert diagonal_extra_fraction(0.0) == pytest.approx(0.0)
    assert diagonal_extra_fraction(math.pi / 2.0) == pytest.approx(0.0)
    assert diagonal_extra_fraction(math.pi / 4.0) == pytest.approx(0.41421356237309503)


def test_rank_prefers_a_safe_single_axis_burn_at_equal_clearance_and_progress() -> None:
    single = AxisManeuver((0.5, 0.0, 0.0), clearance_m=10.0, progress_m=5.0)
    diagonal = AxisManeuver((0.3535, 0.3535, 0.0), clearance_m=10.0, progress_m=5.0)
    ranked = rank_maneuvers(
        [diagonal, single],
        clearance_weight=1.0,
        progress_weight=1.0,
        fuel_weight=1.0,
        minimum_clearance_m=5.0,
    )
    assert ranked == (single,)
    assert ranked[0].active_axes == 1
    comparable = rank_maneuvers(
        [diagonal, single],
        clearance_weight=1.0,
        progress_weight=1.0,
        fuel_weight=1.0,
        minimum_clearance_m=5.0,
        prefer_single_axis=False,
    )
    assert comparable[0] == single
    assert single.l1_effort < diagonal.l1_effort


def test_rank_falls_back_to_a_diagonal_when_no_single_axis_is_safe() -> None:
    unsafe_single = AxisManeuver((0.5, 0.0, 0.0), clearance_m=4.9, progress_m=20.0)
    safe_diagonal = AxisManeuver((0.25, 0.25, 0.0), clearance_m=6.0, progress_m=5.0)
    ranked = rank_maneuvers(
        [unsafe_single, safe_diagonal],
        clearance_weight=1.0,
        progress_weight=1.0,
        fuel_weight=0.1,
        minimum_clearance_m=5.0,
    )
    assert ranked == (safe_diagonal,)


def test_maneuver_rejects_negative_clearance_and_nonfinite_vectors() -> None:
    with pytest.raises(ValueError):
        AxisManeuver((0.5, 0.0, 0.0), clearance_m=-1.0, progress_m=0.0)
    with pytest.raises(ValueError):
        AxisManeuver((np.nan, 0.0, 0.0), clearance_m=1.0, progress_m=0.0)


def test_component_count_uses_an_explicit_tolerance() -> None:
    assert component_count((0.5, 1.0e-13, 0.0)) == 1
    assert component_count((0.5, 1.0e-13, 0.0), tolerance=0.0) == 2
