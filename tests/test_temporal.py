"""Analytical numeric fixtures, not engine or sensor observations."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from wsorb.flight.temporal import MotionSegment, TemporalObstacles, motion_clear

ZERO = (0.0, 0.0, 0.0)


def sphere(
    center=(100.0, 0.0, 0.0), velocity=ZERO, radius=1.0, growth=0.0, growth2=0.0
) -> TemporalObstacles:
    return TemporalObstacles(
        np.array([center]),
        np.array([velocity]),
        np.array([radius]),
        np.array([growth]),
        np.array([growth2]),
    )


def lease_and_brake() -> tuple[MotionSegment, ...]:
    return (
        MotionSegment(1.0, ZERO, (2.0, 0.0, 0.0), ZERO),
        MotionSegment(2.0, (2.0, 0.0, 0.0), (2.0, 0.0, 0.0), (-1.0, 0.0, 0.0)),
        MotionSegment(0.5, (4.0, 0.0, 0.0), ZERO, ZERO),
    )


def test_spatial_crossing_at_different_times_is_clear_but_same_time_is_blocked() -> None:
    path = (MotionSegment(2.0, (-2.0, 0.0, 0.0), (2.0, 0.0, 0.0), ZERO),)
    # The ship passes the origin at t=1. These spheres pass it at t=0.5 and t=1.
    different_time = sphere((0.0, -1.0, 0.0), (0.0, 2.0, 0.0), radius=0.25)
    same_time = sphere((0.0, -2.0, 0.0), (0.0, 2.0, 0.0), radius=0.25)
    assert motion_clear(path, different_time, max_interval_s=2.0)
    assert not motion_clear(path, same_time, max_interval_s=2.0)


def test_fast_opposing_motion_hits_between_clear_endpoints() -> None:
    path = (MotionSegment(0.25, (-10.0, 0.0, 0.0), (80.0, 0.0, 0.0), ZERO),)
    obstacle = sphere((10.0, 0.0, 0.0), (-80.0, 0.0, 0.0), radius=0.5)
    endpoints = np.array([0.0, path[0].duration_s])[:, None]
    relative = (
        np.array(path[0].position_m)
        + endpoints * path[0].velocity_mps
        - obstacle.centers_m
        - endpoints * obstacle.velocities_mps
    )
    assert np.all(np.linalg.norm(relative, axis=1) > obstacle.radii_m[0])
    assert not motion_clear(path, obstacle)


def test_three_dimensional_chord_tangency_is_blocked() -> None:
    # The relative chord passes (0,0,1) at t=0.5, away from either endpoint.
    path = (MotionSegment(1.0, (-3.0, -4.0, 1.0), (6.0, 8.0, 0.0), ZERO),)
    assert not motion_clear(path, sphere(ZERO), max_interval_s=1.0)
    assert motion_clear(path, sphere(ZERO, radius=1.0 - 1e-6), max_interval_s=1.0)


def test_curvature_bulge_hits_while_the_uninflated_chord_is_clear() -> None:
    # y(t)=1-4t+4t**2 reaches zero at t=0.5; the endpoint chord stays at y=1.
    path = (MotionSegment(1.0, (-1.0, 1.0, 0.0), (2.0, -4.0, 0.0), (0.0, 8.0, 0.0)),)
    obstacle = sphere(ZERO, radius=0.1)
    assert not motion_clear(path, obstacle, max_interval_s=1.0)
    assert not motion_clear(path, obstacle)
    higher = (replace(path[0], position_m=(-1.0, 2.0, 0.0)),)
    assert motion_clear(higher, obstacle, max_interval_s=1.0)


@pytest.mark.parametrize(("growth", "growth2"), [(1.0, 0.0), (0.0, 0.5), (0.5, 0.25)])
def test_late_radius_growth_and_tangency_are_blocked(growth, growth2) -> None:
    obstacle = sphere((3.0, 0.0, 0.0), growth=growth, growth2=growth2)
    assert motion_clear((MotionSegment(1.9, ZERO, ZERO, ZERO),), obstacle)
    assert not motion_clear((MotionSegment(2.0, ZERO, ZERO, ZERO),), obstacle)
    assert not motion_clear((MotionSegment(2.1, ZERO, ZERO, ZERO),), obstacle)


def test_radius_uses_interval_end_and_quadratic_coefficient_is_not_halved() -> None:
    stationary = (MotionSegment(1.0, ZERO, ZERO, ZERO),)
    # R(0)=1, R(1)=3. Radius at the start, or a half coefficient, misses tangency.
    assert not motion_clear(stationary, sphere((3.0, 0.0, 0.0), growth2=2.0), max_interval_s=1.0)


def test_global_elapsed_time_is_not_reset_for_motion_or_growth() -> None:
    assert motion_clear(lease_and_brake(), sphere())
    # At decision t=2.5 the braking ship is at x=3.875 and the sphere at (4,0,0).
    moving = sphere((4.0, 5.0, 0.0), (0.0, -2.0, 0.0), radius=0.2)
    assert not motion_clear(lease_and_brake(), moving)
    stationary = (MotionSegment(1.0, ZERO, ZERO, ZERO),) * 2
    assert not motion_clear(stationary, sphere((3.0, 0.0, 0.0), growth=1.0))


@pytest.mark.parametrize("field", ["position_m", "velocity_mps"])
def test_lease_and_brake_reject_position_or_velocity_discontinuity(field) -> None:
    lease, brake, coast = lease_and_brake()
    value = np.array(getattr(brake, field))
    value[1] = 1e-7
    assert not motion_clear((lease, replace(brake, **{field: value}), coast), sphere())


def test_continuity_tolerance_is_absolute_and_covered_by_clearance_padding() -> None:
    lease, brake, _ = lease_and_brake()
    within_tolerance = replace(brake, position_m=(2.0, 5e-9, 0.0), velocity_mps=(2.0, 5e-9, 0.0))
    assert motion_clear((lease, within_tolerance), sphere())
    # Large coordinates must not create a relative continuity tolerance.
    first = MotionSegment(1.0, (1e9, 0.0, 0.0), ZERO, ZERO)
    second = replace(first, position_m=(1e9 + 1.0, 0.0, 0.0))
    assert not motion_clear((first, second), sphere())
    stationary = (MotionSegment(1.0, ZERO, ZERO, ZERO),)
    assert not motion_clear(stationary, sphere((1.0 + 1e-8, 0.0, 0.0)))
    assert motion_clear(stationary, sphere((1.0 + 1e-6, 0.0, 0.0)))


def test_numeric_padding_uses_absolute_coordinates_before_cancellation() -> None:
    stationary = (MotionSegment(1.0, ZERO, ZERO, ZERO),)
    assert motion_clear(stationary, sphere((1.1, 0.0, 0.0)))
    translated = (replace(stationary[0], position_m=(1e12, 0.0, 0.0)),)
    assert not motion_clear(translated, sphere((1e12 + 1.1, 0.0, 0.0)))


def test_every_obstacle_is_retained_including_the_last_radar_row() -> None:
    count = 4099
    centers = np.full((count, 3), 100.0)
    obstacle = TemporalObstacles(
        centers, np.zeros((count, 3)), np.ones(count), np.zeros(count), np.zeros(count)
    )
    stationary = (MotionSegment(0.25, ZERO, ZERO, ZERO),)
    assert motion_clear(stationary, obstacle)
    centers[-1] = ZERO
    blocked = replace(obstacle, centers_m=centers)
    assert blocked.centers_m.shape == (count, 3)
    assert not motion_clear(stationary, blocked)


def test_obstacle_arrays_are_defensive_float64_copies_and_permanently_readonly() -> None:
    source = {
        "centers_m": np.array([[10, 20, 30]]),
        "velocities_mps": np.array([[-1, 2, 3]]),
        "radii_m": np.array([2]),
        "growth_mps": np.array([3]),
        "growth_mps2": np.array([4]),
    }
    obstacle = TemporalObstacles(**source)
    for name, original in source.items():
        saved = original.copy()
        original[:] = 0
        value = getattr(obstacle, name)
        np.testing.assert_array_equal(value, saved)
        assert value.dtype == np.float64
        assert not np.shares_memory(value, original)
        assert not value.flags.writeable
        with pytest.raises(ValueError):
            value.flat[0] = 0.0
        with pytest.raises(ValueError):
            value.setflags(write=True)
    with pytest.raises(FrozenInstanceError):
        obstacle.radii_m = np.ones(1)
    assert obstacle != replace(obstacle)


def test_motion_segment_copies_vectors_to_frozen_float_tuples() -> None:
    source = np.array([1, 2, 3])
    segment = MotionSegment(np.float64(1), source, ZERO, ZERO)
    source[:] = 0
    assert segment.position_m == (1.0, 2.0, 3.0)
    assert all(isinstance(value, float) for value in segment.position_m)
    with pytest.raises(FrozenInstanceError):
        segment.duration_s = 2.0


@pytest.mark.parametrize(
    "duration", [0.0, -1.0, math.nan, math.inf, -math.inf, True, np.bool_(False), "1"]
)
def test_motion_segment_rejects_invalid_duration(duration) -> None:
    with pytest.raises(ValueError):
        MotionSegment(duration, ZERO, ZERO, ZERO)


@pytest.mark.parametrize("field", ["position_m", "velocity_mps", "acceleration_mps2"])
@pytest.mark.parametrize(
    "value",
    [
        (math.nan, 0.0, 0.0),
        (math.inf, 0.0, 0.0),
        (0.0, True, 0.0),
        [0.0, np.bool_(False), 0.0],
        np.array([True, False, False]),
        (1j, 0.0, 0.0),
        ("1", "2", "3"),
        (0.0, 0.0),
        [[0.0, 0.0, 0.0]],
    ],
)
def test_motion_segment_rejects_invalid_vectors(field, value) -> None:
    with pytest.raises(ValueError):
        replace(MotionSegment(1.0, ZERO, ZERO, ZERO), **{field: value})


@pytest.mark.parametrize("count", [0, 4100])
def test_obstacles_reject_out_of_budget_counts(count) -> None:
    with pytest.raises(ValueError):
        TemporalObstacles(
            np.zeros((count, 3)),
            np.zeros((count, 3)),
            np.ones(count),
            np.zeros(count),
            np.zeros(count),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("centers_m", [0.0, 0.0, 0.0]),
        ("centers_m", [[0.0, 0.0]]),
        ("centers_m", [[True, 0.0, 0.0]]),
        ("velocities_mps", [[0.0, 0.0]]),
        ("velocities_mps", [[0.0, False, 0.0]]),
        ("radii_m", [[1.0]]),
        ("radii_m", [0.0]),
        ("radii_m", [-1.0]),
        ("radii_m", [True]),
        ("growth_mps", [-1.0]),
        ("growth_mps", [False]),
        ("growth_mps2", [-1.0]),
        ("growth_mps2", [False]),
        ("growth_mps2", []),
    ],
)
def test_obstacles_reject_invalid_shapes_signs_and_booleans(field, value) -> None:
    with pytest.raises(ValueError):
        replace(sphere(), **{field: value})


@pytest.mark.parametrize(
    "field", ["centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"]
)
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1j, "bad"])
def test_obstacles_reject_nonfinite_or_nonreal_arrays(field, value) -> None:
    shape = (1, 3) if field in ("centers_m", "velocities_mps") else (1,)
    with pytest.raises(ValueError):
        replace(sphere(), **{field: np.full(shape, value)})


@pytest.mark.parametrize(
    "maximum", [0.0, -1.0, math.nan, math.inf, True, np.bool_(True), "0.25", 5e-324]
)
def test_invalid_or_unrepresentable_interval_limits_fail_closed(maximum) -> None:
    assert not motion_clear(
        (MotionSegment(1.0, ZERO, ZERO, ZERO),), sphere(), max_interval_s=maximum
    )


def test_path_shape_time_and_interval_budgets_fail_closed_without_truncation() -> None:
    one = MotionSegment(1.0, ZERO, ZERO, ZERO)
    assert not motion_clear((), sphere())
    assert not motion_clear([one], sphere())
    assert not motion_clear((None,), sphere())
    assert not motion_clear((one,), None)
    assert not motion_clear((one,) * 5, sphere())
    assert not motion_clear((replace(one, duration_s=60.00001),), sphere())
    assert not motion_clear((replace(one, duration_s=60.0), one), sphere())
    assert not motion_clear(
        (replace(one, duration_s=60.0), replace(one, duration_s=5e-324)), sphere()
    )
    assert motion_clear((replace(one, duration_s=15.0),) * 4, sphere())
    assert motion_clear((one,), sphere(), max_interval_s=1.0 / 4096)
    assert not motion_clear((one,), sphere(), max_interval_s=np.nextafter(1.0 / 4096, 0.0))
    assert not motion_clear((one,) * 4, sphere(), max_interval_s=1.0 / 1024.5)


def test_nonbinary_subdivision_still_checks_the_entire_horizon() -> None:
    stationary = (MotionSegment(2.0, ZERO, ZERO, ZERO),)
    assert motion_clear(stationary, sphere(), max_interval_s=0.1)
    assert not motion_clear(stationary, sphere((3.0, 0.0, 0.0), growth=1.0), max_interval_s=0.1)


def test_finite_inputs_with_unrepresentable_arithmetic_fail_closed() -> None:
    maximum = float(np.finfo(np.float64).max)
    stationary = MotionSegment(2.0, ZERO, ZERO, ZERO)
    cases = [
        (
            replace(stationary, position_m=(maximum, 0.0, 0.0), velocity_mps=(maximum, 0.0, 0.0)),
            sphere(),
        ),
        (
            MotionSegment(1.0, ZERO, (0.75 * maximum, 0.0, 0.0), (0.5 * maximum, 0.0, 0.0)),
            sphere(),
        ),
        (replace(stationary, duration_s=1.0, acceleration_mps2=(maximum,) * 3), sphere()),
        (stationary, sphere((maximum, 0.0, 0.0), (maximum, 0.0, 0.0))),
        (replace(stationary, position_m=(maximum, 0.0, 0.0)), sphere((-maximum, 0.0, 0.0))),
        (stationary, sphere(growth=maximum)),
        (stationary, sphere(growth2=maximum)),
        (stationary, sphere((maximum, 0.0, 0.0), radius=maximum)),
        # The true path is distant, but the chord's squared length overflows.
        (MotionSegment(1.0, (-1e200, 1e200, 0.0), (2e200, 0.0, 0.0), ZERO), sphere(ZERO)),
    ]
    for segment, obstacles in cases:
        assert not motion_clear((segment,), obstacles)


@pytest.mark.parametrize(("offset_m", "clear"), [(0.0, False), (0.05, False), (2.0, True)])
def test_piecewise_analytical_polynomials_agree_with_dense_regression(offset_m, clear) -> None:
    first = MotionSegment(0.75, (-2.0, 1.0, 3.0), (3.0, -1.0, 0.0), (0.0, 4.0, -2.0))
    p = np.array(first.position_m)
    v = np.array(first.velocity_mps)
    a = np.array(first.acceleration_mps2)
    d = first.duration_s
    second = MotionSegment(0.5, p + v * d + 0.5 * a * d * d, v + a * d, (0.0, -3.0, 1.0))
    tau = 0.25
    hit = (
        np.array(second.position_m)
        + np.array(second.velocity_mps) * tau
        + 0.5 * np.array(second.acceleration_mps2) * tau * tau
    )
    velocity = np.array([0.0, -0.25, 0.5])
    obstacle = sphere(hit - velocity + [0.0, 0.0, offset_m], velocity, 0.1, 0.05, 0.025)
    minimum = math.inf
    elapsed = 0.0
    for segment in (first, second):
        local = np.linspace(0.0, segment.duration_s, 4097)
        global_time = elapsed + local
        planned = (
            np.array(segment.position_m)
            + local[:, None] * segment.velocity_mps
            + 0.5 * local[:, None] ** 2 * segment.acceleration_mps2
        )
        centers = obstacle.centers_m + global_time[:, None] * obstacle.velocities_mps
        radii = (
            obstacle.radii_m
            + global_time * obstacle.growth_mps
            + global_time**2 * obstacle.growth_mps2
        )
        minimum = min(minimum, float(np.min(np.linalg.norm(planned - centers, axis=1) - radii)))
        elapsed += segment.duration_s
    assert (minimum > 0.0) is clear
    assert motion_clear((first, second), obstacle) is clear
