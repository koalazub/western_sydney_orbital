"""Pure input validation and idle lifecycle, without a substitute simulation.

These tests make no claims about a live worker. Browser playback, manual
interruption and command delivery require the owner's actual browser check.
"""

from __future__ import annotations

import secrets
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import numpy as np
import pytest

from wsorb.experiment import (
    HISTORY_LIMIT,
    Decision,
    ExperimentRunner,
    FlightStateError,
    LivePolicy,
    RunLimits,
    _guidance_key,
    _number,
    controller_interruption,
    controller_start_notice,
    flight_context,
    guidance_due,
    lookahead_target,
    measured_sample,
    next_waypoint,
    observed_setup,
    retime_run,
    run_limit_notice,
    terminal_mission_notice,
)
from wsorb.flight import PlannerConfig, basic_command, coast_command, plan_route, predict_obstacles
from wsorb.local_api import (
    MAX_DEBRIS_COUNT,
    LocalApiError,
    LocalSimulation,
    StaleContextError,
    control_context,
    placement_readback_matches,
    validate_experiment_settings,
    validate_position,
)


def test_decision_copies_body_frame_vectors_without_clamping_or_sending():
    accel = np.array([0.0, 1.0, 2.0], dtype=np.float32)
    angular = [0.0, 0.0, 0.1]
    decision = Decision(accel, angular)
    accel[1] = 99
    angular[2] = 99
    np.testing.assert_array_equal(decision.accel, [0.0, 1.0, 2.0])
    np.testing.assert_array_equal(decision.angular_accel, [0.0, 0.0, 0.1])
    assert decision.accel.dtype == np.float64
    assert decision.angular_accel.dtype == np.float64
    assert not decision.accel.flags.writeable
    assert not decision.angular_accel.flags.writeable
    assert decision.preview is None
    with pytest.raises(FrozenInstanceError):
        decision.accel = np.zeros(3)


@pytest.mark.parametrize("name", ["accel", "angular_accel"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        [],
        [0, 0],
        [[0, 0, 0]],
        [[0], [0, 0], [0]],
        [np.nan, 0, 0],
        [0, np.inf, 0],
        [0, 0, -np.inf],
        [0, 0, 1j],
        [True, False, True],
        ["0", "0", "0"],
        [object(), 0, 0],
        [10**400, 0, 0],
    ],
)
def test_decision_rejects_non_finite_or_non_vector_commands(name, value):
    arguments = {"accel": [0, 0, 0], "angular_accel": [0, 0, 0], name: value}
    with pytest.raises(ValueError, match=f"{name} must be a finite real vector"):
        Decision(**arguments)


def test_preview_is_copied_without_inventing_a_render_schema():
    packet = {"waypoints": [[1.0, 2.0, 3.0]]}
    decision = Decision([0, 0, 0], [0, 0, 0], packet)
    packet["waypoints"][0][0] = 99
    assert decision.preview == {"waypoints": [[1.0, 2.0, 3.0]]}


@pytest.mark.parametrize("value", [[], "preview", 1, False])
def test_preview_must_be_an_optional_mapping(value):
    with pytest.raises(ValueError, match="preview must be a mapping or None"):
        Decision([0, 0, 0], [0, 0, 0], value)


def test_idle_status_and_stop_have_no_connection_or_measurements():
    runner = ExperimentRunner()
    expected = {
        "state": "idle",
        "sample_count": 0,
        "elapsed_sim_s": 0.0,
        "latest": None,
        "error": None,
    }
    assert runner.status() == expected
    assert runner.stop(timeout_s=0) == expected
    assert runner.stop() == expected
    report = runner.status()
    report.update(state="running", sample_count=10, latest={"not": "a measurement"})
    assert runner.status() == expected


@pytest.mark.parametrize("value", [True, np.bool_(False), "1", None, [], 1j, np.nan, np.inf])
def test_parameters_require_finite_real_numbers(value):
    with pytest.raises(ValueError, match="cadence_hz must be a finite real number"):
        _number(value, "cadence_hz")


@pytest.mark.parametrize("value", [0.1, 1, np.float64(10), np.int64(300)])
def test_parameter_numbers_accept_python_and_numpy_reals(value):
    assert _number(value, "parameter") == float(value)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("duration_s", 0),
        ("duration_s", -1),
        ("duration_s", 300.01),
        ("duration_s", np.nan),
        ("duration_s", np.inf),
        ("duration_s", True),
        ("duration_s", "30"),
        ("cadence_hz", 0),
        ("cadence_hz", 0.099),
        ("cadence_hz", 10.01),
        ("cadence_hz", np.nan),
        ("cadence_hz", np.inf),
        ("cadence_hz", False),
        ("cadence_hz", "1"),
    ],
)
def test_start_rejects_invalid_limits_before_any_connection(parameter, value):
    # This is the actual client, constructed without connecting. No valid run is
    # started and no browser/network response is replaced or invented.
    sim = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    runner = ExperimentRunner()
    with pytest.raises(ValueError, match=parameter):
        runner.start(sim, lambda observation: Decision([0, 0, 0], [0, 0, 0]), **{parameter: value})
    assert runner.status()["state"] == "idle"
    assert runner.status()["latest"] is None


@pytest.fixture
def numeric_history_contract():
    """Serialization inputs only. This is not a backend or physics evidence."""
    return {
        "observation": {
            "time_s": 2.25,
            "seed": 12,
            "frame": "numeric-contract-frame",
            "radar_clock": "numeric-contract-clock",
            "manual_interventions": 0,
            "max_accel_mps2": 0.5,
            "shuttle": {"mass_kg": 1000.0, "commanded_accel_mps2": [0.1, 0.2, 0.0]},
        },
        "telemetry": {
            "source_time_s": 2.0,
            "epoch": {"index": 1, "seed": 12, "started_at_time_s": 0.0},
            "status": "partial",
            "metrics": {"speed_mps": 3.0, "damage_j_estimate": None},
        },
    }


