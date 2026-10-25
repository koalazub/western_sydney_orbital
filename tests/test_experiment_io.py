"""Pure export schema/numeric contracts, not simulated flights or backend mocks."""

from __future__ import annotations

import csv
import hashlib
import io
import json

import numpy as np
import pytest

from wsorb.experiment_io import (
    HISTORY_COLUMNS,
    experiment_document,
    history_csv,
    json_bytes,
    json_safe,
    source_manifest,
)


def test_strict_json_preserves_missing_and_redacts_private_data_without_mutation():
    values = {
        "reading": np.float64(1.5),
        "missing": None,
        "bad": np.array([np.nan, np.inf, -np.inf]),
        "nested": {"Authorization": "private", "bearer_token": "private", "ok": 0},
        "connection_descriptor": {"url": "private", "token": "private"},
        "_client": object(),
    }
    assert json.loads(json_bytes(values)) == {
        "reading": 1.5,
        "missing": None,
        "bad": [None, None, None],
        "nested": {"ok": 0},
    }
    assert values["nested"]["Authorization"] == "private"
    assert "NaN" not in json_bytes(values).decode()


def test_export_rejects_clients_and_other_objects_instead_of_inspecting_them():
    with pytest.raises(TypeError, match="only public JSON-shaped data"):
        json_safe({"public": object()})
    with pytest.raises(TypeError, match="field names must be strings"):
        json_safe({1: 2})


def test_source_hashes_are_for_exact_supplied_bytes_and_explicit_labels():
    source = b"def policy():\n    return None\n"
    assert source_manifest({"saved notebook at export": source}) == [
        {"label": "saved notebook at export", "sha256": hashlib.sha256(source).hexdigest()}
    ]
    assert (
        source_manifest({"utf-8 source": "π\n"})[0]["sha256"]
        == hashlib.sha256("π\n".encode()).hexdigest()
    )


def test_csv_keeps_numeric_zero_and_negative_values_but_not_unavailable_or_private_values():
    data = history_csv(
        [
            {
                "telemetry_time_s": 2.0,
                "speed_mps": 0.0,
                "radial_climb_mps": -2.5,
                "damage_j_estimate": None,
                "frame": "=not-a-formula",
                "token": "private",
            }
        ]
    ).decode()
    rows = list(csv.DictReader(io.StringIO(data)))
    assert tuple(rows[0]) == HISTORY_COLUMNS
    assert rows[0]["speed_mps"] == "0.0"
    assert rows[0]["radial_climb_mps"] == "-2.5"
    assert rows[0]["damage_j_estimate"] == ""
    assert rows[0]["frame"] == "'=not-a-formula"
    assert "private" not in data
    assert len(list(csv.reader(io.StringIO(history_csv([]).decode())))) == 1


def test_launch_site_export_keeps_reported_pose_separate_from_a_draft_direction():
    pose = {"position": [14_000, 0, 0], "rotation": [0, 1, 0, 0]}
    row = {
        "observation_time_s": 2.0,
        "launch_site_changed": True,
        "manual_intervention": True,
        "launch_site_direction": [1, 0, 0],
        "launchpad": pose,
    }
    document = experiment_document(
        applied_config=None,
        draft_config={"surface_direction": [-1, 0, 0]},
        selected_policy="coast",
        schedule_s=(),
        history=[row],
        latest_snapshot={
            "observation": {
                "time_s": 2.0,
                "launch_site_direction": [1, 0, 0],
                "launchpad": pose,
            }
        },
        sources={},
    )
    assert document["observed_setup"]["launch_site_direction"] == [1, 0, 0]
    assert document["observed_setup"]["launchpad"] == pose
    assert document["draft_config"]["surface_direction"] == [-1, 0, 0]
    assert document["launch_site_changes"][0]["launchpad"] == pose
    exported = next(csv.DictReader(io.StringIO(history_csv([row]).decode())))
    assert json.loads(exported["launchpad"]) == pose
    assert json.loads(exported["launch_site_direction"]) == [1, 0, 0]


