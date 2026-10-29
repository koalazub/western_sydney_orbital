"""Constructed numeric fixtures only, not SDK, network, graphics or flight evidence."""

from __future__ import annotations

import ast
import json
import sys
from copy import deepcopy
from dataclasses import asdict, replace
from importlib.resources import files
from pathlib import Path

import numpy as np
import pytest

from wsorb.controllers import (
    PROFILE_IDS,
    catalog,
    create_controller,
    policy_factory,
    sample_from_observation,
)
from wsorb.experiment import Decision

IDS = (
    "baseline",
    "body-axis",
    "basic_pd_radar",
    "coast",
    "bui_alpha_beta_mvp",
    "tom_kalman_velocity_mvp",
    "userapi_body_z",
)
GENERIC_IDS = IDS[2:]
TRACKED_IDS = ("bui_alpha_beta_mvp", "tom_kalman_velocity_mvp")
FRAME = "Constructed native reference frame, SI; not a runtime measurement"
SOURCE = "native_manual_public_observation"


def row(time=0.0, relative=(4000.0, 1000.0, 0.0), *, diameter=2.0, rate=0.0):
    return [list(relative), [float(np.linalg.norm(relative)), rate, diameter], [time, 0.0, 0.0]]


def observation(time=0.0, *, rows=None, navigation_time=None, position=None, velocity=None):
    """Versioned numeric contract data. No sensor delivery or backend is mocked."""
    nav = time if navigation_time is None else navigation_time
    rows = [row(time)] if rows is None else deepcopy(rows)
    radar_time = max(r[2][0] for r in rows) if rows else time
    return {
        "time_s": time,
        "seed": 100,
        "trial_revision": 1,
        "tuning_revision": 0,
        "manual_interventions": 0,
        "frame": FRAME,
        "rotation_convention": "active_xyzw",
        "radar_clock": "simulation_elapsed_seconds",
        "navigation_sample_time_s": nav,
        "shuttle": {
            "position": [0.0, 0.0, 0.0] if position is None else list(position),
            "velocity": [0.0, 0.0, 0.0] if velocity is None else list(velocity),
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "angular_velocity_rad_s": [0.0, 0.0, 0.0],
        },
        "target": {"position": [1000.0, 0.0, 0.0]},
        "mission": {"fuel_used_kg": 0.0, "damage": 0.0, "status": "running"},
        "applied_physics": {
            "source_time_s": nav,
            "tuning_revision": 0,
            "frame": FRAME,
            "mass_kg": 3388.7,
            "inertia_body_kg_m2": [[3000.0, 0.0, 0.0], [0.0, 6000.0, 0.0], [0.0, 0.0, 7000.0]],
        },
        "applied_physics_error": None,
        "radar": rows,
        "max_accel_mps2": 0.5,
        "max_angular_accel_rad_s2": 0.05,
        "sensor_health": {
            "schema_version": 1,
            "revision": 0,
            "units": [
                {
                    "id": identity,
                    "delivery": "ready",
                    "active_faults": [],
                    "sample_time_s": epoch,
                    "sequence": round(epoch * 1000),
                }
                for identity, epoch in (("navigation", nav), ("radar_primary", radar_time))
            ],
        },
    }


def sample(value):
    return sample_from_observation(value, source_kind=SOURCE)


def command(controller, value):
    return controller.command(sample(value), value["radar"], now_elapsed_time_s=value["time_s"])


def test_shared_json_catalog_is_exact_detached_safe_and_grouped_by_name():
    expected = json.loads(files("wsorb.controllers").joinpath("catalog.json").read_text())
    entries = catalog()
    assert entries == expected
    assert PROFILE_IDS == IDS and type(PROFILE_IDS) is tuple
    assert [entry["id"] for entry in entries] == list(IDS)
    fields = {
        "id",
        "label",
        "owner",
        "contributor",
        "description",
        "profile",
        "experimental",
        "target_seeking",
        "source",
    }
    for entry in entries:
        assert set(entry) == fields
        assert entry["owner"] in ("Tom", "Harry", "Bui", "WSO")
        assert type(entry["experimental"]) is bool
        assert type(entry["target_seeking"]) is bool
        assert not Path(entry["source"]).is_absolute()
    owners = {entry["id"]: entry["owner"] for entry in entries}
    assert owners == dict(
        zip(IDS, ("WSO", "WSO", "Harry", "Harry", "Bui", "Tom", "Tom"), strict=True)
    )
    assert "harness selection" in entries[2]["contributor"]
    assert "template" in entries[-1]["label"].lower() and not entries[-1]["target_seeking"]
    entries[0]["owner"] = "changed"
    assert catalog() == expected
    json.dumps(catalog(), allow_nan=False)


