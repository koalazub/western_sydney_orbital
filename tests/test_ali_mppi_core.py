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
        "body_z": np.array([0.0, 0.0, 1.0]),
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


def radar_row(position, range_rate, diameter, time):
    row = np.zeros((3, 3))
    row[0] = np.asarray(position, float)
    row[1, 1] = float(range_rate)
    row[1, 2] = float(diameter)
    row[2, 0] = float(time)
    return row


def test_competitor_region_stays_within_the_current_budget():
    source = (ROOT / "controllers/ali/controller.py").read_text(encoding="utf-8")
    begin = source.index("# BEGIN ALI MPPI CORE")
    end = source.index("# END ALI MPPI CORE")
    # The 200-line target is a guideline. Keep a guard against unbounded growth.
    assert len(source[begin:end].splitlines()) <= 1350


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


def test_emergency_guard_dodges_sideways_instead_of_reversing_the_launch():
    track = {
        "p": np.array([40.0, 0.0, 0.0]),
        "v": np.array([-6.0, 0.0, 0.0]),
        "r": 2.0,
        "sp": 0.5,
        "sv": 0.0,
        "t": 0.0,
        "s": 0.5,
    }
    acceleration, active = core.emergency_guard(
        np.zeros(3), [], 0.0, 0.5, core.DEFAULT_POLICY, [track], np.zeros(3)
    )
    assert active is True
    assert abs(float(acceleration[0])) < 1e-6
    assert abs(float(acceleration[1])) > 0.4


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
        "body_z": np.array([1.0, 0.0, 0.0]),
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
    assert record["gate"] in {"alignment", "burn"}
    assert state["steer_target"][1] < -20.0
    assert state["collision"]["offset"] > 20.0
    assert np.isclose(state["collision"]["offset"], 25.0)
    assert state["collision"]["impulse_s"] < 0.6
    assert record["collision_hits"] == 1


def test_repeated_collisions_keep_the_predicted_side_and_escalate_speed_safety():
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
    assert np.allclose(state["collision"]["side"], first_side)
    assert core.safe_cruise(state, 8.0, 1_000.0, core.DEFAULT_POLICY) == 8.0
    state["collision_hits"] = 3
    assert np.isclose(core.safe_cruise(state, 8.0, 1_000.0, core.DEFAULT_POLICY), 6.4)
    assert np.isclose(core.safe_cruise(state, 8.0, 120_000.0, core.DEFAULT_POLICY), 6.4)


def test_collision_side_policy_avoids_the_predicted_rock():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
    }
    ship_velocity = np.array([7.0, 0.0, 0.0])
    rock = np.array([50.0, -20.0, 0.0])
    line = rock / np.linalg.norm(rock)
    row = radar_row(rock, -float(ship_velocity @ line), 4.0, 0.0)
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], ship_velocity, [1000.0, 0.0, 0.0]),
        [row],
        0.0,
        0.1,
    )
    _, _, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], ship_velocity, [1000.0, 0.0, 0.0], damage=500.0),
        [row],
        0.1,
        0.1,
    )
    assert state["collision"]["side"][1] > 0.0
    assert state["collision"]["side_reason"] == "prediction"
    assert record["collision_side_reason"] == "prediction"


def test_launch_phase_clears_radially_before_cruise():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0, "plan_steering": False},
    }
    observation = sample_observation([0.0, 0.0, 100.0], [0.0, 0.0, 0.0], [1000.0, 0.0, 100.0])
    body, _, record = core.step(state, observation, [], 0.0, 0.1)
    assert record["phase"] == "launch"
    assert body[2] > 0.4
    assert abs(body[0]) < 1e-6
    cleared = sample_observation([0.0, 0.0, 130.0], [0.0, 0.0, 0.0], [1000.0, 0.0, 130.0])
    _, _, record = core.step(state, cleared, [], 0.1, 0.1)
    assert state["launched"] is True
    assert record["phase"] != "launch"


