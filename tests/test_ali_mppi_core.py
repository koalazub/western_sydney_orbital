"""Numeric checks for Ali's MPPI core."""

from __future__ import annotations

import json

import numpy as np

from wsorb.controllers._sources import load_math

core = load_math("ali_mppi")["core"]


def test_cruise_velocity_is_seven_metres_per_second_toward_the_target():
    velocity = core.cruise_velocity(np.zeros(3), np.array([100.0, 0.0, 0.0]))
    assert np.isclose(np.linalg.norm(velocity), 7.0)
    assert np.allclose(velocity, [7.0, 0.0, 0.0])


def test_burn_governor_uses_hysteresis_instead_of_micro_bursts():
    nominal = np.array([7.0, 0.0, 0.0])
    acceleration, burning = core.govern(np.zeros(3), nominal, np.zeros(3), False, 0.5)
    assert burning and np.isclose(np.linalg.norm(acceleration), 0.5)

    acceleration, burning = core.govern(np.zeros(3), nominal, np.array([6.9, 0.0, 0.0]), True, 0.5)
    assert not burning and np.allclose(acceleration, np.zeros(3))


def test_capsule_distance_uses_the_oriented_segment():
    distance = core.capsule_distance(
        np.zeros((1, 3)),
        np.array([[10.0, 0.0, 0.0]]),
        np.array([1.0, 0.0, 0.0]),
        3.0,
        1.0,
    )
    assert np.isclose(distance[0, 0], 6.0)


def test_attitude_is_bounded_and_enters_hold_when_aligned():
    state = {"attitude_hold": False}
    aligned = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
        state,
    )
    assert np.allclose(aligned, np.zeros(3))
    assert state["attitude_hold"] is True

    turning_state = {"attitude_hold": False}
    turning = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        turning_state,
    )
    assert 0.0 < np.linalg.norm(turning) <= 0.05
    assert turning_state["attitude_hold"] is False


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


def test_attitude_diagnostics_show_the_braking_curve_and_saturation():
    trace = {}
    state = {"attitude_hold": False}
    command = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        state,
        trace,
    )
    assert trace["angle_deg"] == 90.0
    assert trace["desired_rate_rad_s"] > 0.0
    assert trace["rate_error_rad_s"] > 0.0
    assert trace["hold"] is False
    assert trace["saturated"] is True
    assert np.allclose(trace["command_rad_s2"], command)

    damping = {}
    damping_state = {"attitude_hold": False}
    damped = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, -0.07, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
        damping_state,
        damping,
    )
    assert damping["hold"] is False
    assert damping["desired_rate_rad_s"] == 0.0
    assert damping["rate_error_rad_s"] < 0.0
    assert np.allclose(damped, [0.0, 0.05, 0.0])

    release_state = {"attitude_hold": True}
    release_trace = {}
    released = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([np.cos(np.radians(5.0)), np.sin(np.radians(5.0)), 0.0]),
        0.05,
        0.1,
        release_state,
        release_trace,
    )
    assert release_state["attitude_hold"] is False
    assert release_trace["released"] is True
    assert np.linalg.norm(released) > 0.0


def test_attitude_telemetry_is_finite_json_and_flags_coast_drift():
    trace = {}
    core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        {"attitude_hold": False},
        trace,
    )
    record = core.telemetry_record(
        1.0,
        0.1,
        "coast",
        "off",
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 0.0, 0.0]),
        np.array([0.0, 0.0, 0.0]),
        np.zeros(3),
        trace,
        1.0,
        6.0,
        100.0,
        2,
        1.0,
        0.0,
    )
    assert record["schema"] == "wsorb.mppi_attitude"
    assert record["line"].startswith("MPPI")
    assert record["burn_align_rad"] is None
    assert record["warning"] is None or isinstance(record["warning"], str)
    json.dumps(record, allow_nan=False)
    assert (
        core.attitude_warning(
            {
                "mode": "coast",
                "target_align_rad": np.radians(8.0),
                "rate_rad_s": 0.03,
            }
        )
        == "coast_misaligned_with_residual_rate"
    )