@pytest.mark.parametrize("controller_id", IDS[:2])
def test_checkpoint_factories_return_the_exact_original_controller_and_config(controller_id):
    from wsorb.checkpoint import policy_config
    from wsorb.stp_policy import STPCheckpointController

    controller = create_controller(controller_id)
    original = STPCheckpointController(policy_config(controller_id))
    assert type(controller) is STPCheckpointController
    assert asdict(controller.config) == asdict(original.config)
    value = observation()
    assert all(
        np.array_equal(a, b)
        for a, b in zip(command(controller, value), command(original, value), strict=True)
    )
    assert controller.report == original.report
    assert "eligible" not in controller.report


@pytest.mark.parametrize("controller_id", IDS)
def test_factories_have_fresh_per_trial_state_and_return_real_decisions(controller_id):
    first = policy_factory(controller_id, {})
    second = policy_factory(controller_id, {})
    assert first is not second and first.controller is not second.controller
    assert callable(first) and isinstance(first(observation()), Decision)
    assert second.report["calls"] == 0
    if controller_id in GENERIC_IDS:
        assert first.report["eligible"] is True
        assert first.report["experimental"] is first.report["best_effort"] is True
        assert first.report["decision_valid_until_elapsed_s"] == 1.0
        report = first.report
        report["failure_reasons"].append("changed")
        report["command"]["accel_body_mps2"][0] = 999
        assert first.report != report


@pytest.mark.parametrize(
    "source_kind", ["local_public_snapshot", "hosted_room_public_snapshot", SOURCE]
)
def test_producer_copies_genuine_numeric_fields_and_keeps_healthy_empty_radar(source_kind):
    value = observation(3.0, rows=[])
    produced = sample_from_observation(value, source_kind=source_kind)
    assert produced.elapsed_time_s == 3.0 and produced.source_kind == source_kind
    assert produced.radar_sample_time_s == 3.0
    assert produced.public_context == (100, 1, 0, 0, 0)
    assert produced.mass_kg == value["applied_physics"]["mass_kg"]
    assert produced.frame == value["frame"]
    value["shuttle"]["position"][0] = 123
    assert produced.position_world_m[0] == 0
    with pytest.raises(ValueError):
        produced.position_world_m.flags.writeable = True


@pytest.mark.parametrize("source_kind", ["spaceteams", "native", "", None, "/tmp/controller.py"])
def test_source_labels_are_allowlisted(source_kind):
    with pytest.raises(ValueError, match="source_kind"):
        sample_from_observation(observation(), source_kind=source_kind)


@pytest.mark.parametrize(
    "path,value",
    [
        (("sensor_health",), None),
        (("sensor_health", "schema_version"), True),
        (("sensor_health", "units", 0, "delivery"), "frozen"),
        (("sensor_health", "units", 1, "delivery"), "unavailable"),
        (("sensor_health", "units", 1, "active_faults"), [{"target": "radar_bus"}]),
        (("sensor_health", "units", 1, "sample_time_s"), None),
        (("sensor_health", "units", 0, "sequence"), None),
        (("sensor_health", "units", 0, "sample_time_s"), 1.0),
        (("navigation_sample_time_s",), None),
        (("shuttle", "position"), None),
        (("applied_physics",), None),
        (("applied_physics", "source_time_s"), 0.1),
        (("applied_physics", "frame"), "other"),
        (("applied_physics", "tuning_revision"), 1),
        (("applied_physics_error",), "Unavailable actual physics"),
        (("radar_clock",), "unknown_epoch"),
        (("rotation_convention",), "passive_xyzw"),
        (("frame",), ""),
        (("trial_revision",), True),
        (("seed",), None),
        (("max_accel_mps2",), 1.0),
        (("max_angular_accel_rad_s2",), 0.1),
    ],
)
def test_producer_rejects_missing_faulted_or_mismatched_actual_context(path, value):
    data = observation()
    destination = data
    for key in path[:-1]:
        destination = destination[key]
    destination[path[-1]] = value
    with pytest.raises(ValueError):
        sample(data)