def test_experiment_document_separates_applied_draft_and_reported_world_settings():
    latest = {
        "observation": {
            "frame": "numeric-contract-frame",
            "radar_clock": "numeric-contract-clock",
            "experiment_settings": {"ship": {"mass_kg": 1200}},
        },
        "engine_build": {"sha256": "reported-engine-hash"},
        "connection": {"token": "private"},
        "client": object(),
    }
    document = experiment_document(
        applied_config={"controller": "coast", "gains": {"gain": 1}},
        draft_config={"controller": "planned", "gains": {"gain": 2}},
        selected_policy="planned",
        schedule_s=(0.05, 0.1),
        history=[{"telemetry_time_s": 2, "manual_intervention": True}],
        latest_snapshot=latest,
        sources={"saved numeric-contract source": "# test bytes\n"},
    )
    assert document["schema_version"] == 1
    assert document["applied_config"]["controller"] == "coast"
    assert document["draft_config"]["controller"] == "planned"
    assert document["applied_world_settings"] == {"ship": {"mass_kg": 1200}}
    assert document["engine_build"] == {"sha256": "reported-engine-hash"}
    assert document["frames_and_clocks"]["radar_clock"] == "numeric-contract-clock"
    assert document["claims"]["official_score"] is False
    assert document["claims"]["stp_physics_parity"] is False
    assert len(document["manual_interventions"]) == 1
    assert set(document["latest_public_snapshot"]) == {"observation", "engine_build"}
    assert "private" not in json_bytes(document).decode()


def test_empty_export_does_not_invent_a_run_clock_engine_or_measurements():
    document = experiment_document(
        applied_config=None,
        draft_config={},
        selected_policy="coast",
        schedule_s=(),
        history=[],
        latest_snapshot=None,
        sources={},
    )
    assert document["applied_config"] is None
    assert document["latest_public_snapshot"] is None
    assert document["engine_build"] is None
    assert document["latest_camera_status"] is None
    assert document["measured_history"] == []
    assert document["python_sources"] == []
    assert document["python_execution"] is None
    assert document["live_tuning"] == {
        "source_time_s": None,
        "source_seed": None,
        "tuning_revision": None,
        "ship_tuning": None,
        "debris_flow": None,
    }
    assert document["configuration_changes"] == []
    assert document["accepted_configuration"] is None
    assert document["applied_physics"] is None
    assert document["flight_context"]["playback"] == "unavailable"


def test_tuning_exports_actual_settings_and_held_telemetry_with_independent_clocks():
    ship = {"mass_kg": 1200.0, "inertia_scale": 1.5, "thrust_scale": 2.0}
    flow = {
        "speed_mps": 30.0,
        "speed_spread_mps": 5.0,
        "stream_axis": "z",
        "stream_sign": -1,
    }
    rows = [
        {
            "telemetry_time_s": 2.0,
            "observation_time_s": 2.5,
            "tuning_revision": 3,
            "tuning_changed": True,
            "telemetry_held": True,
            "ship_tuning": ship,
            "debris_flow": flow,
        }
    ]
    changes = [{"kind": "reported_tuning", "source_time_s": 2.5, "tuning_revision": 3}]
    document = experiment_document(
        applied_config={"controller": "planned_waypoint_pd"},
        draft_config={"controller": "coast"},
        selected_policy="coast",
        schedule_s=(),
        history=rows,
        latest_snapshot={
            "observation": {
                "time_s": 2.5,
                "seed": 12,
                "tuning_revision": 3,
                "ship_tuning": ship,
                "debris_flow": flow,
                "experiment_settings": {"ship": {"mass_kg": 1000.0}},
            }
        },
        sources={},
        configuration_changes=changes,
        execution_status={"accepted_policy_revision": 4, "last_command_policy_revision": 3},
    )
    assert document["live_tuning"] == {
        "source_time_s": 2.5,
        "source_seed": 12,
        "tuning_revision": 3,
        "ship_tuning": ship,
        "debris_flow": flow,
    }
    assert document["applied_world_settings"] == {"ship": {"mass_kg": 1000.0}}
    assert document["configuration_changes"] == changes
    assert document["python_execution"]["accepted_policy_revision"] == 4
    assert document["python_execution"]["last_command_policy_revision"] == 3
    exported = list(csv.DictReader(io.StringIO(history_csv(rows).decode())))[0]
    assert json.loads(exported["ship_tuning"]) == ship
    assert json.loads(exported["debris_flow"]) == flow
    assert exported["telemetry_time_s"] == "2.0"
    assert exported["observation_time_s"] == "2.5"
    assert exported["telemetry_held"] == "True"


