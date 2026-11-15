"""Numeric checks and submission-size guard for Ali's compact MPPI core."""

from __future__ import annotations

import json

import numpy as np

from wsorb.controllers._sources import ROOT, load_math

core = load_math("ali_mppi")["core"]


def test_competitor_region_stays_within_the_current_budget():
    source = (ROOT / "examples/ali/controller_mppi.py").read_text(encoding="utf-8")
    begin = source.index("# BEGIN ALI MPPI CORE")
    # Temporary allowance while the policy knobs are parameterized; optimize later.
    assert len(source[begin:].splitlines()) <= 250


def test_cruise_velocity_points_at_the_target():
    velocity = core.cruise_velocity(np.zeros(3), np.array([100.0, 0.0, 0.0]))
    assert np.allclose(velocity, [7.0, 0.0, 0.0])

    inside = core.cruise_velocity(np.zeros(3), np.array([49.0, 0.0, 0.0]))
    assert np.allclose(inside, [7.0, 0.0, 0.0])


def test_burn_governor_uses_hysteresis():
    nominal = np.array([7.0, 0.0, 0.0])
    acceleration, burning = core.govern(np.zeros(3), nominal, np.zeros(3), False, 0.5)
    assert burning and np.isclose(np.linalg.norm(acceleration), 0.5)

    acceleration, burning = core.govern(np.zeros(3), nominal, np.array([6.9, 0.0, 0.0]), True, 0.5)
    assert not burning and np.allclose(acceleration, np.zeros(3))

    acceleration, burning = core.govern(np.array([0.5, 0.0, 0.0]), nominal, nominal, True, 0.5)
    assert burning
    assert np.allclose(acceleration, [0.5, 0.0, 0.0])

    acceleration, burning = core.govern(np.array([0.1, 0.0, 0.0]), nominal, nominal, True, 0.5)
    assert not burning
    assert np.allclose(acceleration, np.zeros(3))


def test_emergency_guard_reacts_to_a_closing_rock():
    row = np.zeros((3, 3))
    row[0] = [20.0, 0.0, 0.0]
    row[1] = [15.0, -5.0, 2.0]
    row[2, 0] = 0.0
    acceleration, active = core.emergency_guard(np.array([7.0, 0.0, 0.0]), [row], 1.0, 0.5)
    assert active is True
    assert np.linalg.norm(acceleration) <= 0.5


def test_plan_returns_a_finite_bounded_first_control():
    state = {
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
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
        [],
    )
    assert np.isfinite(acceleration).all()
    assert np.linalg.norm(acceleration) <= 0.5 + 1e-9
    assert 0.0 < state["cruise_speed_mps"] < 36.0
    assert state["fuel_rate_kg_s"] > 0.0


def test_attitude_holds_saturates_and_releases():
    aligned_state = {}
    aligned = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
        aligned_state,
    )
    assert np.allclose(aligned, np.zeros(3))
    assert aligned_state["attitude_hold"] is True

    turning_state = {}
    trace = {}
    turning = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        turning_state,
        trace,
    )
    assert 0.0 < np.linalg.norm(turning) <= 0.05
    assert turning_state["attitude_hold"] is False
    assert trace["saturated"] is True
    assert trace["angle_deg"] == 90.0

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


def test_attitude_damping_uses_the_off_axis_rate():
    trace = {}
    state = {}
    damped = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, -0.07, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
        state,
        trace,
    )
    assert trace["rate_error_rad_s"] < 0.0
    assert np.allclose(damped, [0.0, 0.05, 0.0])


def test_telemetry_is_finite_json_and_flags_a_coast_warning():
    trace = {}
    core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 0.03, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        {},
        trace,
    )
    record = core.telemetry(
        1.0,
        "coast",
        "off",
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.zeros(3),
        np.zeros(3),
        trace,
        1.0,
        6.0,
        100.0,
        2,
        1,
        False,
    )
    assert record["schema"] == "wsorb.mppi_attitude"
    assert record["line"].startswith("MPPI")
    assert record["burn_align_rad"] is None
    assert record["warning"] == "coast_misaligned_with_residual_rate"
    json.dumps(record, allow_nan=False)


def test_terminal_coast_command_is_zero_inside_the_goal_band():
    state = {
        "burning": True,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    observation = {
        "position": np.array([1000.0, 0.0, 0.0]),
        "velocity": np.array([5.0, 0.0, 0.0]),
        "target": np.array([1200.0, 0.0, 0.0]),
        "body_x": np.array([1.0, 0.0, 0.0]),
        "angular_velocity": np.zeros(3),
        "fuel": 0.0,
        "damage": 0.0,
        "mass": 3388.7,
        "radius": 3.0,
        "max_accel": 0.5,
        "max_angular": 0.05,
    }
    body, angular, record = core.step(state, observation, [], 1.0, 0.1)
    assert np.allclose(body, np.zeros(3))
    assert np.allclose(angular, np.zeros(3))
    assert state["burning"] is False
    assert record["mode"] == "coast"