def test_empty_radar_is_not_a_substitute_for_missing_or_unready_delivery():
    data = observation(rows=[])
    del data["sensor_health"]["units"][1]["sample_time_s"]
    with pytest.raises(ValueError):
        sample(data)
    data = observation(rows=[])
    data["sensor_health"]["units"].append(deepcopy(data["sensor_health"]["units"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        sample(data)
    data = observation(rows=[])
    del data["radar"]
    with pytest.raises(ValueError):
        sample(data)


@pytest.mark.parametrize(
    "data",
    [
        observation(10.0, navigation_time=8.9, rows=[]),
        observation(10.0, navigation_time=10.1, rows=[]),
        observation(10.0, rows=[row(10.1)]),
        observation(10.0, rows=[row(7.4)]),
    ],
)
def test_producer_rejects_future_and_stale_actual_navigation_and_radar(data):
    with pytest.raises(ValueError):
        sample(data)


@pytest.mark.parametrize("controller_id", GENERIC_IDS)
def test_generic_lease_uses_original_sample_and_radar_epochs_not_command_now(controller_id):
    controller = create_controller(controller_id)
    data = observation(10.0, rows=[])
    produced = sample(data)
    controller.command(produced, [], now_elapsed_time_s=10.1)
    assert controller.report["eligible"] is True
    assert controller.report["decision_valid_until_elapsed_s"] == 11.0
    controller.command(produced, [], now_elapsed_time_s=10.9)
    assert controller.report["decision_valid_until_elapsed_s"] == 11.0
    controller.command(produced, [], now_elapsed_time_s=11.1)
    assert controller.report["eligible"] is False
    assert controller.report["command"]["accel_body_mps2"] == [0.0, 0.0, 0.0]


def test_empty_radar_lease_remembers_its_own_publication_time():
    data = observation(10.0, rows=[])
    data["sensor_health"]["units"][1].update(sample_time_s=8.0, sequence=8000)
    controller = create_controller("coast")
    command(controller, data)
    assert controller.report["eligible"] is True
    assert controller.report["decision_valid_until_elapsed_s"] == 10.5


@pytest.mark.parametrize("controller_id", GENERIC_IDS)
def test_generic_rewind_and_context_changes_latch_rejection_until_fresh_factory(controller_id):
    controller = create_controller(controller_id)
    command(controller, observation(2.0, rows=[]))
    assert controller.report["eligible"] is True
    command(controller, observation(1.0, rows=[]))
    assert controller.report["eligible"] is False
    command(controller, observation(3.0, rows=[]))
    assert controller.report["eligible"] is False
    fresh = create_controller(controller_id)
    command(fresh, observation(3.0, rows=[]))
    assert fresh.report["eligible"] is True
    changed = observation(4.0, rows=[])
    changed["trial_revision"] += 1
    command(fresh, changed)
    assert fresh.report["eligible"] is False


@pytest.mark.parametrize("controller_id", GENERIC_IDS)
def test_generic_rejects_source_frame_unknown_clock_future_and_stale_rows(controller_id):
    for changed in (
        {"source_kind": "unknown_source"},
        {"rotation_convention": "passive_xyzw"},
        {"radar_clock": "unknown"},
    ):
        controller = create_controller(controller_id)
        produced = replace(sample(observation(10.0)), **changed)
        accel, angular = controller.command(produced, [row(10.0)], now_elapsed_time_s=10.0)
        assert not controller.report["eligible"]
        assert np.count_nonzero(accel) == np.count_nonzero(angular) == 0
    controller = create_controller(controller_id)
    command(controller, observation(10.0, rows=[]))
    produced = replace(sample(observation(11.0, rows=[])), frame="Changed reference frame")
    controller.command(produced, [], now_elapsed_time_s=11.0)
    assert not controller.report["eligible"]
    for epoch in (10.1, 7.0):
        controller = create_controller(controller_id)
        controller.command(sample(observation(10.0)), [row(epoch)], now_elapsed_time_s=10.0)
        assert not controller.report["eligible"]


def test_source_publication_epoch_and_sequence_cannot_rewind_without_clock_reset():
    controller = create_controller("coast")
    command(controller, observation(10.0, rows=[]))
    data = observation(10.5, rows=[])
    data["sensor_health"]["units"][1].update(sequence=9000, sample_time_s=9.0)
    command(controller, data)
    assert not controller.report["eligible"]
    command(controller, observation(11.0, rows=[]))
    assert not controller.report["eligible"]


def test_plain_numeric_control_sample_also_rejects_radar_row_clock_rewind():
    from wsorb.control_contract import ControlSample

    def plain(value):
        full = asdict(sample(value))
        return ControlSample(**{key: full[key] for key in ControlSample.__dataclass_fields__})

    controller = create_controller("coast")
    controller.command(plain(observation(10.0)), [row(10.0)], now_elapsed_time_s=10.0)
    controller.command(plain(observation(10.5)), [row(9.0)], now_elapsed_time_s=10.5)
    assert not controller.report["eligible"]
    controller.command(plain(observation(11.0)), [row(11.0)], now_elapsed_time_s=11.0)
    assert not controller.report["eligible"]


@pytest.mark.parametrize("controller_id", TRACKED_IDS)
def test_mixed_radar_epochs_use_actual_interpolated_observer_poses(controller_id):
    controller = create_controller(controller_id)
    command(controller, observation(0.0, rows=[], velocity=[10.0, 0.0, 0.0]))
    data = observation(
        2.0,
        rows=[row(1.0, (100.0, 0.0, 0.0)), row(2.0, (0.0, 200.0, 0.0), diameter=8.0)],
        position=[20.0, 0.0, 0.0],
        velocity=[10.0, 0.0, 0.0],
    )
    command(controller, data)
    assert controller.report["eligible"]
    assert controller.report["tracking"]["ingested_rows"] == 2
    if controller_id.startswith("bui"):
        assert controller.tracks.scans == 2
        assert sorted(controller.tracks.last_seen) == [1.0, 2.0]
        first_position = controller.tracks.p[np.argmin(controller.tracks.last_seen)]
    else:
        assert len(controller.tracker.tracks) == 2
        first_position = min(controller.tracker.tracks, key=lambda tr: tr.last_seen).x[:3]
    # At t=1 the actual interpolated shuttle pose is x=10, not current x=20.
    # That contact is at x=110 at its epoch, then advances at its radial 10 m/s.
    np.testing.assert_allclose(first_position, [120.0, 0.0, 0.0])


@pytest.mark.parametrize("controller_id", TRACKED_IDS)
def test_newer_radar_waits_in_buffer_until_actual_navigation_history_covers_it(controller_id):
    controller = create_controller(controller_id)
    command(controller, observation(2.0, navigation_time=1.0, rows=[row(2.0)]))
    assert not controller.report["eligible"]
    assert controller.report["tracking"]["pending_epochs"] == 1
    command(controller, observation(2.0, rows=[row(2.0)]))
    assert controller.report["eligible"]
    assert controller.report["tracking"]["ingested_rows"] == 1
    assert controller.report["tracking"]["pending_epochs"] == 0


@pytest.mark.parametrize("controller_id", TRACKED_IDS)
def test_old_scan_never_uses_current_pose_then_fresh_scan_can_run(controller_id):
    controller = create_controller(controller_id)
    command(controller, observation(10.0, rows=[row(9.0)], position=[100.0, 0.0, 0.0]))
    assert not controller.report["eligible"]
    command(controller, observation(10.5, rows=[row(10.5)], position=[105.0, 0.0, 0.0]))
    assert controller.report["eligible"]
    assert controller.report["tracking"]["ingested_rows"] == 1


@pytest.mark.parametrize("controller_id,limit", [(TRACKED_IDS[0], 500), (TRACKED_IDS[1], 50)])
def test_nearest_original_row_limits_and_track_expiry_on_healthy_empty_scan(controller_id, limit):
    controller = create_controller(controller_id)
    rows = [row(0.0, (4000.0 + i * 100.0, 1000.0, 0.0)) for i in range(limit + 2)]
    command(controller, observation(rows=rows))
    assert controller.report["tracking"]["selected_original_rows"] == limit
    assert controller.report["guidance"]["track_count"] == limit
    command(controller, observation(11.0, rows=[]))
    assert controller.report["eligible"]
    assert controller.report["guidance"]["track_count"] == 0


@pytest.mark.parametrize("controller_id", ("basic_pd_radar", *TRACKED_IDS))
def test_real_reference_commands_are_transformed_to_body_coordinates(controller_id):
    controller = create_controller(controller_id)
    data = observation(rows=[])
    data["shuttle"]["rotation"] = [0.0, 0.0, np.sqrt(0.5), np.sqrt(0.5)]
    accel, angular = command(controller, data)
    assert controller.report["eligible"]
    assert accel[1] < -0.24 and abs(accel[0]) < 1e-14
    assert np.linalg.norm(accel) <= 0.5 and np.linalg.norm(angular) <= 0.05


def test_hy_commands_are_existing_math_and_template_is_explicitly_not_guidance():
    from wsorb.flight.control import basic_command, coast_command

    data = observation()
    for controller_id, function in (("basic_pd_radar", basic_command), ("coast", coast_command)):
        actual = command(create_controller(controller_id), data)
        expected = function(data)
        for left, right in zip(actual, expected, strict=True):
            np.testing.assert_array_equal(left, right)
    controller = create_controller("userapi_body_z")
    accel, angular = command(controller, observation(rows=[]))
    np.testing.assert_array_equal(accel, [0.0, 0.0, 0.5])
    np.testing.assert_array_equal(angular, np.zeros(3))
    assert controller.report["template"]["original_accel_body_mps2"] == [0.0, 0.0, 1.0]
    assert not controller.report["template"]["target_seeking"]


def test_bui_projected_hold_cap_matches_prediction_and_angular_norm_is_bounded():
    controller = create_controller("bui_alpha_beta_mvp")
    guidance = controller.guidance
    guidance.mode = "AVOID"
    guidance.vhat0 = np.array([1.0, 0.0, 0.0])
    guidance.vhold.command(np.array([17.0, 0.0, 0.0]))
    velocity = np.array([0.0, 20.0, 0.0])
    actual = guidance.vhold.step(velocity)
    trajectory = controller.modules["trajectory"]
    _, predicted_velocity = trajectory.propagate(
        np.zeros(3),
        velocity,
        np.array([0.0, 0.25]),
        v_des=guidance.vhold.v_des,
        along=guidance.vhat0,
    )
    np.testing.assert_allclose(actual, [0.25, 0.0, 0.0], atol=1e-15)
    np.testing.assert_allclose((predicted_velocity[1] - velocity) / 0.25, actual, atol=1e-14)
    angular = controller.reorienter.command([0.0, 1.0, 1.0], [1.0, -1.0, 1.0])
    assert np.linalg.norm(angular) <= controller.reorienter.maximum < np.nextafter(0.05, 0.0)
    assert controller.modules["config"].ACC_MAX < 0.25


def test_bui_candidates_and_execution_use_the_same_bounded_burn_composition():
    controller = create_controller("bui_alpha_beta_mvp")
    trajectory, state = controller.modules["trajectory"], controller.modules["state_estimator"]
    avoidance = controller.modules["avoidance"]
    ts = np.arange(0.0, 8.01, 0.25)
    p, v, vhat = np.zeros(3), np.array([10.0, 0.5, 0.0]), np.array([1.0, 0.0, 0.0])
    desired = np.array([17.0, 0.0, 0.0])
    nominal = trajectory.propagate(p, v, ts, v_des=desired, along=vhat)[0]
    snapshot = state.Snapshot(
        pos=np.array([[40.0, 0.0, 0.0]]),
        vel=np.zeros((1, 3)),
        sig_p=np.array([0.1]),
        sig_v=np.array([0.1]),
        diam=np.array([2.0]),
        n=np.array([5]),
        ids=np.array([1]),
        age=np.zeros(1),
    )
    conflicts = trajectory.predict_conflicts(nominal, snapshot, ts)
    plan = avoidance.plan_avoidance(0.0, p, v, vhat, nominal, snapshot, conflicts, ts)
    assert plan is not None
    actual_path = trajectory.propagate(p, v, ts, v_des=desired, along=vhat, burns=plan.burns)[0]
    np.testing.assert_allclose(plan.path, actual_path, atol=1e-12)
    for index, burn in enumerate(plan.burns):
        assert np.linalg.norm(burn.accel) <= 0.25
        assert all(burn.t0 >= other.t1 for other in plan.burns[:index])
        assert np.linalg.norm(burn.accel + np.array([0.25, 0.0, 0.0])) <= 0.5


def test_bui_omitted_short_second_burn_is_assessed_as_omitted_before_ranking():
    controller = create_controller("bui_alpha_beta_mvp")
    accel = controller.modules["accel_controller"]
    ts = np.arange(0.0, 3.01, 0.25)
    direction = np.array([[0.0, 1.0, 0.0]])
    offset, tB, _ = accel.maneuver_offset(
        ts, direction, np.array([0.25]), np.array([1.0]), np.array([0.0, -0.249, 0.0])
    )
    assert 0 < tB[0] <= 0.05
    path, _ = controller.modules["trajectory"].propagate(
        np.zeros(3), np.zeros(3), ts, burns=[accel.Burn(0.0, 1.0, direction[0] * 0.25)]
    )
    np.testing.assert_allclose(offset[0], path, atol=1e-15)


def test_tom_actual_gain_two_governor_is_bounded_before_prediction_and_execution():
    from wsorb.controllers._tom import _batch_governor

    controller = create_controller("tom_kalman_velocity_mvp")
    source = controller.modules["avoidance"]
    assert source.BurnGovernor(gain=2.0).accel([17.0, 0.0, 0.0], [0.0, 0.0, 0.0])[0] == 34
    governor = controller.governor
    assert governor.gain == 2.0
    for velocity in ([0.0, 0.0, 0.0], [16.95, 0.0, 0.0], [16.8, 0.0, 0.0], [16.6, 0.0, 0.0]):
        predicted, firing = _batch_governor(
            np.array([[17.0, 0.0, 0.0]]),
            np.array([velocity]),
            np.array([governor.firing]),
            governor,
        )
        actual = governor.accel([17.0, 0.0, 0.0], velocity)
        np.testing.assert_allclose(predicted[0], actual, atol=1e-15)
        assert bool(firing[0]) == governor.firing
        assert np.linalg.norm(actual) <= 0.5


def test_tom_candidate_assessment_uses_real_bounded_transition_not_old_lag_approval():
    controller = create_controller("tom_kalman_velocity_mvp")
    snapshot = {
        "id": np.array([1]),
        "p_rel": np.array([[0.0, 10.0, 0.0]]),
        "v_abs": np.array([[0.0, -2.0, 0.0]]),
        "diameter": np.array([1.0]),
        "sigma_p": np.zeros(1),
        "sigma_v": np.zeros(1),
    }
    desired = np.array([17.0, 0.0, 0.0])
    velocity = np.zeros(3)
    legacy = controller.modules["avoidance"].Avoider()
    np.testing.assert_array_equal(legacy.pick(desired, snapshot, velocity), desired)
    assert legacy.last_info["blocked"] is False
    clearance = controller.avoider.assess(np.array([desired]), snapshot, velocity)
    assert clearance[0] < 0.0  # The bounded vehicle cannot instantly escape the crossing.
    controller.avoider.pick(desired, snapshot, velocity)
    assert controller.avoider.last_info["blocked"] is True


def test_tom_source_candidate_generation_cost_and_previous_command_hysteresis_are_retained():
    controller = create_controller("tom_kalman_velocity_mvp")
    desired = np.array([17.0, 0.0, 0.0])
    previous = np.array([17.0, 0.1, 0.0])
    legacy = controller.modules["avoidance"].Avoider()
    legacy.v_prev = previous.copy()
    controller.avoider.v_prev = previous.copy()
    candidates = controller.avoider._candidates(desired, desired)
    np.testing.assert_array_equal(candidates, legacy._candidates(desired, desired))
    assert candidates.shape == (343, 3)
    snapshot = {
        "id": np.array([1]),
        "p_rel": np.array([[5000.0, 5000.0, 0.0]]),
        "v_abs": np.zeros((1, 3)),
        "diameter": np.array([1.0]),
        "sigma_p": np.zeros(1),
        "sigma_v": np.zeros(1),
    }
    # Returning exactly to v_des is cheapest, but the source's previous-command
    # cost is within its 0.3 m/s switching margin, so both retain slot 2.
    np.testing.assert_array_equal(legacy.pick(desired, snapshot, desired), previous)
    np.testing.assert_array_equal(controller.avoider.pick(desired, snapshot, desired), previous)


def test_tom_coincident_target_is_zero_desired_velocity_and_factory_does_not_reset_global_ids():
    first = create_controller("tom_kalman_velocity_mvp")
    data = observation()
    data["target"]["position"] = data["shuttle"]["position"].copy()
    accel, angular = command(first, data)
    assert first.report["eligible"]
    np.testing.assert_array_equal(
        first.report["guidance"]["desired_velocity_reference_mps"], np.zeros(3)
    )
    np.testing.assert_array_equal(accel, np.zeros(3))
    np.testing.assert_array_equal(angular, np.zeros(3))
    old_id = first.tracker.tracks[0].id
    second = create_controller("tom_kalman_velocity_mvp")
    assert second.tracker.tracks == [] and not second.governor.firing
    command(second, observation())
    assert second.tracker.tracks[0].id > old_id


@pytest.mark.parametrize("controller_id", GENERIC_IDS)
def test_observation_policy_never_carries_state_across_trial_context_or_clock_rewind(controller_id):
    policy = policy_factory(controller_id)
    policy(observation(2.0, rows=[]))
    with pytest.raises(ValueError, match="fresh policy"):
        policy(observation(1.0, rows=[]))
    with pytest.raises(ValueError, match="fresh policy"):
        policy(observation(3.0, rows=[]))
    policy = policy_factory(controller_id)
    policy(observation(rows=[]))
    changed = observation(1.0, rows=[])
    changed["trial_revision"] += 1
    with pytest.raises(ValueError, match="fresh policy"):
        policy(changed)


def test_sources_load_only_allowlisted_math_without_bare_import_or_sdk_side_effects():
    from wsorb.controllers._sources import load_math

    path_before, hook_before, modules_before = list(sys.path), sys.excepthook, set(sys.modules)
    for controller_id in IDS:
        create_controller(controller_id)
    assert sys.path == path_before and sys.excepthook is hook_before
    added = set(sys.modules) - modules_before
    assert not {"mvp", "config", "avoidance", "debris_tracker", "spaceteams", "cv2"} & added
    assert not any(name.startswith(("spaceteams.", "cv2.", "PIL.")) for name in added)
    for profile in ("bui", "tom"):
        for module in load_math(profile).values():
            assert module.__name__.startswith(f"wsorb.controllers._source_{profile}_bounded.")
            assert "examples" in Path(module.__file__).parts
            assert not any(
                word in module.__name__ for word in ("context", "display", "Controller", "UserAPI")
            )
    with pytest.raises(ValueError, match="Unknown reviewed"):
        load_math("../../Controller.py")


@pytest.mark.parametrize("controller_id", GENERIC_IDS)
def test_native_profiles_never_infer_missing_clock_or_sensor_provenance(controller_id):
    from dataclasses import fields

    from wsorb.control_contract import ControlSample

    native = sample_from_observation(observation(), source_kind="local_public_snapshot")
    numeric_only = ControlSample(
        **{field.name: getattr(native, field.name) for field in fields(ControlSample)}
    )
    controller = create_controller(controller_id)
    linear, angular = controller.command(
        numeric_only, observation()["radar"], now_elapsed_time_s=native.elapsed_time_s
    )
    assert controller.report["eligible"] is False
    np.testing.assert_array_equal(linear, np.zeros(3))
    np.testing.assert_array_equal(angular, np.zeros(3))


def test_catalog_import_is_lightweight_and_factory_has_no_arbitrary_script_settings():
    package = Path(str(files("wsorb.controllers")))
    tree = ast.parse((package / "__init__.py").read_text())
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    for node in top_level:
        names = (
            [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
        )
        assert not any(
            name.startswith(("numpy", "wsorb.checkpoint", "wsorb.experiment")) for name in names
        )
    for controller_id in IDS:
        with pytest.raises(ValueError, match="Unsupported settings"):
            create_controller(controller_id, {"script": "examples/tom-mvp/Controller.py"})
    with pytest.raises(ValueError, match="Unknown reviewed"):
        create_controller("examples/tom-mvp/Controller.py")
    with pytest.raises(ValueError):
        create_controller("basic_pd_radar", {"velocity_gain_s": True})


def test_absent_checkout_sources_fail_truthfully_without_substituting_math(tmp_path, monkeypatch):
    from wsorb.controllers import _sources

    # Filesystem availability only. No SDK, sensor, transport or physics call is replaced.
    monkeypatch.setattr(_sources, "ROOT", tmp_path)
    for profile in ("bui", "tom"):
        with pytest.raises(ValueError, match="requires the source checkout"):
            _sources.load_math(profile)
    with pytest.raises(ValueError, match="requires the source checkout"):
        _sources.require_template_source()