def test_history_projection_keeps_independent_times_and_missing_values(numeric_history_contract):
    row = measured_sample(numeric_history_contract)
    assert row["telemetry_time_s"] == 2.0
    assert row["observation_time_s"] == 2.25
    assert row["telemetry_epoch"] == 1
    assert row["telemetry_clock"] == row["observation_clock"]
    assert row["speed_mps"] == 3.0
    assert row["damage_j_estimate"] is None
    assert row["fuel_used_kg_estimate"] is None
    assert row["ship_mass_kg"] == 1000.0
    assert row["command_accel_body_x_mps2"] == 0.1
    assert row["command_angular_accel_body_x_rad_s2"] is None
    assert row["ship_inertia_xx_kg_m2"] is None


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "2", -1])
def test_history_needs_an_actual_finite_telemetry_time(numeric_history_contract, value):
    numeric_history_contract["telemetry"]["source_time_s"] = value
    assert measured_sample(numeric_history_contract) is None


def test_missing_report_or_epoch_is_not_a_measured_zero(numeric_history_contract):
    assert measured_sample({}) is None
    numeric_history_contract["telemetry"]["epoch"]["seed"] = None
    assert measured_sample(numeric_history_contract) is None


def test_nonfinite_history_metrics_are_unavailable(numeric_history_contract):
    numeric_history_contract["telemetry"]["metrics"].update(
        speed_mps=float("nan"), radial_climb_mps=float("inf"), thrust_force_n=True
    )
    row = measured_sample(numeric_history_contract)
    assert row["speed_mps"] is None
    assert row["radial_climb_mps"] is None
    assert row["thrust_force_n"] is None


def test_repeated_held_telemetry_replaces_instead_of_becoming_new_measurements(
    numeric_history_contract,
):
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    numeric_history_contract["observation"]["time_s"] = 2.5
    runner.record_snapshot(numeric_history_contract)
    assert len(runner.history()) == 1
    assert runner.history()[0]["telemetry_time_s"] == 2.0
    assert runner.history()[0]["observation_time_s"] == 2.5
    assert runner.status()["sample_count"] == 2


def test_history_splits_epochs_and_marks_same_time_manual_intervention(numeric_history_contract):
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    first_segment = runner.history()[0]["segment"]
    numeric_history_contract["observation"]["manual_interventions"] = 1
    runner.record_snapshot(numeric_history_contract, source="upright")
    runner.record_snapshot(numeric_history_contract)
    intervention = runner.history()[0]
    assert intervention["manual_intervention"] is True
    assert intervention["manual_intervention_source"] == "upright"
    assert intervention["segment"] > first_segment
    numeric_history_contract["telemetry"]["epoch"]["index"] = 2
    numeric_history_contract["telemetry"]["source_time_s"] = 0.0
    numeric_history_contract["observation"]["time_s"] = 0.0
    runner.record_snapshot(numeric_history_contract, source="reset")
    rows = runner.history()
    assert len(rows) == 2
    assert rows[0]["telemetry_time_s"] == 2.0
    assert rows[1]["telemetry_time_s"] == 0.0
    assert rows[0]["segment"] != rows[1]["segment"]


def test_history_is_bounded_and_returns_independent_copies(numeric_history_contract):
    runner = ExperimentRunner()
    assert runner.history() == []
    for index in range(HISTORY_LIMIT + 2):
        numeric_history_contract["telemetry"]["source_time_s"] = float(index)
        numeric_history_contract["observation"]["time_s"] = float(index)
        runner.record_snapshot(numeric_history_contract)
    rows = runner.history()
    assert len(rows) == HISTORY_LIMIT == 2000
    assert rows[0]["telemetry_time_s"] == 2.0
    rows[0]["speed_mps"] = 999
    rows.clear()
    assert runner.history()[0]["speed_mps"] == 3.0
    numeric_history_contract["telemetry"]["metrics"]["speed_mps"] = 99
    assert runner.status()["latest"]["telemetry"]["metrics"]["speed_mps"] == 3.0


def test_start_requires_the_real_local_client_and_a_callback():
    runner = ExperimentRunner()
    with pytest.raises(TypeError, match="sim must be a LocalSimulation"):
        runner.start(object(), lambda observation: Decision([0, 0, 0], [0, 0, 0]))
    sim = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(TypeError, match="policy must be callable"):
        runner.start(sim, None)
    assert runner.status()["state"] == "idle"


@pytest.mark.parametrize("value", [-1, np.nan, np.inf, True, "10", None, threading.TIMEOUT_MAX * 2])
def test_stop_rejects_invalid_join_timeout_while_idle(value):
    runner = ExperimentRunner()
    with pytest.raises(ValueError, match="timeout_s"):
        runner.stop(value)
    assert runner.status()["state"] == "idle"


