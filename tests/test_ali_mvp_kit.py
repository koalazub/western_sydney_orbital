"""Numeric checks for Ali's functional radar core, without an engine or SDK."""

from __future__ import annotations

import numpy as np

from wsorb.controllers._sources import load_math

kit = load_math("ali")["core"]


def radar_row(relative, diameter, measured_at, range_rate):
    return np.array(
        [
            list(relative),
            [float(np.linalg.norm(relative)), range_rate, diameter],
            [measured_at, 0.0, 0.0],
        ]
    )


def test_polyfit_tracker_associates_one_rock_and_keeps_its_largest_diameter():
    rocks = []
    for time in (0.0, 1.0, 2.0, 3.0):
        diameter = 4.0 if time == 2.0 else 2.0
        row = radar_row([10.0 * (time + 1.0), 0.0, 0.0], diameter, time, -10.0)
        rocks = kit.update_tracker(rocks, [row], np.zeros(3), time)
    assert len(rocks) == 1
    assert np.allclose(rocks[0]["velocity"], [10.0, 0.0, 0.0])
    assert rocks[0]["max_diameter"] == 4.0


def test_planner_steers_around_a_head_on_rock_and_leaves_a_clear_path_alone():
    head_on = [
        {
            "position": np.array([100.0, 0.0, 0.0]),
            "velocity": np.array([-10.0, 0.0, 0.0]),
            "diameter": 2.0,
            "max_diameter": 2.0,
            "last_seen": 0.0,
            "samples": [],
        }
    ]
    dodged, held = kit.choose_planner(np.array([17.0, 0.0, 0.0]), head_on, np.zeros(3), 0.0, None)
    assert not np.allclose(dodged, [17.0, 0.0, 0.0])
    assert np.isclose(np.linalg.norm(dodged), 17.0)
    assert held is not None

    clear = [{**head_on[0], "position": np.array([0.0, 100.0, 0.0]), "velocity": np.zeros(3)}]
    same, held = kit.choose_planner(np.array([17.0, 0.0, 0.0]), clear, np.zeros(3), 0.0, None)
    assert np.allclose(same, [17.0, 0.0, 0.0])
    assert held is None


def test_emergency_triggers_on_a_close_front_threat_only():
    velocity = np.array([17.0, 0.0, 0.0])
    safe, active = kit.choose_emergency(
        velocity, [radar_row([100.0, 0.0, 0.0], 2.0, 0.0, -17.0)], 0.0, velocity
    )
    assert active
    assert abs(safe[1]) + abs(safe[2]) > 1.0

    behind, active = kit.choose_emergency(
        velocity, [radar_row([-100.0, 0.0, 0.0], 2.0, 0.0, 17.0)], 0.0, velocity
    )
    assert not active
    assert np.allclose(behind, velocity)


def test_attitude_pivots_toward_a_reversed_thrust_direction_and_damps_when_aligned():
    alpha, angle = kit.attitude_acceleration(
        np.array([1.0, 0.0, 0.0]), np.zeros(3), np.array([-1.0, 0.0, 0.0])
    )
    assert angle > 3.0
    assert np.linalg.norm(alpha) > 0.0

    alpha, angle = kit.attitude_acceleration(
        np.array([1.0, 0.0, 0.0]), np.array([0.0, 0.0, 0.1]), np.array([1.0, 0.0, 0.0])
    )
    assert angle < 0.1
    assert alpha[2] < 0.0


def test_controller_caps_below_the_native_limit_and_goal_coasts_when_aligned():
    assert np.allclose(
        kit.controlled_acceleration(np.array([10.0, 0.0, 0.0]), np.zeros(3)),
        [0.5, 0.0, 0.0],
    )
    assert np.allclose(
        kit.controlled_acceleration(np.array([0.1, 0.0, 0.0]), np.zeros(3)),
        np.zeros(3),
    )
    coast = kit.goal_velocity(
        np.zeros(3), np.array([17.0, 0.0, 0.0]), np.array([1000.0, 10.0, 0.0])
    )
    assert np.allclose(coast, [17.0, 0.0, 0.0])
    steer = kit.goal_velocity(
        np.zeros(3), np.array([17.0, 0.0, 0.0]), np.array([1000.0, 100.0, 0.0])
    )
    assert steer[1] > 0.0
