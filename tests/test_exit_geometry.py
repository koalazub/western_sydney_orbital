"""Constructed three-dimensional geometry, not sensors or flight observations."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.config import PlannerConfig
from wsorb.flight.local_motion import (
    blocked_radar_stop_endpoints,
    initial_escape_side,
    stop_endpoint_margins,
    stopping_motion,
)
from wsorb.flight.temporal import TemporalObstacles, motion_clear


def scene(relative_centers, radii):
    position = np.array((2000.0, 0.0, 0.0))
    centers = np.vstack(
        (
            (0.0, 0.0, 0.0),
            (1e6, 0.0, 0.0),
            (10000.0, 0.0, 0.0),
            position + np.asarray(relative_centers),
        )
    )
    return position, TemporalObstacles(
        centers,
        np.zeros_like(centers),
        np.asarray((1000.0, 1000.0, 32.0, *radii)),
        np.zeros(len(centers)),
        np.full(len(centers), 0.25),
    )


def test_initial_exit_chooses_the_wider_real_3d_corridor_before_building_momentum():
    position, obstacles = scene(
        ((0.0, 100.0, 0.0), (0.0, -100.0, 0.0), (0.0, 0.0, -100.0)), (70.0, 70.0, 70.0)
    )
    side, gap = initial_escape_side(
        position,
        np.array((1.0, 0.0, 0.0)),
        np.array((0.0, 1.0, 0.0)),
        40.0,
        obstacles,
        PlannerConfig(),
    )
    np.testing.assert_allclose(side, (0.0, 0.0, 1.0), atol=1e-12)
    assert gap == pytest.approx(30.0)
    np.testing.assert_array_equal(position, (2000.0, 0.0, 0.0))
    assert len(obstacles.radii_m) == 6


def test_a_distant_inlet_body_is_not_projected_onto_the_departure_plane():
    position, obstacles = scene(((-1200.0, 400.0, 0.0),), (60.0,))
    side, gap = initial_escape_side(
        position,
        np.array((1.0, 0.0, 0.0)),
        np.array((0.0, 1.0, 0.0)),
        40.0,
        obstacles,
        PlannerConfig(),
    )
    np.testing.assert_array_equal(side, (0.0, 1.0, 0.0))
    assert gap > 1000.0
    assert not obstacles.radii_m.flags.writeable


def test_reported_stop_endpoint_margin_keeps_all_radius_growth():
    obstacles = TemporalObstacles(
        np.array(((80.0, 0.0, 0.0),)),
        np.zeros((1, 3)),
        np.array((10.0,)),
        np.array((12.16,)),
        np.array((0.25,)),
    )
    commands = np.array(((0.5, 0.0, 0.0), (-0.5, 0.0, 0.0)))
    margins = stop_endpoint_margins(
        np.zeros(3),
        np.array((2.0, 0.0, 0.0)),
        commands,
        1.0,
        0.75,
        0.49,
        0.05,
        obstacles,
        np.array((0.5, 0.5)),
    )
    assert margins[1] > margins[0]
    for index, acceleration in enumerate(commands):
        end_speed = 2.0 + acceleration[0] * 0.75
        horizon = 0.75 + (end_speed + 0.05) / 0.49
        endpoint = 2.0 * 0.75 + 0.5 * acceleration[0] * 0.75**2 + end_speed**2 / (2 * 0.49)
        expected = abs(80.0 - endpoint) - 10.0 - 12.16 * horizon - 0.25 * horizon**2 - 0.5
        assert margins[index] == pytest.approx(expected)


@pytest.mark.parametrize("radar_count", [1, 4096])
def test_batched_endpoint_filter_rejects_only_whole_stops_blocked_by_an_original_row(radar_count):
    centers = np.tile((10000.0, 10000.0, 10000.0), (radar_count + 3, 1))
    centers[-1] = (80.0, 0.0, 0.0)
    obstacles = TemporalObstacles(
        centers,
        np.zeros_like(centers),
        np.full(len(centers), 10.0),
        np.full(len(centers), 12.16),
        np.full(len(centers), 0.25),
    )
    commands = np.array(
        ((0.5, 0.0, 0.0), (-0.5, 0.0, 0.0), (0.0, 0.5, 0.0), (0.0, 0.0, 0.5), (-0.25, 0.25, 0.0))
    )
    position, velocity = np.zeros(3), np.array((2.0, 0.0, 0.0))
    blocked = blocked_radar_stop_endpoints(
        position,
        velocity,
        commands,
        1.0,
        0.75,
        0.49,
        0.05,
        obstacles,
        np.full(len(commands), 0.5),
        40.0,
    )
    assert tuple(commands[0]) in blocked
    assert tuple(commands[1]) not in blocked
    padded = TemporalObstacles(
        obstacles.centers_m,
        obstacles.velocities_mps,
        obstacles.radii_m + 0.5,
        obstacles.growth_mps,
        obstacles.growth_mps2,
    )
    for command in commands:
        if tuple(np.round(command, 14)) in blocked:
            assert not motion_clear(
                stopping_motion(
                    position,
                    velocity,
                    command,
                    0.75,
                    0.49,
                    0.05,
                ),
                padded,
            )


@pytest.mark.parametrize(
    "gain, controls, gap",
    [
        (1.0, ((1e-12, 0.0, 0.0),), 1.7500001),
        (10.0, ((1e-12, 0.0, 0.0), (1.004e-12, 0.0, 0.0)), None),
    ],
)
def test_endpoint_filter_preserves_first_keys_and_the_tiny_speed_cutoff(gain, controls, gap):
    position, velocity = np.zeros(3), np.zeros(3)
    commands = np.asarray(controls)
    first = stopping_motion(position, velocity, commands[0] * gain, 0.75, 1e-6, 0.0)
    last = first[-1]
    endpoint = np.asarray(last.position_m) + np.asarray(last.velocity_mps) * last.duration_s
    endpoint += 0.5 * np.asarray(last.acceleration_mps2) * last.duration_s**2
    horizon = sum(piece.duration_s for piece in first)
    growth = 1.0 if gap is not None else 12.0
    separation = gap if gap is not None else 1.0 + growth * horizon + 1e-7
    centers = np.tile((10000.0, 10000.0, 10000.0), (4, 1))
    centers[-1] = endpoint + np.array((separation, 0.0, 0.0))
    obstacles = TemporalObstacles(
        centers,
        np.zeros_like(centers),
        np.ones(4),
        np.full(4, growth),
        np.zeros(4),
    )
    assert motion_clear(first, obstacles)
    assert not blocked_radar_stop_endpoints(
        position,
        velocity,
        commands,
        gain,
        0.75,
        1e-6,
        0.0,
        obstacles,
        np.zeros(len(commands)),
        40.0,
    )