@pytest.fixture
def numeric_guidance_input():
    """Constructed algorithm input, not a backend response or observed flight."""
    return {
        "time_s": 10.0,
        "seed": 100,
        "frame": "numeric-world",
        "rotation_convention": "active_xyzw",
        "radar_clock": "numeric-clock",
        "manual_interventions": 0,
        "tuning_revision": 0,
        "mission": {"status": "running", "phase": "transit"},
        "environment": {"mars_position_m": [0, 1e6, 0], "mars_radius_m": 1000.0},
        "geometry": {
            "phobos_enclosing_radius_m": 100.0,
            "arrival_radius_m": 50.0,
            "debris_count": 500,
        },
        "shuttle": {
            "position": [1000.0, 0.0, 0.0],
            "velocity": [0.0, 0.0, 0.0],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "radius_m": 4.0,
        },
        "target": {"position": [1500.0, 0.0, 0.0], "radius_m": 20.0},
        "radar": [[[0.0, 300.0, 0.0], [299.0, -2.0, 2.0], [9.0, 0.0, 0.0]]],
        "max_accel_mps2": 0.5,
        "max_angular_accel_rad_s2": 0.05,
    }


def editable_numeric_command(observation, route, mode, gains):
    """Actual pure flight calculations, without a client or simulated response."""
    if mode == "coast":
        return coast_command(observation)
    goal = next_waypoint(observation, route, gains["waypoint_arrival_m"])
    return basic_command(
        observation,
        goal_world_m=goal,
        position_gain_s2=gains["position_gain_s2"],
        velocity_gain_s=0.0,
    )


@pytest.fixture
def numeric_live_policy():
    return LivePolicy(
        predict_obstacles,
        plan_route,
        editable_numeric_command,
        PlannerConfig(),
        gains={"waypoint_arrival_m": 20.0, "position_gain_s2": 0.0012},
        config={"planner_label": "numeric algorithm contract"},
    )


def test_live_policy_uses_the_supplied_real_python_algorithms_and_current_gains(
    numeric_guidance_input, numeric_live_policy
):
    route = numeric_live_policy.plan(deepcopy(numeric_guidance_input))
    assert route.status == "candidate"
    assert route.to_preview()["points_world_m"] == route.points_world_m.tolist()
    first = numeric_live_policy.decision(numeric_guidance_input, route)
    edited = replace(
        numeric_live_policy,
        gains={"waypoint_arrival_m": 20.0, "position_gain_s2": 0.0006},
    )
    second = edited.decision(numeric_guidance_input, route)
    assert not np.array_equal(first.accel, second.accel)
    assert edited.predict_scene is predict_obstacles
    assert edited.find_path is plan_route
    assert edited.decide_command is editable_numeric_command
    assert numeric_live_policy.gains["position_gain_s2"] == 0.0012

    def current_coast_selection(observation, route, mode, gains):
        return coast_command(observation)

    replaced_function = replace(numeric_live_policy, decide_command=current_coast_selection)
    np.testing.assert_array_equal(
        replaced_function.decision(numeric_guidance_input, route).accel, np.zeros(3)
    )


def test_live_policy_copies_configuration_instead_of_retaining_mutable_widget_dicts():
    gains = {"waypoint_arrival_m": 20.0, "position_gain_s2": 0.0012}
    config = {"nested": {"value": 1}}
    policy = LivePolicy(
        predict_obstacles,
        plan_route,
        editable_numeric_command,
        PlannerConfig(),
        gains=gains,
        config=config,
    )
    gains["position_gain_s2"] = 99
    config["nested"]["value"] = 99
    assert policy.gains["position_gain_s2"] == 0.0012
    assert policy.config == {"nested": {"value": 1}}


def test_worker_policy_acceptance_is_distinct_from_request_and_does_not_invent_readings(
    numeric_guidance_input, numeric_live_policy
):
    runner = ExperimentRunner()
    # Exercise the worker's pure acceptance boundary. No fake thread/client is
    # installed, and no lifecycle or network operation is replaced.
    runner._policy = numeric_live_policy
    runner._policy_revision = 1
    assert runner.status().get("accepted_policy_revision") is None
    accepted, revision = runner._accept_policy(numeric_guidance_input)
    assert (accepted, revision) == (numeric_live_policy, 1)
    assert runner.status()["accepted_config"]["controller"] == "planned_waypoint_pd"
    edited = replace(numeric_live_policy, controller="coast")
    runner._policy = edited
    runner._policy_revision = 2
    assert runner.status()["controller"] == "planned_waypoint_pd"
    runner._accept_policy(numeric_guidance_input)
    assert runner.status()["controller"] == "coast"
    runner._accept_policy(numeric_guidance_input)
    changes = runner.configuration_changes()
    assert [change["policy_revision"] for change in changes] == [1, 2]
    assert all(change["source_time_s"] == 10.0 for change in changes)
    assert all(change["source_seed"] == 100 for change in changes)
    changes[-1]["config"]["controller"] = "changed copy"
    assert runner.status()["controller"] == "coast"
    assert runner.status()["sample_count"] == 0
    assert runner.history() == []
    assert runner._worker is None


def test_idle_live_updates_and_run_requests_cannot_start_a_worker(numeric_live_policy):
    runner = ExperimentRunner()
    with pytest.raises(RuntimeError, match="Connect"):
        runner.update_policy(numeric_live_policy)
    with pytest.raises(RuntimeError, match="Connect"):
        runner.run_controller()
    with pytest.raises(TypeError, match="LivePolicy"):
        runner.update_policy(None)
    assert runner.status()["state"] == "idle"
    assert runner._worker is None


@pytest.mark.parametrize(
    ("parameter", "value"),
    [("duration_s", 0), ("duration_s", 301), ("cadence_hz", 0), ("cadence_hz", 11)],
)
def test_run_current_policy_validates_limits_without_a_connection(parameter, value):
    runner = ExperimentRunner()
    with pytest.raises(ValueError, match=parameter):
        runner.run_controller(**{parameter: value})
    assert runner._worker is None