def test_export_keeps_rigid_body_source_clock_and_accepted_hot_limits_separate_from_drafts():
    physics = {
        "source_time_s": 12.125,
        "tuning_revision": 4,
        "frame": "numeric-body-frame",
        "mass_kg": 99.5,
        "total_external_force_world_n": [0.0, 0.0, -0.1],
        "actuation_state": "awaiting_application",
        "last_actuator_application": {
            "application_time_s": 11.5,
            "tuning_revision": 3,
            "force_body_n": [0.2, 0.0, 0.0],
        },
    }
    execution = {
        "accepted_policy_revision": 3,
        "last_command_policy_revision": 2,
        "accepted_run_limits_revision": 5,
        "run_limits": {"duration_s": 12.0, "requested_cadence_hz": 4.0},
        "run_origin_time_s": 5.0,
    }
    rows = [
        {
            "telemetry_time_s": 12.0,
            "observation_time_s": 12.25,
            "applied_physics_time_s": 12.125,
            "rapier_mass_kg": 99.5,
            "actuator_application_time_s": 11.5,
            "actuator_application_tuning_revision": 3,
            "actuator_force_body_x_n": 0.2,
            "applied_physics": physics,
            "accepted_policy_revision": 3,
            "accepted_run_limits_revision": 5,
            "run_limits": execution["run_limits"],
            "configuration_changed": True,
        }
    ]
    document = experiment_document(
        applied_config={"controller": "planned_waypoint_pd"},
        draft_config={"duration_s": 30, "cadence_hz": 2},
        selected_policy="planned_waypoint_pd",
        schedule_s=(),
        history=rows,
        latest_snapshot={
            "observation": {
                "time_s": 12.25,
                "ship_tuning": {"mass_kg": 100.0, "inertia_scale": 1.0, "thrust_scale": 3.2},
                "applied_physics": physics,
                "mission": {"status": "success", "phase": "arrived"},
            },
            "runtime": {"running": False},
        },
        sources={},
        execution_status=execution,
    )
    assert document["applied_physics"] == physics
    assert document["accepted_configuration"]["run_limits_revision"] == 5
    assert document["accepted_configuration"]["run_origin_time_s"] == 5.0
    assert document["accepted_configuration"]["run_limits"]["duration_s"] == 12.0
    assert document["draft_config"]["duration_s"] == 30
    assert document["flight_context"]["playback"] == "paused"
    assert "Reset flight" in document["flight_context"]["terminal_notice"]
    assert document["flight_context"]["applied_physics"]["source_time_s"] == 12.125
    csv_row = list(csv.DictReader(io.StringIO(history_csv(rows).decode())))[0]
    assert json.loads(csv_row["applied_physics"]) == physics
    assert csv_row["applied_physics_time_s"] == "12.125"
    assert csv_row["actuator_application_time_s"] == "11.5"
    assert csv_row["actuator_application_tuning_revision"] == "3"
    assert csv_row["actuator_force_body_x_n"] == "0.2"
    assert csv_row["rapier_mass_kg"] == "99.5"
    assert csv_row["telemetry_time_s"] == "12.0"
    assert csv_row["observation_time_s"] == "12.25"
    assert csv_row["accepted_run_limits_revision"] == "5"
    assert json.loads(csv_row["run_limits"]) == execution["run_limits"]


def test_setup_distribution_trial_and_recorded_placement_remain_actual_not_draft_values():
    distribution = {
        "kind": "impact_ejecta",
        "settings": {
            "source_direction": [-1, 0, 0],
            "kick_velocity_mps": [-1, 0, 0],
            "tangential_speed_mps": 7,
            "patch_half_angle_deg": 15,
        },
    }
    row = {
        "segment": 2,
        "trial_revision": 5,
        "flight_preset": "mars_facing_l1",
        "debris_distribution": distribution,
        "recorded_placement": True,
        "setup_changed": True,
        "manual_intervention": True,
        "telemetry_time_s": 7.0,
        "observation_time_s": 7.0,
        "rapier_position_world_x_m": 14_000.0,
    }
    document = experiment_document(
        applied_config=None,
        draft_config={"flight_preset": "far_side_l2", "placement_xyz_draft_m": {"x": 15_000}},
        selected_policy="planned_waypoint_pd",
        schedule_s=(),
        history=[row],
        sources={},
        latest_snapshot={
            "observation": {
                "time_s": 7.0,
                "trial_revision": 5,
                "flight_preset": "mars_facing_l1",
                "debris_distribution": distribution,
            }
        },
    )
    assert document["observed_setup"]["trial_revision"] == 5
    assert document["observed_setup"]["flight_preset"] == "mars_facing_l1"
    assert document["observed_setup"]["debris_distribution"] == distribution
    assert document["draft_config"]["flight_preset"] == "far_side_l2"
    assert document["recorded_placements"][0]["rapier_position_world_x_m"] == 14_000.0
    csv_row = list(csv.DictReader(io.StringIO(history_csv([row]).decode())))[0]
    assert csv_row["trial_revision"] == "5"
    assert json.loads(csv_row["debris_distribution"]) == distribution
    assert csv_row["observation_time_s"] == "7.0"
