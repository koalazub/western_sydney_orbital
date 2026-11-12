"""Numeric checks for Ali's MPPI core."""

from __future__ import annotations

import numpy as np

from wsorb.controllers._sources import load_math

core = load_math("ali_mppi")["core"]


def test_cruise_velocity_is_seven_metres_per_second_toward_the_target():
    velocity = core.cruise_velocity(np.zeros(3), np.array([100.0, 0.0, 0.0]))
    assert np.isclose(np.linalg.norm(velocity), 7.0)
    assert np.allclose(velocity, [7.0, 0.0, 0.0])


def test_burn_governor_respects_the_planner_and_avoids_micro_bursts():
    state = {"burning": False, "hold_until": 0.0, "last_accel": np.zeros(3)}
    acceleration = core.govern(np.array([0.5, 0.0, 0.0]), state, 0.5, 0.0)
    assert state["burning"] and np.isclose(np.linalg.norm(acceleration), 0.5)

    acceleration = core.govern(np.zeros(3), state, 0.5, 0.1)
    assert state["burning"] and np.isclose(np.linalg.norm(acceleration), 0.5)

    acceleration = core.govern(np.zeros(3), state, 0.5, 1.0)
    assert not state["burning"] and np.allclose(acceleration, np.zeros(3))

    state = {"burning": False, "hold_until": 0.0, "last_accel": np.zeros(3)}
    acceleration = core.govern(np.array([0.01, 0.0, 0.0]), state, 0.5, 0.0)
    assert not state["burning"]


def test_capsule_distance_uses_the_oriented_segment():
    distance = core.capsule_distance(
        np.zeros((1, 3)),
        np.array([[10.0, 0.0, 0.0]]),
        np.array([1.0, 0.0, 0.0]),
        3.0,
        1.0,
    )
    assert np.isclose(distance[0, 0], 6.0)


def test_attitude_is_bounded_and_damps_an_aligned_ship():
    aligned = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
    )
    assert np.allclose(aligned, np.zeros(3))

    turning = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
    )
    assert 0.0 < np.linalg.norm(turning) <= 0.05


def test_plan_returns_a_finite_bounded_first_control():
    state = {
        "rocks": [
            {
                "position": np.array([100.0, 0.0, 0.0]),
                "velocity": np.array([-5.0, 0.0, 0.0]),
                "diameter": 2.0,
                "max_diameter": 2.0,
                "last_seen": 0.0,
                "samples": [],
                "sigma": 0.5,
            }
        ],
        "last_control": np.zeros(3),
    }
    acceleration = core.plan(
        state,
        np.zeros(3),
        np.zeros(3),
        np.array([1000.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        3.0,
        0.5,
        0.1,
        0.0,
        0.0,
        3388.7,
        0.0,
    )
    assert np.isfinite(acceleration).all()
    assert np.linalg.norm(acceleration) <= 0.5 + 1e-9