def test_watch_rejects_wrong_client_and_policy_without_connecting(numeric_live_policy):
    runner = ExperimentRunner()
    with pytest.raises(TypeError, match="LocalSimulation"):
        runner.watch(object(), numeric_live_policy)
    sim = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(TypeError, match="LivePolicy"):
        runner.watch(sim, None)
    with pytest.raises(ValueError, match="poll_hz"):
        runner.watch(sim, numeric_live_policy, poll_hz=0)
    with pytest.raises(ValueError, match="replan_s"):
        runner.watch(sim, numeric_live_policy, replan_s=0)
    assert runner._worker is None


def test_guidance_schedule_uses_simulation_time_without_held_clock_or_catchup_bursts(
    numeric_guidance_input,
):
    observation = numeric_guidance_input
    assert guidance_due(None, observation, 1)
    previous = _guidance_key(observation, 1)
    assert not guidance_due(previous, observation, 1)
    observation["time_s"] += 0.25
    assert not guidance_due(previous, observation, 1)
    observation["time_s"] += 0.25
    assert guidance_due(previous, observation, 1)
    observation["time_s"] = 100.0
    assert guidance_due(previous, observation, 1)
    previous = _guidance_key(observation, 1)
    assert not guidance_due(previous, observation, 1)
    assert guidance_due(previous, observation, 2)
    observation["tuning_revision"] += 1
    assert guidance_due(previous, observation, 1)


def test_terminal_transition_replans_once_even_when_the_final_simulation_clock_is_held(
    numeric_guidance_input,
):
    observation = numeric_guidance_input
    previous = _guidance_key(observation, 1)
    observation["mission"] = {"status": "success", "phase": "arrived"}
    assert guidance_due(previous, observation, 1)
    assert not guidance_due(_guidance_key(observation, 1), observation, 1)


@pytest.mark.parametrize(
    ("key", "value"),
    [("time_s", 0.0), ("seed", 101), ("manual_interventions", 1), ("frame", "other-world")],
)
def test_guidance_replans_after_real_clock_or_identity_changes(numeric_guidance_input, key, value):
    previous = _guidance_key(numeric_guidance_input, 1)
    numeric_guidance_input[key] = value
    assert guidance_due(previous, numeric_guidance_input, 1)


def test_debug_target_is_the_next_genuine_python_waypoint_not_a_fabricated_shortcut(
    numeric_guidance_input, numeric_live_policy
):
    route = numeric_live_policy.plan(numeric_guidance_input)
    point = next_waypoint(numeric_guidance_input, route, 20.0)
    target = lookahead_target(numeric_guidance_input, route)
    np.testing.assert_array_equal(point, route.points_world_m[1])
    np.testing.assert_array_equal(target, point)
    point[0] = 99
    assert route.points_world_m[1, 0] != 99
    assert np.linalg.norm(target - numeric_guidance_input["shuttle"]["position"]) > 250
    numeric_guidance_input["shuttle"]["position"][0] += 30.0
    np.testing.assert_array_equal(
        next_waypoint(numeric_guidance_input, route), route.points_world_m[1]
    )


@pytest.mark.parametrize("convention", ["active_xyzw", "passive_xyzw"])
def test_debug_no_plan_target_uses_body_x_and_the_explicit_quaternion_transform(
    numeric_guidance_input, convention
):
    observation = numeric_guidance_input
    observation["rotation_convention"] = convention
    observation["shuttle"]["rotation"] = [0.0, 0.0, np.sqrt(0.5), np.sqrt(0.5)]
    target = lookahead_target(observation)
    np.testing.assert_allclose(target, [1000.0, 250.0, 0.0], atol=1e-10)
    assert next_waypoint(observation, None) is None


def test_live_tuning_segments_actual_same_clock_reads_without_faking_telemetry_time(
    numeric_history_contract,
):
    observation = numeric_history_contract["observation"]
    observation.update(
        tuning_revision=0,
        ship_tuning={"mass_kg": 1000.0, "inertia_scale": 1.0, "thrust_scale": 1.0},
        debris_flow={
            "speed_mps": 20.0,
            "speed_spread_mps": 5.0,
            "stream_axis": "y",
            "stream_sign": -1,
        },
        experiment_settings={"baseline": "unchanged"},
    )
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    observation["tuning_revision"] = 1
    observation["ship_tuning"]["thrust_scale"] = 2.0
    observation["time_s"] = 2.5
    runner.record_snapshot(numeric_history_contract, source="ship_live_tune")
    rows = runner.history()
    assert len(rows) == 2
    assert [row["telemetry_time_s"] for row in rows] == [2.0, 2.0]
    assert [row["observation_time_s"] for row in rows] == [2.25, 2.5]
    assert rows[0]["segment"] != rows[1]["segment"]
    assert rows[1]["tuning_changed"] is True
    assert rows[1]["telemetry_held"] is True
    assert rows[1]["manual_intervention"] is False
    assert rows[1]["ship_tuning"]["thrust_scale"] == 2.0
    runner.record_snapshot(numeric_history_contract)
    assert len(runner.history()) == 2
    assert runner.history()[-1]["tuning_changed"] is True
    changes = [
        event for event in runner.configuration_changes() if event["kind"] == "reported_tuning"
    ]
    assert [change["tuning_revision"] for change in changes] == [0, 1]
    assert changes[-1]["sample_source"] == "ship_live_tune"
    assert changes[-1]["source_time_s"] == 2.5
    assert observation["experiment_settings"] == {"baseline": "unchanged"}