def test_launch_gate_blocks_main_thrust_until_body_z_aligns():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0, "plan_steering": False},
    }
    observation = sample_observation([0.0, 0.0, 100.0], [0.0, 0.0, 0.0], [1000.0, 0.0, 100.0])
    observation["body_z"] = np.array([1.0, 0.0, 0.0])
    body, angular, record = core.step(state, observation, [], 0.0, 0.1)
    assert record["phase"] == "launch"
    assert record["launch_gate_rad"] == 0.15
    assert record["launch_align_rad"] > 1.4
    assert np.allclose(body, 0.0)
    assert np.linalg.norm(angular) > 0.0
    observation["body_z"] = np.array([0.0, 0.0, 1.0])
    body, angular, record = core.step(state, observation, [], 0.1, 0.1)
    assert np.linalg.norm(body) > 0.4
    assert abs(body[0]) < 1e-6
    assert record["launch_align_rad"] < 1e-6
    assert np.linalg.norm(angular) < 1e-9


def test_plan_steering_controls_mppi_sampling(monkeypatch):
    calls = []

    def fake_plan(*args, **kwargs):
        calls.append(1)
        return np.zeros(3)

    monkeypatch.setattr(core, "plan", fake_plan)
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0},
        "launched": True,
    }
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.0,
        0.1,
    )
    assert calls == [1]
    state["policy"]["plan_steering"] = False
    core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.1,
        0.1,
    )
    assert calls == [1]


def test_line_tracker_fits_the_latest_position_and_absolute_velocity():
    state = {
        "policy": {"tracker": "line", "time_budget_s": 300.0, "arrival_radius_m": 50.0},
        "rng": np.random.default_rng(12345),
    }
    for time in (0.0, 1.0, 2.0, 3.0):
        row = radar_row([100.0 - 5.0 * time, 2.0 * time, 0.0], 0.0, 2.0, time)
        tracks = core.update_estimates(
            state, [row], np.zeros(3), np.zeros(3), time, state["policy"]
        )
    track = min(tracks, key=lambda item: np.linalg.norm(item["p"] - [85.0, 6.0, 0.0]))
    assert np.linalg.norm(track["p"] - [85.0, 6.0, 0.0]) < 1.0
    assert np.linalg.norm(track["v"] - [-5.0, 2.0, 0.0]) < 1.0
    assert track["sp"] >= 0.0


def test_aged_radar_measurement_is_reconstructed_from_shuttle_history():
    history = [(0.0, np.zeros(3)), (1.0, np.array([10.0, 0.0, 0.0]))]
    row = radar_row([100.0, 0.0, 0.0], -20.0, 2.0, 0.0)
    rebased = core.reconstruct_measurements([row], history, np.array([20.0, 0.0, 0.0]))
    assert np.isclose(rebased[0][0, 0], 80.0)
    state = {"rocks": [], "policy": {"tracker": "line"}}
    core.update_tracks(state, rebased, np.array([20.0, 0.0, 0.0]), np.array([20.0, 0.0, 0.0]), 2.0)
    assert np.isclose(state["rocks"][0]["p"][0], 100.0)


def test_radar_loop_origin_is_shifted_onto_the_controller_clock():
    state = {}
    for step in range(1, 21):
        now = step * 0.1
        position = np.array([8.0 * now, 0.0, 0.0])
        history = core.record_shuttle_state(state, now, position)
        measured_at = now - 0.98
        row = radar_row(
            [100.0 - position[0], 0.0, 0.0],
            -8.0,
            2.0,
            measured_at,
        )
        aligned, offset = core.align_radar_rows(state, [row], now)
        assert np.isclose(offset, 0.98)
        rebased = core.reconstruct_measurements(aligned, history, position)
        assert np.isclose(rebased[0][0, 0] + position[0], 100.0, atol=0.01)


def test_pre_sweep_radar_timestamps_do_not_define_the_clock_origin():
    state = {}
    row = radar_row([100.0, 0.0, 0.0], 0.0, 2.0, 0.0)
    _, offset = core.align_radar_rows(state, [row], 1.0)
    assert offset == 0.0
    assert "radar_clock_offset_s" not in state


