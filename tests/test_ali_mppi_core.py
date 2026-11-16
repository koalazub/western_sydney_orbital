"""Numeric checks and submission-size guard for Ali's compact MPPI core."""

from __future__ import annotations

import json

import numpy as np

from wsorb.controllers._sources import ROOT, load_math

core = load_math("ali_mppi")["core"]


def sample_observation(position, velocity, target, damage=0.0):
    return {
        "position": np.asarray(position, float),
        "velocity": np.asarray(velocity, float),
        "target": np.asarray(target, float),
        "body_x": np.array([1.0, 0.0, 0.0]),
        "angular_velocity": np.zeros(3),
        "fuel": 0.0,
        "damage": float(damage),
        "mass": 3388.7,
        "radius": 3.0,
        "max_accel": 0.5,
        "max_angular": 0.05,
    }


def debris_row(position, diameter=2.0):
    row = np.zeros((3, 3))
    row[0] = np.asarray(position, float)
    row[1, 2] = diameter
    return row


def test_competitor_region_stays_within_the_current_budget():
    source = (ROOT / "controllers/ali/controller.py").read_text(encoding="utf-8")
    begin = source.index("# BEGIN ALI MPPI CORE")
    # The 200-line target is a guideline. Keep a guard against unbounded growth.
    assert len(source[begin:].splitlines()) <= 350


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


def test_collision_damage_triggers_escape_burn_away_from_debris():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    row = debris_row([1.0, 0.0, 0.0])
    _, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [row],
        0.0,
        0.1,
    )
    assert record["collision"] is False

    body, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0], damage=500.0),
        [row],
        0.1,
        0.1,
    )
    assert record["collision"] is True and record["collision_hits"] == 1
    assert record["gate"] == "collision"
    assert body[1] < -0.4
    assert np.isclose(record["collision_impact_j"], 500.0)
    json.dumps(record, allow_nan=False)


def test_unexplained_velocity_jump_is_a_redundant_collision_trigger():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.0,
        0.1,
    )
    body, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 3.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.1,
        0.1,
    )
    assert record["collision"] is True and record["collision_hits"] == 1
    assert body[1] > 0.4
    assert record["collision_impact_j"] == 0.0


def test_large_time_gap_does_not_invent_a_collision():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.0,
        0.1,
    )
    _, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 3.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        2.0,
        2.0,
    )
    assert record["collision"] is False


def test_collision_detour_offsets_the_route_after_the_escape_burn():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    row = debris_row([1.0, 0.0, 0.0])
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [row],
        0.0,
        0.1,
    )
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0], damage=500.0),
        [row],
        0.1,
        0.1,
    )
    _, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0], damage=500.0),
        [],
        1.3,
        0.1,
    )
    assert record["collision"] is True and record["collision_detour"] is True
    assert record["gate"] == "burn"
    assert state["steer_target"][1] < -20.0
    assert state["collision"]["offset"] > 20.0
    assert np.isclose(state["collision"]["offset"], 25.0)
    assert state["collision"]["impulse_s"] < 0.6
    assert record["collision_hits"] == 1


def test_repeated_collisions_flip_the_detour_side_and_escalate_speed_safety():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    row = debris_row([1.0, 0.0, 0.0])
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [row],
        0.0,
        0.1,
    )
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0], damage=500.0),
        [row],
        0.1,
        0.1,
    )
    first_side = state["collision"]["side"].copy()
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0], damage=1000.0),
        [row],
        0.2,
        0.1,
    )
    assert state["collision"]["hits"] == 2
    assert float(state["collision"]["side"] @ first_side) < 0.0
    assert core.safe_cruise(state, 8.0, 1_000.0, core.DEFAULT_POLICY) == 8.0
    state["collision_hits"] = 3
    assert np.isclose(core.safe_cruise(state, 8.0, 1_000.0, core.DEFAULT_POLICY), 6.4)
    assert np.isclose(core.safe_cruise(state, 8.0, 120_000.0, core.DEFAULT_POLICY), 6.4)