def test_tuning_report_without_telemetry_is_configuration_not_an_invented_measurement():
    runner = ExperimentRunner()
    runner.record_snapshot(
        {
            "observation": {
                "time_s": 7.5,
                "seed": 1,
                "tuning_revision": 2,
                "ship_tuning": {"mass_kg": 1200},
                "debris_flow": {},
            }
        }
    )
    assert runner.history() == []
    assert runner.configuration_changes()[0]["source_time_s"] == 7.5


@pytest.mark.parametrize(
    ("duration", "cadence"),
    [(0, 2), (301, 2), (True, 2), (np.nan, 2), (30, 0), (30, 11), (30, np.inf)],
)
def test_hot_run_limits_reject_invalid_numbers_without_any_worker(duration, cadence):
    with pytest.raises(ValueError):
        RunLimits(duration, cadence)
    runner = ExperimentRunner()
    with pytest.raises(ValueError):
        runner.update_run_limits(duration_s=duration, cadence_hz=cadence)
    assert runner._worker is None
    assert runner._run_request is None


def test_hot_run_limits_are_data_only_and_cannot_start_an_idle_runner():
    limits = RunLimits(np.float64(300), np.float64(10))
    assert limits.to_dict() == {"duration_s": 300.0, "requested_cadence_hz": 10.0}
    with pytest.raises(FrozenInstanceError):
        limits.duration_s = 1
    runner = ExperimentRunner()
    with pytest.raises(FlightStateError, match="Connect"):
        runner.update_run_limits(duration_s=12, cadence_hz=3)
    assert runner.status()["state"] == "idle"
    assert runner._worker is None
    assert runner.history() == runner.configuration_changes() == []


def test_worker_accepts_hot_timing_once_with_an_actual_source_clock_not_a_restart(
    numeric_guidance_input,
):
    """Pure worker acceptance boundary, with no substitute client or thread."""
    runner = ExperimentRunner()
    with runner._lock:
        revision = runner._queue_run_limits(RunLimits(12, 4))
    assert revision == 1
    assert runner.status().get("accepted_run_limits_revision") is None
    accepted, accepted_revision = runner._accept_run_limits(numeric_guidance_input)
    assert (accepted.duration_s, accepted.cadence_hz, accepted_revision) == (12.0, 4.0, 1)
    runner._accept_run_limits(numeric_guidance_input)
    assert len(runner.configuration_changes()) == 1
    event = runner.configuration_changes()[0]
    assert event["kind"] == "run_limits_accepted"
    assert (event["source_time_s"], event["source_seed"]) == (10.0, 100)
    assert event["run_origin_time_s"] is None
    assert runner._worker is None
    assert runner._run_request is None
    assert runner.status()["sample_count"] == 0
    assert runner.history() == []


@pytest.fixture
def numeric_active_timing():
    """Scheduling numbers only, not an emulated flight or transport response."""
    return {
        "origin": 100.0,
        "last_time": 112.0,
        "wall_origin": 500.0,
        "last_decision_wall": 514.0,
        "decision_time": 111.0,
        "duration": 30.0,
        "cadence": 2.0,
        "next_decision": 514.5,
        "deadline": 665.0,
    }


def test_hot_timing_preserves_simulation_origin_wall_origin_and_decision_clock(
    numeric_active_timing,
):
    original = deepcopy(numeric_active_timing)
    slower = retime_run(original, RunLimits(10, 0.5), now_wall_s=515.0)
    assert slower["origin"] == 100.0
    assert slower["wall_origin"] == 500.0
    assert slower["last_time"] == 112.0
    assert slower["decision_time"] == 111.0
    assert slower["deadline"] == 565.0
    assert slower["next_decision"] == 516.0
    assert run_limit_notice(slower).startswith("Observed run duration 12.000 s")
    assert numeric_active_timing == original
    faster = retime_run(original, RunLimits(60, 4), now_wall_s=515.0)
    assert faster["next_decision"] == 515.0
    assert faster["deadline"] == 815.0
    assert run_limit_notice(faster) is None
    duration_only = retime_run(original, RunLimits(60, 2), now_wall_s=515.0)
    assert duration_only["next_decision"] == original["next_decision"]


@pytest.mark.parametrize(
    ("status", "phase"),
    [("success", "arrived"), ("failed_damage", "failed")],
)
def test_terminal_mission_prevents_controller_start_without_guessed_force_or_distance(
    status,
    phase,
):
    snapshot = {
        "observation": {"mission": {"status": status, "phase": phase}},
        "runtime": {"running": False},
    }
    notice = controller_start_notice(snapshot)
    assert "Reset flight" in notice
    assert notice == terminal_mission_notice(snapshot["observation"])
    assert snapshot["runtime"]["running"] is False


def test_controller_guard_does_not_invent_a_running_mission_or_infer_success_from_range(
    numeric_guidance_input,
):
    assert controller_start_notice({}) is not None
    observation = deepcopy(numeric_guidance_input)
    observation["mission"]["target_distance_m"] = 0.0
    snapshot = {"observation": observation, "runtime": {"running": False}}
    assert terminal_mission_notice(observation) is None
    assert controller_start_notice(snapshot) is None
    del observation["mission"]["status"]
    assert "not reported a running mission" in controller_start_notice(snapshot)