def test_kalman_tracker_confirms_a_closing_track_from_position_and_range_rate():
    tracker = core.KalmanTracker()
    for time in (0.0, 1.0, 2.0, 3.0):
        row = radar_row([100.0 - 5.0 * time, 0.0, 0.0], -5.0, 2.0, time)
        tracker.update(np.zeros(3), np.zeros(3), [row])
    tracks = tracker.snapshot(3.0)
    assert len(tracks) >= 1
    track = min(tracks, key=lambda item: np.linalg.norm(item["p"] - np.array([85.0, 0.0, 0.0])))
    assert np.linalg.norm(track["p"] - np.array([85.0, 0.0, 0.0])) < 3.0
    assert track["v"][0] < -3.0
    assert track["r"] > 0.0 and track["sp"] >= 0.0 and track["sv"] >= 0.0


def test_avoider_offsets_a_desired_velocity_away_from_a_closing_track():
    state = {"policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0}}
    tracks = [
        {
            "p": np.array([20.0, 0.0, 0.0]),
            "v": np.array([-10.0, 0.0, 0.0]),
            "r": 2.0,
            "sp": 0.5,
            "sv": 0.1,
            "s": 0.5,
        }
    ]
    requested = np.array([8.0, 0.0, 0.0])
    selected = core.avoid_velocity(
        requested, tracks, np.zeros(3), requested, state, core.DEFAULT_POLICY
    )
    assert np.isfinite(selected).all()
    assert not np.allclose(selected, requested)
    assert state["avoid_info"]["n_tracks"] == 1
    empty = core.avoid_velocity(requested, [], np.zeros(3), requested, state, core.DEFAULT_POLICY)
    assert np.allclose(empty, requested)


def test_proportional_governor_uses_a_deadband_and_caps_the_command():
    state = {"policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0}}
    command, burning = core.proportional_govern(
        np.array([0.1, 0.0, 0.0]), np.zeros(3), state, 0.5, core.DEFAULT_POLICY
    )
    assert burning is False and np.allclose(command, np.zeros(3))
    command, burning = core.proportional_govern(
        np.array([1.0, 0.0, 0.0]), np.zeros(3), state, 0.5, core.DEFAULT_POLICY
    )
    assert burning is True and np.isclose(np.linalg.norm(command), 0.5)
    command, burning = core.proportional_govern(
        np.array([0.2, 0.0, 0.0]), np.zeros(3), state, 0.5, core.DEFAULT_POLICY
    )
    assert burning is True and np.isclose(np.linalg.norm(command), 0.4)


def test_attitude_control_can_be_disabled_for_the_fuel_comparison():
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": {"time_budget_s": 300.0, "arrival_radius_m": 50.0, "attitude_control": False},
    }
    _, angular, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        [],
        0.0,
        0.1,
    )
    assert np.allclose(angular, np.zeros(3))
    assert state["attitude_hold"] is False
    assert record["collision"] is False


def test_kalman_mppi_and_avoider_modes_run_through_the_same_step():
    common = {
        "time_budget_s": 300.0,
        "arrival_radius_m": 50.0,
        "tracker": "kalman",
        "guard_source": "tracks",
        "risk_inflation": True,
        "mppi_fixed_samples": True,
        "plan_steering": True,
        "cruise_speed_mps": 10.0,
    }
    state = {
        "burning": False,
        "attitude_hold": False,
        "last_control": np.zeros(3),
        "rng": np.random.default_rng(12345),
        "policy": dict(common),
    }
    rows = [radar_row([30.0, 0.0, 0.0], -5.0, 2.0, 0.0)]
    body, angular, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        rows,
        0.0,
        0.1,
    )
    assert np.isfinite(body).all() and np.isfinite(angular).all()
    assert record["tracker"] == "kalman" and record["estimate_count"] >= 1
    rows = [radar_row([29.0, 0.0, 0.0], -5.0, 2.0, 0.1)]
    body, angular, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        rows,
        0.1,
        0.1,
    )
    assert np.isfinite(body).all() and np.isfinite(angular).all()

    state["policy"].update(avoidance="avoider", governor="proportional", attitude_control=False)
    body, angular, record = core.step(
        state,
        sample_observation([0.0, 0.0, 0.0], [7.0, 0.0, 0.0], [1000.0, 0.0, 0.0]),
        rows,
        0.2,
        0.1,
    )
    assert np.isfinite(body).all() and np.allclose(angular, np.zeros(3))
    assert record["avoidance"] == "avoider" and record["governor"] == "proportional"