def test_native_pause_or_manual_takeover_yields_but_live_tuning_does_not(numeric_guidance_input):
    observation = deepcopy(numeric_guidance_input)
    active = {"counter": 0, "seed": 100, "last_time": 9.0, "expect_running": True}
    runtime = {"running": True, "keyboard_thrust": False}
    observation["tuning_revision"] = 5
    assert controller_interruption(active, observation, runtime) is None
    runtime["running"] = False
    assert "paused" in controller_interruption(active, observation, runtime)
    runtime.update(running=True, keyboard_thrust=True)
    assert "manual thrust" in controller_interruption(active, observation, runtime)
    runtime["keyboard_thrust"] = False
    observation["manual_interventions"] = 1
    assert "intervention" in controller_interruption(active, observation, runtime)
    observation["manual_interventions"] = 0
    observation["time_s"] = 0.0
    assert "rewound" in controller_interruption(active, observation, runtime)


def test_context_separates_calibration_zero_requests_stored_wrench_and_optional_body_getter():
    context = flight_context(
        {
            "observation": {
                "time_s": 7.0,
                "ship_tuning": {"mass_kg": 100.0, "inertia_scale": 1.0, "thrust_scale": 3.2},
                "mission": {"status": "success", "phase": "arrived"},
                "shuttle": {
                    "commanded_accel_mps2": [0, 0, 0],
                    "commanded_angular_accel_rad_s2": [0, 0, 0],
                    "thrust_n": [0, 0, 0],
                    "torque_nm": [0, 0, 0],
                },
            },
            "runtime": {"running": False},
        }
    )
    assert context["command_state"] == "zero"
    assert context["playback"] == "paused"
    assert "Reset flight" in context["terminal_notice"]
    assert context["confirmed_ship_tuning"]["thrust_scale"] == 3.2
    assert context["stored_actuator_report"]["thrust_n"] == [0, 0, 0]
    assert context["applied_physics"] is None
    empty = flight_context(None)
    assert empty["command_state"] == empty["playback"] == "unavailable"
    assert empty["source_time_s"] is None
    assert empty["applied_physics"] is None


def test_actual_physics_projection_keeps_its_independent_clock_and_never_uses_requested_mass(
    numeric_history_contract,
):
    observation = numeric_history_contract["observation"]
    observation["ship_tuning"] = {"mass_kg": 100.0, "inertia_scale": 1.0, "thrust_scale": 3.2}
    physics = {
        "source_time_s": 2.125,
        "tuning_revision": 4,
        "frame": "numeric-body-frame",
        "mass_kg": 99.5,
        "inertia_body_kg_m2": [[1, 0, 0], [0, 2, 0], [0, 0, 3]],
        "total_external_force_world_n": [1, 2, 3],
        "total_external_torque_world_nm": [4, 5, 6],
        "external_force_provenance": "numeric serialization contract",
        "actuator_gain": 3.2,
        "actuation_state": "awaiting_application",
        "requested": {
            "application": "center_of_mass",
            "accel_body_mps2": [0.2, 0, 0],
            "angular_accel_body_rad_s2": [0, 0, 0],
        },
        "last_actuator_application": {
            "application_time_s": 1.5,
            "tuning_revision": 2,
            "requested": {
                "application": "center_of_mass",
                "accel_body_mps2": [0.05, 0, 0],
                "angular_accel_body_rad_s2": [0, 0, 0],
            },
            "actuator_gain": 1.0,
            "force_body_n": [2, 0, 0],
            "torque_body_nm": [0, 0.3, 0],
            "force_world_n": [0, 2, 0],
            "torque_world_nm": [0.3, 0, 0],
            "propellant_force_n": 2.0,
        },
    }
    observation["applied_physics"] = physics
    observation["shuttle"]["thrust_n"] = [9999.0, 0.0, 0.0]
    observation["shuttle"]["wrench_provenance"] = "command-derived numeric field"
    row = measured_sample(numeric_history_contract)
    assert row["telemetry_time_s"] == 2.0
    assert row["observation_time_s"] == 2.25
    assert row["applied_physics_time_s"] == 2.125
    assert row["applied_physics_tuning_revision"] == 4
    assert row["applied_physics_frame"] == "numeric-body-frame"
    assert row["applied_physics"] == physics
    assert row["ship_mass_kg"] == 1000.0
    assert row["ship_mass_report_kind"] == "public_shuttle_report"
    assert row["rapier_mass_kg"] == 99.5
    assert row["rapier_inertia_yy_kg_m2"] == 2.0
    assert row["external_force_world_x_n"] == 1.0
    assert row["actuator_force_body_x_n"] == 2.0
    assert row["actuator_application_time_s"] == 1.5
    assert row["actuator_application_tuning_revision"] == 2
    context = flight_context(numeric_history_contract)
    assert context["requested_accel_body_mps2"] == [0.2, 0, 0]
    assert context["requested_report_source"] == "applied_physics.requested"
    assert context["stored_actuator_report"]["thrust_n"][0] == 9999.0
    assert context["applied_physics"]["last_actuator_application"]["application_time_s"] == 1.5
    physics["mass_kg"] = 0.0
    assert row["applied_physics"]["mass_kg"] == 99.5


def test_missing_application_or_body_getter_never_becomes_zero_force_or_request_based_mass(
    numeric_history_contract,
):
    observation = numeric_history_contract["observation"]
    observation["applied_physics_error"] = "Numeric getter failure contract"
    observation["ship_tuning"] = {"mass_kg": 100.0, "inertia_scale": 1.0, "thrust_scale": 3.2}
    row = measured_sample(numeric_history_contract)
    assert row["rapier_mass_kg"] is None
    assert row["actuator_force_body_x_n"] is None
    assert row["actuator_application_time_s"] is None
    assert row["applied_physics_error"] == "Numeric getter failure contract"
    observation["applied_physics"] = {
        "source_time_s": 2.25,
        "mass_kg": 100.000002,
        "actuation_state": "not_applied",
        "last_actuator_application": None,
    }
    row = measured_sample(numeric_history_contract)
    assert row["rapier_mass_kg"] == 100.000002
    assert row["actuator_application_time_s"] is None
    assert row["actuator_force_body_x_n"] is None


def test_accepted_hot_versions_segment_held_history_without_creating_new_measurement_times(
    numeric_history_contract,
):
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    with runner._lock:
        runner._queue_run_limits(RunLimits(12, 4))
    runner._accept_run_limits(numeric_history_contract["observation"])
    runner.record_snapshot(numeric_history_contract)
    rows = runner.history()
    assert len(rows) == 2
    assert [row["telemetry_time_s"] for row in rows] == [2.0, 2.0]
    assert rows[-1]["configuration_changed"] is True
    assert rows[-1]["telemetry_held"] is True
    assert rows[-1]["accepted_run_limits_revision"] == 1
    assert rows[-1]["run_limits"] == {"duration_s": 12.0, "requested_cadence_hz": 4.0}
    assert rows[0]["segment"] != rows[1]["segment"]
    runner.record_snapshot(numeric_history_contract)
    assert len(runner.history()) == 2


@pytest.fixture
def numeric_setup_settings():
    """Parameter parsing inputs only, never SDK, network or physics responses."""
    return {
        "ship": {"mass_kg": 1000.0, "inertia_scale": 1.0},
        "debris": {
            "count": 500,
            "radius_min_m": 0.4,
            "radius_max_m": 10.0,
            "speed_mps": 20.0,
            "speed_spread_mps": 4.0,
            "shell_inner_m": 60.0,
            "shell_outer_m": 700.0,
            "stream_axis": "x",
            "stream_sign": 1,
        },
    }


@pytest.mark.parametrize("count", [0, 500, 1000, 10_000, 65_536, 100_000])
def test_real_body_count_parser_preserves_large_requested_counts(numeric_setup_settings, count):
    numeric_setup_settings["debris"]["count"] = count
    parsed = validate_experiment_settings(numeric_setup_settings)
    assert parsed["debris"]["count"] == count
    assert parsed["debris"]["distribution"] == {"kind": "directed_stream"}
    assert MAX_DEBRIS_COUNT == 100_000


@pytest.mark.parametrize("count", [-1, 1.5, True, 100_001])
def test_setup_parser_rejects_invalid_counts_without_any_request(numeric_setup_settings, count):
    numeric_setup_settings["debris"]["count"] = count
    with pytest.raises(ValueError, match="debris.count"):
        validate_experiment_settings(numeric_setup_settings)


def test_ejecta_schema_is_closed_and_keeps_initial_conditions_separate_from_stream_reference(
    numeric_setup_settings,
):
    initial = {
        "source_direction": [-1.0, 0.0, 0.0],
        "kick_velocity_mps": [-1.0, 0.0, 0.0],
        "tangential_speed_mps": 7.0,
        "patch_half_angle_deg": 15.0,
    }
    numeric_setup_settings["debris"]["distribution"] = {
        "kind": "impact_ejecta",
        "settings": initial,
    }
    parsed = validate_experiment_settings(numeric_setup_settings)
    assert parsed["debris"]["distribution"]["settings"] == initial
    assert parsed["debris"]["speed_mps"] == 20.0
    assert parsed["debris"]["stream_axis"] == "x"
    initial["source_direction"][0] = 99.0
    assert parsed["debris"]["distribution"]["settings"]["source_direction"][0] == -1.0
    for bad in (
        None,
        {"kind": "directed_stream", "orbit_speed_mps": 7},
        {"kind": "impact_ejecta", "settings": {**initial, "orbit_speed_mps": 7}},
        {"kind": "impact_ejecta", "settings": {**initial, "source_direction": [0, 0, 0]}},
        {"kind": "impact_ejecta", "settings": {**initial, "kick_velocity_mps": [150, 150, 0]}},
        {"kind": "impact_ejecta", "settings": {**initial, "patch_half_angle_deg": 0}},
    ):
        numeric_setup_settings["debris"]["distribution"] = bad
        with pytest.raises(ValueError):
            validate_experiment_settings(numeric_setup_settings)


def test_checked_control_context_requires_real_receipt_fields_without_synthetic_defaults():
    values = {
        "observation": {
            "trial_revision": 5,
            "tuning_revision": 7,
            "manual_interventions": 2,
            "seed": 100,
        },
        "runtime": {"running": False},
    }
    assert control_context(values) == {
        "trial_revision": 5,
        "tuning_revision": 7,
        "manual_interventions": 2,
        "seed": 100,
        "running": False,
    }
    for field in ("trial_revision", "tuning_revision", "manual_interventions", "seed"):
        missing = deepcopy(values)
        del missing["observation"][field]
        with pytest.raises(ValueError):
            control_context(missing)
    values["observation"]["trial_revision"] = True
    with pytest.raises(ValueError):
        control_context(values)
    assert issubclass(StaleContextError, LocalApiError)


def test_new_client_actions_validate_bad_inputs_before_any_connection(numeric_setup_settings):
    # The actual client is constructed without connecting. No response method,
    # network call or simulation is replaced, and no valid action is submitted.
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(ValueError, match="Flight preset"):
        client.configure_flight(numeric_setup_settings, "mars_facing_l2")
    with pytest.raises(ValueError, match="seed"):
        client.configure_flight(numeric_setup_settings, "mars_facing_l1", seed=0x100000000)
    with pytest.raises(ValueError, match="position_m"):
        client.place_lander([float("nan"), 0, 0])
    with pytest.raises(ValueError, match="trial_revision"):
        client.set_command_checked(
            [0, 0, 0],
            [0, 0, 0],
            {
                "observation": {"seed": 100, "tuning_revision": 0, "manual_interventions": 0},
                "runtime": {"running": False},
            },
        )


def test_runner_placement_validation_does_not_detach_or_start_an_idle_writer():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    runner = ExperimentRunner()
    before = runner.status()
    with pytest.raises(ValueError, match="position_m"):
        runner.place_lander(client, [float("nan"), 0, 0])
    with pytest.raises(ValueError, match="timeout_s"):
        runner.place_lander(client, [14_000, 0, 0], timeout_s=-1)
    assert runner.status() == before
    assert runner._worker is None


def test_same_seed_same_clock_new_trial_invalidates_guidance_and_controller_without_time_math(
    numeric_guidance_input,
):
    observation = numeric_guidance_input
    observation.update(time_s=0.0, trial_revision=1)
    previous = _guidance_key(observation, 1)
    active = {
        "seed": 100,
        "counter": 0,
        "last_time": 0.0,
        "expect_running": True,
        "trial_revision": 1,
    }
    observation["trial_revision"] = 2
    assert guidance_due(previous, observation, 1)
    assert "trial/setup changed" in controller_interruption(active, observation, {"running": True})
    assert observation["time_s"] == 0.0
    assert not guidance_due(_guidance_key(observation, 1), observation, 1)


def test_recorded_placement_splits_held_measurements_and_exports_real_pose_inputs(
    numeric_history_contract,
):
    observation = numeric_history_contract["observation"]
    observation.update(trial_revision=1, flight_preset="far_side_l2")
    observation["experiment_settings"] = {"debris": {"distribution": {"kind": "directed_stream"}}}
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    observation.update(trial_revision=2, manual_interventions=1)
    observation["applied_physics"] = {
        "source_time_s": 2.25,
        "position_world_m": [-14_000.0, 10.0, 20.0],
        "velocity_world_mps": [0.0, 0.0, 0.0],
    }
    runner.record_snapshot(numeric_history_contract, source="place_lander")
    rows = runner.history()
    assert len(rows) == 2
    assert rows[-1]["recorded_placement"] is True
    assert rows[-1]["setup_changed"] is True
    assert rows[-1]["rapier_position_world_x_m"] == -14_000.0
    assert rows[-1]["rapier_velocity_world_x_mps"] == 0.0
    assert rows[0]["telemetry_time_s"] == rows[1]["telemetry_time_s"] == 2.0
    assert rows[0]["segment"] != rows[1]["segment"]
    assert runner.configuration_changes()[-1]["kind"] == "recorded_placement"
    assert observed_setup(observation)["debris_distribution"] == {"kind": "directed_stream"}


def test_reported_launch_site_pose_splits_history_without_claiming_a_flown_trajectory(
    numeric_history_contract,
):
    """Projection inputs only, not a replacement for the native terrain/physics tests."""
    observation = numeric_history_contract["observation"]
    observation.update(trial_revision=1, launch_site_direction=None)
    observation["launchpad"] = {"position": [-14_000, 0, 0], "rotation": [0, 0, 0, 1]}
    runner = ExperimentRunner()
    runner.record_snapshot(numeric_history_contract)
    observation.update(
        trial_revision=2, manual_interventions=1, launch_site_direction=[1.0, 0.0, 0.0]
    )
    observation["launchpad"] = {"position": [14_000, 0, 0], "rotation": [0, 1, 0, 0]}
    runner.record_snapshot(numeric_history_contract, source="guidance_worker")
    rows = runner.history()
    assert len(rows) == 2
    assert rows[1]["launch_site_changed"] is True
    assert rows[1]["manual_intervention"] is True
    assert rows[1]["launchpad"]["position"] == [14_000, 0, 0]
    assert rows[1]["segment"] != rows[0]["segment"]
    assert rows[1]["telemetry_held"] is True
    assert rows[1]["telemetry_time_s"] == rows[0]["telemetry_time_s"]
    assert runner.configuration_changes()[-1]["kind"] == "reported_launch_site"
    observation["launchpad"]["source_time_s"] = 100.0
    runner.record_snapshot(numeric_history_contract)
    assert len(runner.history()) == 2, "a held pad pose is not another launch-site change"


def test_xyz_bounds_and_pose_readback_math_do_not_use_a_request_echo():
    assert validate_position([10_000_000, -10_000_000, 0]) == [10_000_000.0, -10_000_000.0, 0.0]
    for value in ([float("nan"), 0, 0], [10_000_001, 0, 0], [0, 0], [True, False, False]):
        with pytest.raises(ValueError):
            validate_position(value)
    # Geometry/data projection only. No client RPC or physics result is replaced.
    s = np.sqrt(0.5)
    values = {
        "runtime": {"running": False},
        "observation": {
            "applied_physics": {
                "position_world_m": [14_000.0, 0.0, 0.0],
                "rotation_body_to_world_xyzw": [0, s, 0, s],
                "velocity_world_mps": [0, 0, 0],
                "angular_velocity_world_rad_s": [0, 0, 0],
                "total_external_force_world_n": [0, 0, 0],
                "total_external_torque_world_nm": [0, 0, 0],
                "requested": {"accel_body_mps2": [0, 0, 0], "angular_accel_body_rad_s2": [0, 0, 0]},
            }
        },
    }
    assert placement_readback_matches(values, [14_000, 0, 0])
    assert not placement_readback_matches(values, [15_000, 0, 0])
    values["observation"]["applied_physics"]["velocity_world_mps"] = [1, 0, 0]
    assert not placement_readback_matches(values, [14_000, 0, 0])
