"""Constructed numeric regressions, not observed sensors or flight-safety evidence."""

from __future__ import annotations

import copy
import json
import math
from dataclasses import replace

import numpy as np
import pytest

from test_flight import numeric_checked_route, numeric_unit, resilient_numeric_input
from wsorb.flight import (
    PlannerConfig,
    ResilienceConfig,
    ResilientGuidance,
    local_motion,
    plan_route,
)
from wsorb.flight.estimation import NavigationSample, StateEstimator
from wsorb.flight.local_motion import (
    LocalMotionPlanner,
    RadarMotionHistory,
    stopping_motion,
    temporal_obstacles,
)
from wsorb.flight.official_profile import (
    OFFICIAL_BEELINE_SPEED_MPS,
    official_profile,
)
from wsorb.flight.prediction import predict_obstacles
from wsorb.flight.temporal import TemporalObstacles, motion_clear

ZERO = (0.0, 0.0, 0.0)


def transit_numeric_input(time_s=10.0, sequence=100):
    observation = resilient_numeric_input(time_s, sequence)
    observation["debris_distribution"] = {"kind": "transit_stream"}
    observation["mission"]["status"] = "running"
    observation["shuttle"]["position"] = [2000.0, 0.0, 0.0]
    observation["target"]["position"] = [10000.0, 0.0, 0.0]
    observation["radar"] = numeric_rows(1, time_s)
    observation["radar_backup"] = numeric_rows(2, time_s)
    return observation


def official_numeric_input(time_s=10.0, sequence=100):
    """Constructed official-profile inputs; not the real competition cloud."""
    observation = transit_numeric_input(time_s, sequence)
    observation["flight_preset"] = "competition_reference"
    observation["debris_distribution"] = {"kind": "official_cloud"}
    observation["shuttle"]["position"] = [2000.0, 0.0, 0.0]
    observation["target"]["position"] = [3305.8, 0.0, 0.0]
    observation["radar"] = numeric_rows(1, time_s)
    observation["radar_backup"] = numeric_rows(2, time_s)
    return observation


def numeric_rows(count, time_s):
    """Anonymous mathematical rows, deliberately not persistent object identities."""
    index = np.arange(count)
    rows = np.zeros((count, 3, 3))
    rows[:, 0, 0] = 500.0 + 25.0 * (index % 7)
    rows[:, 0, 1] = 3000.0 + 10.0 * (index % 17)
    rows[:, 0, 2] = -200.0 + 40.0 * (index % 11)
    rows[:, 1, 0] = np.linalg.norm(rows[:, 0], axis=1) - 1.0
    rows[:, 1, 1] = -3.0 + 0.5 * (index % 13)
    rows[:, 1, 2] = 2.0 + 0.1 * (index % 5)
    rows[:, 2, 0] = time_s
    return rows.tolist()


def assert_coast(command):
    acceleration, angular = command
    np.testing.assert_array_equal(acceleration, ZERO)
    np.testing.assert_array_equal(angular, ZERO)


def checked_advisory_context():
    # Register complete frozen geometry before enabling the local strategy.
    # All 500 constructed rows are supplied here to isolate advisory progress
    # from the separate partial-scan heading policy, not to emulate sensor reach.
    observation = transit_numeric_input()
    observation["radar"] = numeric_rows(500, 10.0)
    distribution = observation.pop("debris_distribution")
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    advisory = numeric_checked_route(guidance, receipt)
    assert advisory.status == "candidate"
    observation["debris_distribution"] = distribution
    assert guidance.update(observation, wall_time_s=0.1, running=False).can_command
    return observation, guidance, advisory


@pytest.mark.parametrize("history", ["held_navigation", "new_navigation", "no_radar_history"])
def test_temporal_radii_match_full_frozen_forecasts_for_known_bodies_and_all_rows(history):
    observation = transit_numeric_input()
    observation["shuttle"]["velocity"] = [2.0, 3.0, -1.0]
    radar_time = 9.8 if history == "no_radar_history" else 10.0
    observation["radar"] = numeric_rows(4096, radar_time)
    numeric_unit(observation, "radar_primary")["sample_time_s"] = radar_time
    guidance = ResilientGuidance()
    assert guidance.update(observation, wall_time_s=0.0, running=False).can_command
    if history == "new_navigation":
        current = transit_numeric_input(10.2, 102)
        current["shuttle"].update(position=[2001.0, 0.5, -0.2], velocity=[3.0, 4.0, -2.0])
        current["radar"] = copy.deepcopy(observation["radar"])
        numeric_unit(current, "radar_primary").update(sample_time_s=radar_time, sequence=100)
        assert guidance.update(current, wall_time_s=0.2, running=False).can_command
        observation = current
    held = copy.deepcopy(observation)
    held["time_s"] += 0.3
    receipt = guidance.update(held, wall_time_s=0.5, running=False)
    assert receipt.can_command
    selected = receipt.observation
    before = copy.deepcopy(selected)
    temporal = temporal_obstacles(selected, guidance._estimator, guidance.config)
    assert temporal.centers_m.shape == (4099, 3)
    assert receipt.report["estimate"]["source_time_s"] == held["navigation_sample_time_s"]
    assert receipt.report["ownship_history_samples"] == (2 if history == "new_navigation" else 1)
    rows = np.asarray(selected["radar"])
    raw_position = np.asarray(selected["shuttle"]["position"])
    raw_velocity = np.asarray(selected["shuttle"]["velocity"])
    age = held["time_s"] - rows[:, 2, 0]
    directions = rows[:, 0] / np.linalg.norm(rows[:, 0], axis=1)[:, None]
    expected_velocity = directions * (rows[:, 1, 1] + directions @ raw_velocity)[:, None]
    expected_center = raw_position - age[:, None] * raw_velocity + rows[:, 0]
    expected_center += age[:, None] * expected_velocity
    np.testing.assert_allclose(temporal.centers_m[3:], expected_center, rtol=1e-13)
    np.testing.assert_allclose(temporal.velocities_mps[3:], expected_velocity, rtol=1e-13)
    np.testing.assert_array_equal(temporal.velocities_mps[:3], np.zeros((3, 3)))
    for horizon in (0.0, 0.75, 6.0, 40.0):
        settings = replace(guidance.config.planner, forecast_horizon_s=horizon)
        frozen = guidance._inflate(predict_obstacles(selected, settings))
        radii = temporal.radii_m + temporal.growth_mps * horizon
        radii += temporal.growth_mps2 * horizon**2
        np.testing.assert_allclose(
            radii, [obstacle.radius_m for obstacle in frozen.obstacles], rtol=1e-13, atol=1e-10
        )
        np.testing.assert_allclose(
            temporal.centers_m, [obstacle.from_m for obstacle in frozen.obstacles], rtol=1e-13
        )
        np.testing.assert_allclose(
            temporal.centers_m + temporal.velocities_mps * horizon,
            [obstacle.to_m for obstacle in frozen.obstacles],
            rtol=1e-13,
        )
    # Reordering even repeated anonymous readings only reorders their envelopes.
    selected["radar"].reverse()
    reversed_rows = temporal_obstacles(selected, guidance._estimator, guidance.config)
    for name in ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
        np.testing.assert_array_equal(
            getattr(reversed_rows, name)[3:], getattr(temporal, name)[3:][::-1]
        )
    assert receipt.observation == before
    assert held["radar"] == before["radar"]


@pytest.mark.parametrize("assumed_noise", [(0.0, 0.0, 0.0), (8.0, 0.7, 0.2)])
def test_public_noise_is_a_floor_and_diameter_allowance_uses_the_inverse(assumed_noise):
    observation = transit_numeric_input()
    observation["radar"] = [[[0.0, 3000.0, 0.0], [2990.0, 0.0, 20.0], [9.7, 0.0, 0.0]]]
    public_noise = (5.0, 0.4, 0.1)
    names = ("range_noise_m", "range_rate_noise_mps", "diameter_noise_fraction")
    observation["sensor_model"] = dict(zip(names, public_noise, strict=True))
    settings = replace(PlannerConfig(), **dict(zip(names, assumed_noise, strict=True)))
    config = ResilienceConfig(planner=settings)
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    obstacles = temporal_obstacles(observation, estimator, config)
    range_noise, rate_noise, diameter_fraction = map(max, assumed_noise, public_noise)
    age = 0.3
    sigma = settings.sigma_multiplier
    observer = max(settings.observer_accel_allowance_mps2, observation["max_accel_mps2"])
    growth = settings.unknown_tangential_speed_mps + sigma * rate_noise + observer * age
    diameter_radius = 10.0 / (1.0 - sigma * diameter_fraction)
    assert diameter_radius > 10.0 * (1.0 + sigma * diameter_fraction)
    for horizon in (0.0, 6.0):
        expected = diameter_radius + observation["shuttle"]["radius_m"] + settings.clearance_m
        expected += sigma * range_noise + 0.5 * observer * age**2 + growth * (age + horizon)
        expected += config.position_allowance_m + config.velocity_allowance_mps * horizon
        expected += 0.5 * estimator.accel_allowance_mps2 * horizon**2
        actual = obstacles.radii_m[-1] + obstacles.growth_mps[-1] * horizon
        actual += obstacles.growth_mps2[-1] * horizon**2
        assert actual == pytest.approx(expected)


def test_public_diameter_noise_cannot_be_hidden_by_smaller_configured_noise():
    observation = transit_numeric_input()
    observation["sensor_model"]["diameter_noise_fraction"] = 1.0 / 3.0
    config = ResilienceConfig(planner=replace(PlannerConfig(), diameter_noise_fraction=0.0))
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    with pytest.raises(ValueError, match="diameter_noise_fraction"):
        temporal_obstacles(observation, estimator, config)


@pytest.mark.parametrize("channel", ["radar_primary", "radar_backup"])
def test_every_selected_row_is_checked_including_a_last_row_that_alone_blocks(channel):
    key = "radar" if channel == "radar_primary" else "radar_backup"
    observation = transit_numeric_input()
    observation[key] = numeric_rows(4096, 10.0)
    if channel == "radar_backup":
        numeric_unit(observation, "radar_primary")["delivery"] = "unavailable"
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.report["selected_channel"] == channel
    assert math.hypot(*guidance.command(observation, None, {})[0]) > 0.0
    assert guidance.report["checked_radar_rows"] == 4096
    current = transit_numeric_input(10.1, 101)
    current[key] = numeric_rows(4096, 10.1)
    current[key][-1][:2] = [[1.0, 0.0, 0.0], [1.0, 0.0, 2.0]]
    if channel == "radar_backup":
        numeric_unit(current, "radar_primary")["delivery"] = "unavailable"
    receipt = guidance.update(current, wall_time_s=0.1, running=False)
    assert receipt.can_command and receipt.observation["radar"] == current[key]
    assert_coast(guidance.command(current, None, {}))
    assert guidance.report["checked_radar_rows"] == 4096
    assert guidance.report["admissible_local_candidates"] == 0


@pytest.mark.parametrize("count", [1, 200])
@pytest.mark.parametrize("channel", ["radar_primary", "radar_backup"])
def test_local_report_and_preview_keep_the_unobserved_debris_lower_bound(count, channel):
    observation = transit_numeric_input()
    key = "radar" if channel == "radar_primary" else "radar_backup"
    observation[key] = numeric_rows(count, 10.0)
    if channel == "radar_backup":
        numeric_unit(observation, "radar_primary")["delivery"] = "unavailable"
    assert observation["geometry"]["debris_count"] == 500
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    frame = predict_obstacles(receipt.observation, guidance.config.planner)
    preview = plan_route(replace(frame, local_motion_only=True), frame.config).to_preview()
    assert preview["measured_obstacles"] == count
    assert preview["unmodelled_obstacles_lower_bound"] == 500 - count
    assert math.hypot(*guidance.command(observation, None, {})[0]) > 0.0
    assert guidance.report["checked_radar_rows"] == count
    assert guidance.report["unmodelled_obstacles_lower_bound"] == 500 - count


@pytest.mark.parametrize(
    "distribution, local_enabled",
    [
        (None, False),
        ({"kind": "broad_stream"}, False),
        ("transit_stream", False),
        ({"kind": "transit_stream"}, True),
    ],
)
def test_only_explicit_public_transit_distribution_allows_commands_without_a_route(
    distribution, local_enabled
):
    observation = transit_numeric_input()
    if distribution is None:
        del observation["debris_distribution"]
    else:
        observation["debris_distribution"] = distribution
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    command = guidance.command(observation, None, {})
    if local_enabled:
        assert 0.0 < math.hypot(*command[0]) <= 0.5
        assert guidance.report["guidance_strategy"] == "time_indexed_local_motion"
        assert guidance.report["decision_valid_until_s"] == 10.75
        assert guidance.report["braking_model_kind"] == "assumed_active_braking_not_fault_coasting"
    else:
        assert_coast(command)
    assert observation == before


def test_official_preset_selects_the_official_profile_while_transit_keeps_its_stop_gate():
    official = official_numeric_input()
    assert official_profile(official)
    assert not official_profile(transit_numeric_input())
    transit_on_official_world = official_numeric_input()
    transit_on_official_world["debris_distribution"] = {"kind": "transit_stream"}
    guidance = ResilientGuidance()
    receipt = guidance.update(transit_on_official_world, wall_time_s=0.0, running=False)
    assert receipt.can_command
    assert guidance.report["guidance_strategy"] == "time_indexed_local_motion"
    assert "modeled stop" in guidance.report["command_gate"]
    assert guidance.report.get("official_stop_required") is None


def test_official_profile_commands_the_published_ten_metre_per_second_beeline():
    observation = official_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.lease_sim_s == 0.75
    acceleration, angular = guidance.command(observation, None, {})
    report = guidance.report
    assert report["guidance_strategy"] == "official_competition_beeline"
    assert report["official_stop_required"] is False
    assert report["official_stop_certificate"] is False
    assert (
        report["command_gate"]
        == "fresh_rolling_radar_publication_current_lease_sweep_without_stop_certificate"
    )
    assert report["official_beeline_speed_mps"] == OFFICIAL_BEELINE_SPEED_MPS
    assert report["desired_speed_mps"] == pytest.approx(OFFICIAL_BEELINE_SPEED_MPS)
    assert report["nominal_motion_used"] is False
    assert report["nominal_motion_pair_intervals"] == 0
    assert 0.499 < math.hypot(*acceleration) <= 0.5
    assert acceleration[0] > 0.0
    np.testing.assert_array_equal(angular, ZERO)


def test_official_profile_uses_genuine_radar_for_lateral_avoidance():
    observation = official_numeric_input()
    observation["radar"] = [[[100.0, 0.0, 0.0], [99.0, 0.0, 10.0], [10.0, 0.0, 0.0]]]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    acceleration, _ = guidance.command(observation, None, {})
    assert guidance.report["avoidance_rows"] == 1
    assert guidance.report["avoidance_risk"] > 0.0
    assert abs(acceleration[2]) > 0.1
    assert math.hypot(*acceleration) <= 0.5


def test_official_profile_coasts_when_no_current_lease_radar_clear_command_exists():
    observation = official_numeric_input()
    observation["radar"] = [[[1.0, 0.0, 0.0], [0.5, 0.0, 10.0], [10.0, 0.0, 0.0]]]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    acceleration, angular = guidance.command(observation, None, {})
    np.testing.assert_array_equal(acceleration, ZERO)
    np.testing.assert_array_equal(angular, ZERO)
    assert guidance.report["current_lease_clear"] is False
    assert guidance.report["admissible_local_candidates"] == 0
    assert guidance.report["official_stop_required"] is False
    assert "No official command is clear" in guidance.report["reason"]


def test_official_profile_aligns_body_plus_x_with_bounded_angular_acceleration():
    observation = official_numeric_input()
    observation["shuttle"]["rotation"] = [0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)]
    observation["shuttle"]["angular_velocity_rad_s"] = [0.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    acceleration, angular = guidance.command(observation, None, {})
    assert guidance.report["desired_velocity_world_mps"][0] > 0.0
    assert math.hypot(*acceleration) <= 0.5
    assert angular[2] < 0.0
    assert math.hypot(*angular) <= 0.05
    assert guidance.report["attitude_model"].startswith("shortest_arc_body_plus_x")


def test_official_rolling_radar_accepts_per_row_times_without_relaxing_transit_freshness():
    observation = official_numeric_input()
    observation["radar"] = numeric_rows(3, 10.0)
    observation["radar"][0][2][0] = 8.8
    observation["radar"][1][2][0] = 9.4
    observation["radar"][2][2][0] = 10.0
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.lease_sim_s == 0.75
    acceleration, _ = guidance.command(observation, None, {})
    assert math.hypot(*acceleration) > 0.0
    assert guidance.report["radar_model"].startswith("genuine_rolling_observed_rows")
    assert guidance.report["checked_radar_rows"] == 3
    transit = copy.deepcopy(observation)
    transit["debris_distribution"] = {"kind": "transit_stream"}
    transit["radar_backup"] = copy.deepcopy(transit["radar"])
    receipt = ResilientGuidance().update(transit, wall_time_s=0.0, running=False)
    assert not receipt.can_command and receipt.lease_sim_s == 0.0


def test_official_profile_keeps_freshness_consent_and_lease_gates():
    observation = official_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.lease_sim_s == 0.75
    assert math.hypot(*guidance.command(observation, None, {})[0]) > 0.0
    changed = copy.deepcopy(observation)
    changed["time_s"] += 0.1
    assert_coast(guidance.command(changed, None, {}))
    assert "latest public source context" in guidance.report["reason"]
    manual = copy.deepcopy(observation)
    manual["manual_interventions"] = 1
    receipt = guidance.update(manual, wall_time_s=0.1, running=False)
    assert not receipt.can_command and receipt.report["requires_consent"]
    assert_coast(guidance.command(manual, None, {}))
    failed = copy.deepcopy(observation)
    failed["sensor_health"]["revision"] += 1
    numeric_unit(failed, "radar_primary")["active_faults"] = ["radar_bus"]
    receipt = ResilientGuidance().update(failed, wall_time_s=0.0, running=False)
    assert not receipt.can_command and receipt.lease_sim_s == 0.0


def test_default_local_gains_remain_point0012_and_point07():
    observation = transit_numeric_input()
    observation["target"]["position"] = [2049.1, 0.0, 0.0]
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=False)
    default = guidance.command(observation, None, {})
    explicit = guidance.command(
        observation, None, {"position_gain_s2": 0.0012, "velocity_gain_s": 0.07}
    )
    np.testing.assert_array_equal(default[0], explicit[0])
    assert math.hypot(*default[0]) == pytest.approx(0.0012 * 0.1)
    assert guidance.report["desired_speed_mps"] == pytest.approx(0.0012 * 0.1 / 0.07)
    assert_coast(guidance.command(observation, None, {"position_gain_s2": 0.0}))
    assert_coast(guidance.command(observation, None, {"velocity_gain_s": 0.0}))


def test_partial_scan_escape_prioritizes_sideways_departure_over_a_corridor_length_radial_leg():
    observation = transit_numeric_input()
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=False)
    acceleration, _ = guidance.command(observation, None, {})
    assert 0.0 < math.hypot(*acceleration) <= 0.5
    planner = guidance._local_motion
    delta = planner.traffic_bypass[0] - observation["shuttle"]["position"]
    outward = float(np.dot(delta, planner.anchor))
    sideways = delta - outward * planner.anchor
    assert outward == pytest.approx(guidance.config.planner.cell_size_m)
    assert math.hypot(*sideways) == pytest.approx(guidance.config.planner.corridor_half_width_m)
    assert sideways[1] > 0.0
    assert guidance.report["decision_valid_until_s"] == 10.75
    assert (
        guidance.report["global_route_kind"]
        == "advisory_geometry_only_not_a_complete_current_route"
    )


def test_current_spatial_advice_does_not_turn_future_stop_growth_into_a_blocked_start():
    observation = transit_numeric_input()
    observation["shuttle"]["position"] = [1030.0, 0.0, 0.0]
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    guidance = ResilientGuidance()
    ready = guidance.update(observation, wall_time_s=0.0, running=False)
    raw = temporal_obstacles(ready.observation, guidance._estimator, guidance.config)
    assert raw.radii_m[0] < 1030.0
    acceleration, _ = guidance.command(observation, None, {})
    assert ready.can_command and ready.lease_sim_s == 0.75
    assert guidance.report["nominal_route_status"] == "local_advice"
    assert 0.0 < math.hypot(*acceleration) <= 0.5
    cruise_stop_horizon = (
        0.75
        + (guidance.config.max_speed_mps + ready.report["estimate"]["velocity_uncertainty_mps"])
        / guidance.report["braking_model_accel_mps2"]
    )
    future_radius = raw.radii_m[0] + raw.growth_mps[0] * cruise_stop_horizon
    future_radius += raw.growth_mps2[0] * cruise_stop_horizon**2
    assert future_radius > math.hypot(*observation["shuttle"]["position"])
    segments = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        acceleration,
        0.75,
        guidance.report["braking_model_accel_mps2"],
        ready.report["estimate"]["velocity_uncertainty_mps"],
    )
    assert motion_clear(segments, raw)


@pytest.mark.parametrize("geometry_budget_available", [True, False])
def test_spatial_and_motion_advice_keep_the_current_full_stop_gate(geometry_budget_available):
    observation = transit_numeric_input()
    observation["shuttle"]["position"] = [1030.0, 0.0, 0.0]
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    observation["debris_flow"] = {
        "stream_axis": "x",
        "stream_sign": 1,
        "speed_mps": 20.0,
        "speed_spread_mps": 4.0,
    }
    relative = np.array((-300.0, 20.0, 0.0))
    distance = math.hypot(*relative)
    observation["radar"] = [
        [relative.tolist(), [distance - 10.0, -6000.0 / distance, 20.0], [10.0, 0.0, 0.0]]
    ]
    # Exceed only the adviser's capsule-index budget, not its all-row safety
    # input. These distant mathematical rows create no extra flow encounter.
    if not geometry_budget_available:
        observation["radar"].extend(
            [[[-100.0, 200.0, 0.0], [math.sqrt(50000.0) - 1.0, 0.0, 2.0], [10.0, 0.0, 0.0]]] * 129
        )
    guidance = ResilientGuidance()
    ready = guidance.update(observation, wall_time_s=0.0, running=False)
    raw = temporal_obstacles(ready.observation, guidance._estimator, guidance.config)
    acceleration, _ = guidance.command(observation, None, {})
    assert guidance.report["nominal_route_status"] == (
        "local_advice" if geometry_budget_available else "no_nominal_path"
    )
    assert guidance.report["checked_radar_rows"] == (1 if geometry_budget_available else 130)
    assert guidance.report["advisory_flow_encounters"] == 1
    assert 0.0 < math.hypot(*acceleration) <= 0.5
    assert guidance.report["admissible_local_candidates"] >= 1
    if guidance.report["nominal_motion_used"]:
        assert guidance.report["nominal_motion_proposal_gap_m"] > 0.0
        assert guidance.report["selected_nominal_motion_cost"][0] == 0.0
    else:
        assert guidance.report["selected_nominal_motion_cost"] is None
    segments = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        acceleration,
        0.75,
        guidance.report["braking_model_accel_mps2"],
        ready.report["estimate"]["velocity_uncertainty_mps"],
    )
    assert motion_clear(segments, raw)


@pytest.mark.parametrize(
    "change",
    [
        "time",
        "navigation_clock",
        "position",
        "velocity",
        "rotation",
        "manual",
        "trial",
        "tuning",
        "sensor_revision",
        "frame",
        "radar_clock",
    ],
)
def test_local_command_keeps_the_latest_source_context_signature_gate(change):
    observation = transit_numeric_input()
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=False)
    assert math.hypot(*guidance.command(observation, None, {})[0]) > 0.0
    changed = copy.deepcopy(observation)
    if change == "time":
        changed["time_s"] += 0.1
    elif change == "navigation_clock":
        changed["navigation_sample_time_s"] -= 0.1
    elif change in ("position", "velocity", "rotation"):
        changed["shuttle"][change][0] += 1.0
    elif change == "sensor_revision":
        changed["sensor_health"]["revision"] += 1
    elif change in ("frame", "radar_clock"):
        changed[change] = "another constructed clock or frame"
    else:
        name = {
            "manual": "manual_interventions",
            "trial": "trial_revision",
            "tuning": "tuning_revision",
        }[change]
        changed[name] += 1
    assert_coast(guidance.command(changed, None, {}))
    assert "latest public source context" in guidance.report["reason"] or change == "radar_clock"


@pytest.mark.parametrize("source", ["navigation", "radar_primary"])
@pytest.mark.parametrize("change", ["content", "sequence_only", "time_only"])
def test_local_guidance_rejects_source_mutation_without_both_advancing_clocks(source, change):
    observation = transit_numeric_input()
    numeric_unit(observation, "radar_backup")["delivery"] = "unavailable"
    guidance = ResilientGuidance()
    assert guidance.update(observation, wall_time_s=0.0, running=False).can_command
    changed = copy.deepcopy(observation)
    changed["time_s"] = 10.1
    unit = numeric_unit(changed, source)
    if change == "sequence_only":
        unit["sequence"] += 1
    elif change == "time_only":
        unit["sample_time_s"] = 10.1
        if source == "navigation":
            changed["navigation_sample_time_s"] = 10.1
        else:
            changed["radar"][0][2][0] = 10.1
    elif source == "navigation":
        changed["shuttle"]["position"][0] += 1.0
    else:
        changed["radar"][0][0][0] += 1.0
    receipt = guidance.update(changed, wall_time_s=0.1, running=False)
    assert not receipt.can_command and receipt.lease_sim_s == 0.0
    assert not receipt.report["assessments"][source]["usable"]
    assert_coast(guidance.command(changed, None, {}))


@pytest.mark.parametrize("fault", ["radar_bus", "all_avionics", "navigation"])
def test_local_sensor_loss_coasts_instead_of_reusing_a_future_braking_plan(fault):
    observation = transit_numeric_input()
    observation["shuttle"]["velocity"] = [2.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=False)
    assert math.hypot(*guidance.command(observation, None, {})[0]) > 0.0
    failed = copy.deepcopy(observation)
    failed["sensor_health"]["revision"] += 1
    identity = "navigation" if fault == "navigation" else "radar_primary"
    numeric_unit(failed, identity)["active_faults"] = [fault]
    receipt = guidance.update(failed, wall_time_s=0.1, running=False)
    assert not receipt.can_command and receipt.lease_sim_s == 0.0
    assert receipt.observation["radar"] == []
    assert_coast(guidance.command(failed, None, {}))
    assert guidance.report["decision"] == "zero_thrust_not_braking_or_guaranteed_avoidance"


def test_manual_changes_latch_consent_despite_fresh_transit_samples():
    guidance = ResilientGuidance()
    initial = transit_numeric_input()
    guidance.update(initial, wall_time_s=0.0, running=False)
    assert math.hypot(*guidance.command(initial, None, {})[0]) > 0.0
    for index, timestamp in enumerate((10.1, 10.3, 10.9, 11.2), start=1):
        changed = transit_numeric_input(timestamp, 100 + index)
        changed["manual_interventions"] = 1
        receipt = guidance.update(changed, wall_time_s=float(index), running=False)
        assert not receipt.can_command and receipt.report["requires_consent"]
        assert_coast(guidance.command(changed, None, {}))
    consented = ResilientGuidance()
    assert consented.update(changed, wall_time_s=5.0, running=False).can_command
    assert math.hypot(*consented.command(changed, None, {})[0]) > 0.0


@pytest.mark.parametrize("held_source", ["navigation", "radar_primary"])
def test_local_recovery_requires_advancing_genuine_navigation_and_radar_samples(held_source):
    guidance = ResilientGuidance(ResilienceConfig(recovery_samples=3, recovery_dwell_s=0.5))
    initial = transit_numeric_input()
    guidance.update(initial, wall_time_s=0.0, running=False)
    failed = copy.deepcopy(initial)
    failed["sensor_health"]["revision"] = 1
    numeric_unit(failed, "radar_primary")["active_faults"] = ["radar_bus"]
    assert not guidance.update(failed, wall_time_s=0.1, running=False).can_command
    first = transit_numeric_input(10.1, 101)
    first["sensor_health"]["revision"] = 2
    for wall in (0.2, 0.3, 0.4):
        receipt = guidance.update(first, wall_time_s=wall, running=False)
        assert not receipt.can_command and receipt.report["recovery_samples"] == 1
        assert_coast(guidance.command(first, None, {}))
    one_clock = transit_numeric_input(10.2, 102)
    one_clock["sensor_health"]["revision"] = 2
    numeric_unit(one_clock, held_source).update(sample_time_s=10.1, sequence=101)
    if held_source == "navigation":
        one_clock["navigation_sample_time_s"] = 10.1
    else:
        one_clock["radar"] = copy.deepcopy(first["radar"])
    receipt = guidance.update(one_clock, wall_time_s=0.5, running=False)
    assert not receipt.can_command and receipt.report["recovery_samples"] == 1
    for index, timestamp in enumerate((10.3, 10.8), start=2):
        current = transit_numeric_input(timestamp, 101 + index)
        current["sensor_health"]["revision"] = 2
        receipt = guidance.update(current, wall_time_s=0.4 + index * 0.1, running=False)
        assert receipt.report["recovery_samples"] == index
        assert receipt.can_command is (index == 3)
        command = guidance.command(current, None, {})
        if index == 3:
            assert math.hypot(*command[0]) > 0.0
            assert guidance.report["source_time_s"] == timestamp
        else:
            assert_coast(command)


@pytest.mark.parametrize(
    "speed, acceleration, public_cap", [(3.0, 0.25, 0.5), (1.2, 0.1, 0.5), (0.4, 0.05, 0.04)]
)
def test_backup_uses_copied_actual_rows_noise_and_configured_reduced_limits(
    speed, acceleration, public_cap
):
    config = ResilienceConfig(degraded_max_speed_mps=speed, degraded_max_accel_mps2=acceleration)
    observation = transit_numeric_input()
    observation["max_accel_mps2"] = public_cap
    observation["radar"][0][:2] = [[1.0, 0.0, 0.0], [1.0, 0.0, 2.0]]
    observation["radar_backup"] = numeric_rows(31, 10.0)
    numeric_unit(observation, "radar_primary")["delivery"] = "unavailable"
    noise = {"range_noise_m": 5.0, "range_rate_noise_mps": 0.08, "diameter_noise_fraction": 0.08}
    numeric_unit(observation, "radar_backup")["noise"] = noise
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance(config)
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.report["selected_channel"] == "radar_backup"
    assert receipt.observation["radar"] == observation["radar_backup"]
    assert receipt.observation["sensor_model"] == noise
    copied = receipt.observation
    copied["radar"][-1][0][0] = 1e9
    copied["sensor_model"]["range_noise_m"] = 0.0
    assert receipt.observation["radar"] == before["radar_backup"]
    assert receipt.observation["sensor_model"] == noise
    acceleration_vector, angular = guidance.command(observation, None, {})
    assert 0.0 < math.hypot(*acceleration_vector) <= min(acceleration, public_cap)
    np.testing.assert_array_equal(angular, ZERO)
    report = guidance.report
    assert report["mode"] == "degraded" and report["checked_radar_rows"] == 31
    assert report["configured_speed_limit_mps"] == speed <= 3.0
    assert report["desired_speed_mps"] <= speed
    assert report["reference_accel_limit_mps2"] == min(acceleration, public_cap) <= 0.25
    assert receipt.lease_sim_s == config.lease_sim_s
    assert observation == before


def test_stopping_motion_keeps_the_whole_lease_brake_and_velocity_reserve():
    position = np.array([2000.0, 4.0, -2.0])
    velocity = np.array([5.0, 2.0, 0.0])
    acceleration = np.array([0.1, -0.2, 0.0])
    lease, braking, allowance = 0.75, 0.25, 0.05
    segments = stopping_motion(position, velocity, acceleration, lease, braking, allowance)
    assert len(segments) == 3
    first, brake, reserve = segments
    assert first.duration_s == lease
    end_position = position + velocity * lease + 0.5 * acceleration * lease**2
    end_velocity = velocity + acceleration * lease
    np.testing.assert_allclose(brake.position_m, end_position)
    np.testing.assert_allclose(brake.velocity_mps, end_velocity)
    assert brake.duration_s == pytest.approx(math.hypot(*end_velocity) / braking)
    assert math.hypot(*brake.acceleration_mps2) == pytest.approx(braking)
    np.testing.assert_allclose(
        np.asarray(brake.velocity_mps) + np.asarray(brake.acceleration_mps2) * brake.duration_s,
        ZERO,
        atol=1e-14,
    )
    np.testing.assert_allclose(
        reserve.position_m, end_position + 0.5 * end_velocity * brake.duration_s
    )
    assert reserve.duration_s == pytest.approx(allowance / braking)
    np.testing.assert_allclose(reserve.velocity_mps, ZERO, atol=1e-14)
    assert math.fsum(segment.duration_s for segment in segments) == pytest.approx(
        lease + (math.hypot(*end_velocity) + allowance) / braking
    )


@pytest.mark.parametrize(
    "braking, velocity, allowed",
    [
        (0.25, 10.0, True),
        (0.25, 10.2, False),
        (0.45, 17.8, True),
        (0.45, 18.1, False),
        (0.49, 19.4, True),
        (0.49, 19.7, False),
    ],
)
def test_local_full_stopping_horizon_is_bounded_by_40_without_shortening_leases(
    braking, velocity, allowed
):
    observation = transit_numeric_input()
    observation["shuttle"]["velocity"] = [velocity, 0.0, 0.0]
    guidance = ResilientGuidance(ResilienceConfig(local_braking_accel_mps2=braking))
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    command = guidance.command(observation, None, {})
    assert guidance.config.max_stopping_horizon_s == 40.0
    assert receipt.lease_sim_s == 0.75
    if allowed:
        assert command[0][0] < 0.0
        end_velocity = np.array([velocity, 0.0, 0.0]) + command[0] * receipt.lease_sim_s
        allowance = receipt.report["estimate"]["velocity_uncertainty_mps"]
        braking = guidance.report["braking_model_accel_mps2"]
        expected_horizon = receipt.lease_sim_s + (math.hypot(*end_velocity) + allowance) / braking
        assert guidance.report["command_stopping_horizon_s"] == pytest.approx(expected_horizon)
        assert expected_horizon <= 40.0
        assert guidance.report["decision_valid_until_s"] == 10.75
        segments = stopping_motion(
            observation["shuttle"]["position"],
            observation["shuttle"]["velocity"],
            command[0],
            receipt.lease_sim_s,
            braking,
            allowance,
        )
        assert segments[0].duration_s == 0.75
        assert math.fsum(segment.duration_s for segment in segments) == pytest.approx(
            expected_horizon
        )
        assert motion_clear(
            segments, temporal_obstacles(receipt.observation, guidance._estimator, guidance.config)
        )
    else:
        assert_coast(command)
        # Even a maximum opposing burn cannot fit the complete stop. This lower
        # bound applies to every capped direction without replacing the planner.
        minimum_end_speed = velocity - guidance.config.max_accel_mps2 * receipt.lease_sim_s
        minimum_horizon = (
            receipt.lease_sim_s
            + (minimum_end_speed + receipt.report["estimate"]["velocity_uncertainty_mps"]) / braking
        )
        assert minimum_horizon > 40.0
        assert guidance.report["local_rejections"]["stopping_horizon_budget"] > 0
        assert guidance.report["admissible_local_candidates"] == 0


def test_clear_immediate_lease_does_not_authorize_a_blocked_future_active_stop():
    observation = transit_numeric_input()
    observation["shuttle"]["velocity"] = [8.0, 0.0, 0.0]
    observation["radar"] = [[[150.0, 0.0, 0.0], [149.0, -8.0, 2.0], [10.0, 0.0, 0.0]]]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    obstacles = temporal_obstacles(receipt.observation, guidance._estimator, guidance.config)
    segments = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        ZERO,
        receipt.lease_sim_s,
        guidance.config.local_braking_accel_mps2,
        receipt.report["estimate"]["velocity_uncertainty_mps"],
    )
    assert segments[0].duration_s == receipt.lease_sim_s == 0.75
    assert 6.0 < math.fsum(segment.duration_s for segment in segments) <= 40.0
    assert motion_clear(segments[:1], obstacles)
    assert not motion_clear(segments, obstacles)
    assert_coast(guidance.command(observation, None, {}))
    assert guidance.report["admissible_local_candidates"] == 0
    assert "stopping reserve" in guidance.report["reason"]


def test_candidate_search_checks_partial_burns_without_relaxing_the_full_stop():
    observation = transit_numeric_input()
    observation["shuttle"]["velocity"] = [0.0, 1.0, 0.0]
    observation["target"]["position"] = [2000.0, 10000.0, 0.0]
    observation["radar"] = numeric_rows(500, 10.0)
    observation["radar"][0] = [[0.0, 65.0, 0.0], [64.0, -1.0, 2.0], [10.0, 0.0, 0.0]]
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    obstacles = temporal_obstacles(receipt.observation, guidance._estimator, guidance.config)
    full = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        np.array((0.0, 0.5, 0.0)),
        receipt.lease_sim_s,
        0.49,
        0.05,
    )
    assert not motion_clear(full, obstacles)
    partial = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        np.array((0.0, 0.25, 0.0)),
        receipt.lease_sim_s,
        0.49,
        0.05,
    )
    assert motion_clear(partial, obstacles)
    acceleration, _ = guidance.command(observation, None, {})
    assert 0.0 < math.hypot(*acceleration) <= 0.5
    selected = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        acceleration,
        receipt.lease_sim_s,
        guidance.report["braking_model_accel_mps2"],
        0.05,
    )
    assert motion_clear(selected, obstacles)
    assert selected[0].duration_s == 0.75
    assert guidance.report["checked_radar_rows"] == 500
    assert guidance.report["admissible_local_candidates"] > 0
    assert guidance.report["admissible_local_candidates"] <= guidance.report["local_candidates"]
    assert guidance.report["continuation_checks"] == 0
    allocation = guidance.report["continuation_checks_by_finalist"]
    assert len(allocation) == guidance.report["admissible_local_candidates"]
    assert all(checks <= 2 for checks in allocation)
    assert sum(allocation) == guidance.report["continuation_checks"]
    assert not guidance.report["continuation_clear"]
    assert guidance.report["continuation_stopping_horizon_s"] is None
    assert guidance.report["command_stopping_horizon_s"] == pytest.approx(
        math.fsum(segment.duration_s for segment in selected)
    )
    assert observation == before


@pytest.mark.parametrize(
    "surface, velocity, allowed",
    [
        (1000.0, ZERO, True),
        (None, ZERO, False),
        (1000.01, ZERO, False),
        (1000.0, (-1.0, 0.0, 0.0), False),
        (1000.0, (0.0, 2.0, 0.0), False),
    ],
)
def test_departure_requires_public_terrain_and_only_supported_outward_radial_motion(
    surface, velocity, allowed
):
    observation = transit_numeric_input()
    observation["shuttle"].update(
        position=[1000.0, 0.0, 0.0], velocity=list(velocity), status="landed"
    )
    observation["target"]["position"] = [1000.0, 10000.0, 0.0]
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    if surface is not None:
        observation["geometry"]["terrain_radius_at_shuttle_m"] = surface
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command
    command = guidance.command(observation, None, {})
    if allowed:
        assert 0.0 < command[0][0] <= 0.5
        np.testing.assert_array_equal(command[0][1:], (0.0, 0.0))
        assert guidance.report["surface_departure"]
        assert guidance.report["terrain_clearance_verified"] is False
    else:
        assert_coast(command)


@pytest.mark.parametrize("mission_status", ["running", "arrived", "failed"])
def test_zero_length_goal_and_native_completion_never_generate_nan(mission_status):
    observation = transit_numeric_input()
    observation["target"]["position"] = list(observation["shuttle"]["position"])
    observation["mission"]["status"] = mission_status
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=False)
    command = guidance.command(observation, None, {})
    assert_coast(command)
    assert np.isfinite(np.asarray(command)).all()
    json.dumps(guidance.report, allow_nan=False)
    assert observation == before


def test_advisory_geometry_never_overrides_fresh_occupancy_checks():
    observation, guidance, advisory = checked_advisory_context()
    assert math.hypot(*guidance.command(observation, advisory, {})[0]) > 0.0
    current = transit_numeric_input(11.2, 112)
    current["radar"][0][:2] = [[1.0, 0.0, 0.0], [1.0, 0.0, 2.0]]
    assert guidance.update(current, wall_time_s=1.2, running=False).can_command
    assert_coast(guidance.command(current, advisory, {}))
    assert guidance.report["source_time_s"] == 11.2
    assert guidance.report["admissible_local_candidates"] == 0
    assert (
        guidance.report["global_route_kind"]
        == "advisory_geometry_only_not_a_complete_current_route"
    )


def test_unregistered_geometry_cannot_supply_a_local_advisory_heading():
    observation = transit_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    frame = predict_obstacles(receipt.observation, guidance.config.planner)
    unregistered = replace(
        plan_route(frame, frame.config),
        status="candidate",
        points_world_m=(tuple(observation["shuttle"]["position"]), (2000.0, 500.0, 0.0)),
    )
    baseline = guidance.command(observation, None, {})
    baseline_goal = guidance.report["next_waypoint_world_m"]
    acceleration, angular = guidance.command(observation, unregistered, {})
    np.testing.assert_array_equal(acceleration, baseline[0])
    np.testing.assert_array_equal(angular, baseline[1])
    assert guidance.report["next_waypoint_world_m"] == baseline_goal
    assert not guidance._local_motion.has_advisory


def test_non_departure_advisory_keeps_its_first_unpassed_corner():
    observation, guidance, original = checked_advisory_context()
    assert not original.frame.has_surface_departure
    corner = (2000.0, 500.0, 0.0)
    advisory = replace(
        original,
        points_world_m=(
            tuple(observation["shuttle"]["position"]),
            corner,
            tuple(original.points_world_m[-1]),
        ),
    )
    acceleration, _ = guidance.command(observation, advisory, {})
    assert tuple(guidance.report["next_waypoint_world_m"]) == corner
    assert guidance.report["waypoint_index"] == 1
    assert acceleration[1] > 0.0


def test_local_preview_request_does_not_wait_for_the_first_command():
    observation = transit_numeric_input()
    guidance = ResilientGuidance()
    first = guidance.update(observation, wall_time_s=0.0, running=False)
    assert first.can_command and first.requires_replan
    preview = numeric_checked_route(guidance, first)
    assert preview.status == "no_candidate"
    current = guidance.update(observation, wall_time_s=0.1, running=False)
    assert current.can_command and not current.requires_replan
    acceleration, angular = guidance.command(current.observation, preview, {})
    assert 0 < math.hypot(*acceleration) <= 0.5
    np.testing.assert_array_equal(angular, ZERO)


def test_local_motion_only_frame_cannot_publish_a_partial_route_as_a_candidate():
    observation = transit_numeric_input()
    del observation["debris_distribution"]
    frame = predict_obstacles(observation, ResilienceConfig().planner)
    assert plan_route(frame, frame.config).status == "candidate"
    local_frame = replace(frame, local_motion_only=True)
    route = plan_route(local_frame, local_frame.config)
    assert route.status == "no_candidate"
    assert route.points_world_m.shape == (0, 3)
    preview = route.to_preview()
    assert preview["status"] == "no_candidate" and preview["points_world_m"] == []
    assert "no complete route" in preview["detail"]
    partial = replace(
        route, status="candidate", points_world_m=((2000.0, 0.0, 0.0), (2001.0, 0.0, 0.0))
    )
    with pytest.raises(ValueError, match="complete path"):
        partial.to_preview()


def test_direct_local_planner_accepts_the_documented_optional_route_keyword():
    observation = transit_numeric_input()
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    decision = LocalMotionPlanner().command(
        observation, estimator, config, 0.75, {}, degraded=False, route=None
    )
    assert 0.0 < math.hypot(*decision.acceleration_mps2) <= 0.5
    assert decision.report["checked_radar_rows"] == len(observation["radar"])
    assert decision.report["decision_valid_until_s"] == 10.75


def history_scan(time_s, relative):
    """Constructed anonymous displacement data, not engine observations."""
    observation = transit_numeric_input(time_s, round(time_s * 10))
    observation["debris_flow"] = {
        "speed_mps": 20.0,
        "speed_spread_mps": 4.0,
        "stream_axis": "x",
        "stream_sign": 1,
    }
    observation["radar"] = [
        [
            list(position),
            [math.hypot(*position) - 1, 20 * position[0] / math.hypot(*position), 2.0],
            [time_s, 0.0, 0.0],
        ]
        for position in relative
    ]
    return observation


def test_anonymous_history_adds_resolved_velocity_and_never_reduces_raw_margins():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    first = history_scan(10.0, [(100.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    initial = temporal_obstacles(first, estimator, config)
    assert history.refine(first, estimator, config, initial)[1] == 0
    current = history_scan(15.0, [(200.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(15.0, current["shuttle"]))
    raw = temporal_obstacles(current, estimator, config)
    refined, count = history.refine(current, estimator, config, raw)
    assert count == 1
    assert len(refined.radii_m) == len(current["radar"]) + 3
    np.testing.assert_allclose(refined.velocities_mps[3], (20.0, 0.0, 0.0), atol=1e-12)
    np.testing.assert_array_equal(refined.centers_m, raw.centers_m)
    assert np.all(refined.radii_m >= raw.radii_m)
    assert np.all(refined.growth_mps >= raw.growth_mps)
    assert refined.growth_mps[3] > raw.growth_mps[3]
    np.testing.assert_array_equal(refined.growth_mps2, raw.growth_mps2)
    np.testing.assert_array_equal(refined.velocities_mps[:3], raw.velocities_mps[:3])
    history.refine(current, estimator, config, raw)
    assert len(history.frames) == 2, "polling is not another source scan"


def test_range_rate_ineligible_pair_cannot_hide_an_otherwise_unique_motion_association():
    """Constructed ambiguity: both positions fit, but only one range rate fits."""
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    first = history_scan(10.0, [(100.0, 300.0, 0.0), (200.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    latest = history_scan(15.0, [(200.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(15.0, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    refined, count = history.refine(latest, estimator, config, raw)
    assert count == 1 and history.associated_rows == 1
    np.testing.assert_allclose(history.advisory_velocities_mps[0], (20.0, 0.0, 0.0), atol=1e-12)
    np.testing.assert_allclose(refined.velocities_mps[3], (20.0, 0.0, 0.0), atol=1e-12)
    assert len(refined.radii_m) == len(latest["radar"]) + 3
    assert np.all(refined.radii_m >= raw.radii_m)
    assert np.all(refined.growth_mps >= raw.growth_mps)
    # Raw radial occupancy remains an independent input to actual acceptance.
    unchanged = temporal_obstacles(latest, estimator, config)
    for name in ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
        np.testing.assert_array_equal(getattr(raw, name), getattr(unchanged, name))


@pytest.mark.parametrize("ambiguous_side", ["previous", "current"])
def test_ambiguous_anonymous_history_falls_back_without_dropping_any_row(ambiguous_side):
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    previous = [(100.0, 300.0, 0.0)]
    current = [(200.0, 300.0, 0.0)]
    (previous if ambiguous_side == "previous" else current).append(
        (103.0, 300.0, 0.0) if ambiguous_side == "previous" else (203.0, 300.0, 0.0)
    )
    first = history_scan(10.0, previous)
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    latest = history_scan(15.0, current)
    estimator.correct(NavigationSample.from_public(15.0, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    refined, count = history.refine(latest, estimator, config, raw)
    assert count == 0
    assert len(refined.radii_m) == len(current) + 3
    np.testing.assert_array_equal(refined.velocities_mps, raw.velocities_mps)
    np.testing.assert_array_equal(refined.radii_m, raw.radii_m)


def test_flow_changes_reset_anonymous_motion_history():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    first = history_scan(10.0, [(100.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    current = history_scan(15.0, [(200.0, 300.0, 0.0)])
    current["debris_flow"]["speed_mps"] = 18.0
    estimator.correct(NavigationSample.from_public(15.0, current["shuttle"]))
    raw = temporal_obstacles(current, estimator, config)
    refined, count = history.refine(current, estimator, config, raw)
    assert count == 0 and len(history.frames) == 1
    np.testing.assert_array_equal(refined.velocities_mps, raw.velocities_mps)


@pytest.mark.parametrize(
    "fallback", ["pair_budget", "unresolved_tangent", "radial_disagreement", "missing_flow"]
)
def test_anonymous_history_fallback_keeps_every_original_obstacle_and_allowance(fallback):
    config = ResilienceConfig(
        planner=replace(
            ResilienceConfig().planner, max_nodes=1 if fallback == "pair_budget" else 50000
        )
    )
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    previous = [(100.0, 300.0, 0.0), (100.0, 1000.0, 0.0)]
    current = [(200.0, 300.0, 0.0), (200.0, 1000.0, 0.0)]
    if fallback == "unresolved_tangent":
        previous, current = [(100.0, 0.0, 0.0)], [(200.0, 0.0, 0.0)]
    first = history_scan(10.0, previous)
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    latest = history_scan(15.0, current)
    if fallback == "radial_disagreement":
        for row in latest["radar"]:
            row[1][1] += 100.0
    if fallback == "missing_flow":
        del latest["debris_flow"]
    estimator.correct(NavigationSample.from_public(15.0, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    refined, count = history.refine(latest, estimator, config, raw)
    assert count == 0
    for name in ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
        np.testing.assert_array_equal(getattr(refined, name), getattr(raw, name))
    assert len(refined.radii_m) == len(current) + 3


def test_anonymous_history_uses_measurement_time_ownship_pose_and_retains_row_order():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    first = history_scan(10.0, [(100.0, 300.0, 0.0), (100.0, 1000.0, 0.0)])
    first["shuttle"]["velocity"] = [2.0, 0.0, 0.0]
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    latest = history_scan(15.0, [(190.0, 300.0, 0.0), (190.0, 1000.0, 0.0)])
    latest["time_s"] = 15.1
    latest["shuttle"].update(position=[2010.2, 0.0, 0.0], velocity=[2.0, 0.0, 0.0])
    for row in latest["radar"]:
        row[1][1] = 18.0 * row[0][0] / math.hypot(*row[0])
    estimator.correct(NavigationSample.from_public(15.1, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    refined, count = history.refine(latest, estimator, config, raw)
    assert count == 2
    np.testing.assert_allclose(
        refined.velocities_mps[3:], ((20.0, 0.0, 0.0), (20.0, 0.0, 0.0)), atol=1e-12
    )
    assert np.all(refined.radii_m >= raw.radii_m)
    assert np.all(refined.growth_mps >= raw.growth_mps)
    latest["radar"].reverse()
    reversed_raw = temporal_obstacles(latest, estimator, config)
    reversed_refined, reversed_count = history.refine(latest, estimator, config, reversed_raw)
    assert reversed_count == count
    for name in ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
        np.testing.assert_allclose(
            getattr(reversed_refined, name)[3:], getattr(refined, name)[3:][::-1], atol=1e-12
        )


@pytest.mark.parametrize("approaching", [True, False])
def test_nominal_encounter_preference_prioritizes_escape_and_ignores_receding_rocks(approaching):
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    observation = history_scan(10.0, [(-300.0 if approaching else 300.0, 5.0, -3.0)])
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    obstacles = temporal_obstacles(observation, estimator, config)
    preferred = np.array((8.0, 0.0, 0.0))
    steered, count = local_motion._traffic_steering(
        observation,
        obstacles,
        np.asarray(observation["shuttle"]["position"]),
        np.zeros(3),
        preferred,
        config.max_stopping_horizon_s,
    )
    if approaching:
        away = np.asarray((0.0, -5.0, 3.0)) / math.hypot(5.0, 3.0)
        assert count == 1
        assert np.dot(steered, away) == pytest.approx(math.hypot(*steered))
        assert steered[0] == 0.0
    else:
        assert count == 0
        np.testing.assert_array_equal(steered, preferred)


def test_reachable_continuation_keeps_measured_start_both_leases_brake_and_reserve():
    position = np.array((2000.0, 4.0, -2.0))
    velocity = np.array((0.4, 0.1, 0.0))
    first_accel = np.array((0.1, 0.2, 0.0))
    second_accel = np.array((0.05, -0.1, 0.0))
    segments = local_motion.continuing_motion(
        position, velocity, first_accel, second_accel, 0.75, 0.49, 0.05
    )
    assert len(segments) == 4
    first, second, brake, reserve = segments
    assert first.duration_s == second.duration_s == 0.75
    np.testing.assert_array_equal(first.position_m, position)
    np.testing.assert_array_equal(first.velocity_mps, velocity)
    next_position, next_velocity = local_motion._end(first)
    np.testing.assert_allclose(second.position_m, next_position)
    np.testing.assert_allclose(second.velocity_mps, next_velocity)
    brake_position, brake_velocity = local_motion._end(second)
    np.testing.assert_allclose(brake.position_m, brake_position)
    np.testing.assert_allclose(brake.velocity_mps, brake_velocity)
    assert brake.duration_s == pytest.approx(math.hypot(*brake_velocity) / 0.49)
    assert reserve.duration_s == pytest.approx(0.05 / 0.49)
    assert math.fsum(segment.duration_s for segment in segments) == pytest.approx(
        1.5 + (math.hypot(*brake_velocity) + 0.05) / 0.49
    )
    np.testing.assert_allclose(local_motion._end(brake)[1], ZERO, atol=1e-14)
    np.testing.assert_allclose(reserve.velocity_mps, ZERO, atol=1e-14)


def test_first_stop_can_pass_while_a_reachable_continuation_is_blocked_by_unchanged_growth():
    config = ResilienceConfig()
    position = np.array((2000.0, 0.0, 0.0))
    velocity = np.array((0.0, 1.0, 0.0))
    acceleration = np.array((0.0, 0.5, 0.0))
    obstacles = TemporalObstacles(
        np.array(((0.0, 0.0, 0.0), (1e8, 0.0, 0.0), (10000.0, 0.0, 0.0), (2000.0, 75.0, 0.0))),
        np.zeros((4, 3)),
        np.array((100.0, 1.0, 1.0, 20.0)),
        np.array((0.0, 0.0, 0.0, 12.0)),
        np.full(4, 0.25),
    )
    first = stopping_motion(position, velocity, acceleration, 0.75, 0.49, 0.05)
    continued = local_motion.continuing_motion(
        position, velocity, acceleration, acceleration, 0.75, 0.49, 0.05
    )
    context = dict(
        axis=np.array((1.0, 0.0, 0.0)),
        radial_prefix=False,
        departure=False,
        departure_radius=152.0,
        tolerance=0.26,
        padding=0.5,
    )
    assert motion_clear(first, obstacles)
    assert not motion_clear(continued, obstacles)
    reason, supported, first_horizon = local_motion._motion_rejection(
        first, obstacles, config, **context
    )
    assert reason is None and not supported
    reason, _, continued_horizon = local_motion._motion_rejection(
        continued, obstacles, config, **context
    )
    assert reason == "radar_occupancy"
    assert 0.75 < first_horizon < continued_horizon <= 40.0
    assert first[0] == continued[0]
    assert continued[1].duration_s == 0.75


def test_history_advice_never_replaces_the_original_raw_safety_envelopes():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    planner = LocalMotionPlanner()
    first = history_scan(10.0, [(100.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    planner.command(first, estimator, config, 0.75, {}, degraded=False)
    latest = history_scan(15.0, [(200.0, 300.0, 0.0)])
    estimator.correct(NavigationSample.from_public(15.0, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    decision = planner.command(latest, estimator, config, 0.75, {}, degraded=False)
    assert decision.report["history_velocity_rows"] == 1
    assert decision.report["history_corroborated_velocity_rows"] == 0
    assert (
        decision.report["radar_model"]
        == "unchanged_raw_radial_safety_envelopes_with_all_additive_margins"
    )
    selected = stopping_motion(
        latest["shuttle"]["position"],
        latest["shuttle"]["velocity"],
        np.asarray(decision.acceleration_mps2),
        0.75,
        decision.report["braking_model_accel_mps2"],
        0.05,
    )
    assert motion_clear(selected, raw)
    assert selected[0].duration_s == 0.75
    np.testing.assert_array_equal(selected[0].velocity_mps, latest["shuttle"]["velocity"])


def test_escape_search_never_authorizes_an_executable_sideways_departure():
    observation = transit_numeric_input()
    observation["shuttle"]["position"] = [1000.0, 0.0, 0.0]
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    observation["geometry"]["terrain_radius_at_shuttle_m"] = 1000.0
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    planner = LocalMotionPlanner()
    decision = planner.command(observation, estimator, config, 0.75, {}, degraded=False)
    assert decision.acceleration_mps2[0] > 0
    np.testing.assert_array_equal(decision.acceleration_mps2[1:], (0.0, 0.0))
    assert decision.report["surface_departure"]
    segments = stopping_motion(
        observation["shuttle"]["position"],
        observation["shuttle"]["velocity"],
        np.asarray(decision.acceleration_mps2),
        0.75,
        decision.report["braking_model_accel_mps2"],
        0.05,
    )
    np.testing.assert_array_equal(segments[0].position_m, observation["shuttle"]["position"])
    np.testing.assert_array_equal(segments[0].velocity_mps, observation["shuttle"]["velocity"])
    assert segments[0].duration_s == 0.75
    assert local_motion._radial_supported(segments, planner.anchor, 0.26)


def test_unique_slow_displacement_is_advice_not_a_forced_nominal_flow_velocity():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    history = RadarMotionHistory()
    first = history_scan(10.0, [(-100.0, 5.0, -3.0)])
    first["radar"][0][1][1] = 0.0
    estimator.correct(NavigationSample.from_public(10.0, first["shuttle"]))
    history.refine(first, estimator, config, temporal_obstacles(first, estimator, config))
    latest = history_scan(15.0, [(-100.0, 5.0, -3.0)])
    latest["radar"][0][1][1] = 0.0
    estimator.correct(NavigationSample.from_public(15.0, latest["shuttle"]))
    raw = temporal_obstacles(latest, estimator, config)
    refined, count = history.refine(latest, estimator, config, raw)
    assert count == 0 and history.associated_rows == 1
    np.testing.assert_array_equal(history.advisory_velocities_mps, np.zeros((1, 3)))
    np.testing.assert_array_equal(refined.velocities_mps, raw.velocities_mps)
    goal = np.array((8.0, 0.0, 0.0))
    position = np.asarray(latest["shuttle"]["position"])
    _, nominal_encounters = local_motion._traffic_steering(
        latest, raw, position, np.zeros(3), goal, 40.0
    )
    advised, associated_encounters = local_motion._traffic_steering(
        latest,
        raw,
        position,
        np.zeros(3),
        goal,
        40.0,
        advisory_velocities=history.advisory_velocities_mps,
    )
    assert nominal_encounters == 1 and associated_encounters == 0
    np.testing.assert_array_equal(advised, goal)


def test_encounter_preference_retains_outward_momentum_and_builds_radial_turn_reserve():
    observation = history_scan(10.0, [(-300.0, 5.0, -3.0)])
    observation["shuttle"].update(position=[1014.0, 0.0, 0.0], velocity=[1.0, 0.0, 0.0])
    observation["radar"][0][1][1] = (
        19.0 * observation["radar"][0][0][0] / math.hypot(*observation["radar"][0][0])
    )
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    observation["geometry"]["terrain_radius_at_shuttle_m"] = 1000.0
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    decision = LocalMotionPlanner().command(
        observation, estimator, config, 0.75, {}, degraded=False
    )
    goal = np.asarray(decision.report["desired_velocity_world_mps"])
    assert decision.report["advisory_flow_encounters"] == 1
    assert goal[0] >= observation["shuttle"]["velocity"][0]
    assert math.hypot(*goal[1:]) > 0.0
    assert math.hypot(*goal) <= config.max_speed_mps
    assert decision.report["decision_valid_until_s"] == 10.75
    assert decision.report["continuation_checks"] <= 6


def test_nominal_escape_heading_hysteresis_does_not_reverse_for_small_geometry_noise():
    observation = history_scan(10.0, [(500.0, 3000.0, 0.0)])
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    estimator.correct(NavigationSample.from_public(10.0, observation["shuttle"]))
    obstacles = temporal_obstacles(observation, estimator, config)
    position = np.asarray(observation["shuttle"]["position"])
    velocity = np.array((1.0, 0.0, 0.0))
    goal = np.array((4.0, 0.0, -math.sqrt(48.0)))
    heading = local_motion.EscapeHeading()
    chosen = heading.choose(
        position, velocity, goal, obstacles, obstacles.velocities_mps, config, 0.5, 1.0, 0.07, 10.0
    ).copy()
    before = obstacles.centers_m.copy()
    for index, offset in enumerate((0.2, -0.2, 0.1, -0.1), start=1):
        shifted = np.array(obstacles.centers_m, copy=True)
        shifted[3, 1] += offset
        noisy_advice = TemporalObstacles(
            shifted,
            obstacles.velocities_mps,
            obstacles.radii_m,
            obstacles.growth_mps,
            obstacles.growth_mps2,
        )
        current = heading.choose(
            position,
            velocity,
            goal,
            noisy_advice,
            noisy_advice.velocities_mps,
            config,
            0.5,
            1.0,
            0.07,
            10.0 + index * 0.1,
        )
        np.testing.assert_array_equal(current, chosen)
    assert heading.sample_time_s == pytest.approx(10.4)
    assert math.hypot(*chosen) == pytest.approx(1.0)
    assert np.dot(chosen, position) > 0.0
    np.testing.assert_array_equal(obstacles.centers_m, before)
    np.testing.assert_array_equal(position, observation["shuttle"]["position"])
    np.testing.assert_array_equal(velocity, (1.0, 0.0, 0.0))


def test_fresh_high_spin_allows_bounded_damping_without_claiming_linear_clearance():
    observation = transit_numeric_input()
    observation["shuttle"]["angular_velocity_rad_s"] = [0.0, 0.5, 0.0]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    assert receipt.can_command and receipt.lease_sim_s == 0.75
    linear, angular = guidance.command(observation, None, {})
    np.testing.assert_array_equal(linear, ZERO)
    assert 0.0 < math.hypot(*angular) <= 0.05
    assert np.dot(angular, observation["shuttle"]["angular_velocity_rad_s"]) < 0.0
    assert (
        guidance.report["decision"] == "bounded_angular_rate_damping_without_translation_clearance"
    )
    assert "linear clearance is not granted" in guidance.report["command_gate"]
    failed = copy.deepcopy(observation)
    failed["sensor_health"]["revision"] += 1
    numeric_unit(failed, "navigation")["active_faults"] = ["navigation"]
    assert not guidance.update(failed, wall_time_s=0.1, running=False).can_command
    assert_coast(guidance.command(failed, None, {}))


def test_angular_navigation_content_cannot_change_on_a_held_source_clock():
    observation = transit_numeric_input()
    observation["shuttle"]["angular_velocity_rad_s"] = [0.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    assert guidance.update(observation, wall_time_s=0.0, running=False).can_command
    altered = copy.deepcopy(observation)
    altered["shuttle"]["angular_velocity_rad_s"][1] = 0.5
    assert_coast(guidance.command(altered, None, {}))
    receipt = guidance.update(altered, wall_time_s=0.1, running=False)
    assert not receipt.can_command
    assert not receipt.report["assessments"]["navigation"]["usable"]
    assert_coast(guidance.command(altered, None, {}))


@pytest.mark.parametrize("lease", [0.25, 0.75])
def test_nominal_corridor_rollouts_do_not_assign_instant_cruise_velocity(lease):
    position = np.array((2000.0, 4.0, -2.0))
    velocity = np.zeros(3)
    directions = np.eye(3)
    original = (position.copy(), velocity.copy(), directions.copy())
    times, positions, accelerations, burns = local_motion.nominal_rollouts(
        position,
        velocity,
        directions,
        8.0,
        0.5,
        1.0,
        lease,
        40.0,
    )
    assert times[0] == 0.0 and times[-1] == 40.0
    np.testing.assert_array_equal(positions[:, 0], np.tile(position, (3, 1)))
    np.testing.assert_allclose(np.hypot.reduce(accelerations, axis=1), 0.5, atol=1e-15)
    np.testing.assert_allclose(burns, lease, atol=1e-15)
    reachable_speed = 0.5 * lease
    expected_travel = reachable_speed * (40.0 - 0.5 * lease)
    np.testing.assert_allclose(
        np.hypot.reduce(positions[:, -1] - position, axis=1), expected_travel
    )
    assert expected_travel < 15.0, "The advice cannot silently give a resting ship an 8 m/s cruise"
    for actual, before in zip((position, velocity, directions), original, strict=True):
        np.testing.assert_array_equal(actual, before)


def test_nominal_corridor_turns_start_at_measured_velocity_under_bounded_acceleration():
    position = np.array((2000.0, 0.0, 0.0))
    velocity = np.array((1.0, 0.0, 0.0))
    directions = np.array(((0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    times, positions, accelerations, burns = local_motion.nominal_rollouts(
        position,
        velocity,
        directions,
        8.0,
        0.5,
        1.0,
        0.75,
        40.0,
    )
    assert np.all(np.hypot.reduce(accelerations, axis=1) <= 0.5 + 1e-15)
    target_velocities = velocity + accelerations * burns[:, None]
    np.testing.assert_allclose(np.hypot.reduce(target_velocities, axis=1), 1.375, atol=1e-15)
    for index in range(len(directions)):
        for offset, calculated in zip(times, positions[index], strict=True):
            burn = min(offset, burns[index])
            expected = (
                position + velocity * offset + accelerations[index] * burn * (offset - 0.5 * burn)
            )
            np.testing.assert_allclose(calculated, expected, atol=1e-12)


def test_checkpoint_geometric_advice_targets_the_arrival_region_outside_the_station_body():
    target = np.array((10000.0, 0.0, 0.0))
    position = np.array((10000.0, 180.0, 0.0))
    obstacles = TemporalObstacles(
        np.array(((0.0, 0.0, 0.0), (1e8, 0.0, 0.0), target)),
        np.zeros((3, 3)),
        np.array((1000.0, 1000.0, 32.0)),
        np.zeros(3),
        np.full(3, 0.25),
    )
    approach, index = local_motion.checkpoint_approach(position, target, 49.0, obstacles)
    assert math.dist(approach, target) == pytest.approx(49.0)
    assert math.dist(approach, target) > obstacles.radii_m[2]
    assert index.clear(approach, approach)
    assert not index.clear(tuple(target), tuple(target))
    assert math.dist(position, approach) < math.dist(position, target)


def test_approaching_traffic_does_not_authorize_thrust_that_erases_measured_sideways_escape():
    observation = history_scan(10.0, [(-150.0, 5.0, -3.0)])
    observation["shuttle"]["velocity"] = [0.0, 1.0, 0.0]
    relative = np.asarray(observation["radar"][0][0])
    observation["radar"][0][1][1] = float(
        np.dot(
            relative / math.hypot(*relative),
            np.array((20.0, -1.0, 0.0)),
        )
    )
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    acceleration, angular = guidance.command(observation, None, {})
    assert receipt.can_command and receipt.lease_sim_s == 0.75
    assert guidance.report["measured_cross_stream_speed_mps"] == pytest.approx(1.0)
    assert guidance.report["directly_closing_traffic_deadline_s"] is not None
    assert 0.0 < guidance.report["advisory_traffic_deadline_s"] < 15.0
    end_velocity = np.asarray(observation["shuttle"]["velocity"]) + acceleration * 0.75
    assert math.hypot(*end_velocity[1:]) >= 1.0 - 1e-12
    assert math.hypot(*acceleration) <= 0.5
    np.testing.assert_array_equal(angular, ZERO)
    if guidance.report.get("admissible_local_candidates"):
        segments = stopping_motion(
            observation["shuttle"]["position"],
            observation["shuttle"]["velocity"],
            acceleration,
            0.75,
            guidance.report["braking_model_accel_mps2"],
            receipt.report["estimate"]["velocity_uncertainty_mps"],
        )
        assert motion_clear(
            segments,
            temporal_obstacles(
                receipt.observation,
                guidance._estimator,
                guidance.config,
            ),
        )
        assert segments[0].duration_s == 0.75


def test_nominal_inlet_velocity_cannot_create_a_braking_veto_for_a_nonclosing_scan():
    observation = history_scan(10.0, [(-150.0, 5.0, -3.0)])
    observation["shuttle"]["velocity"] = [0.0, 1.0, 0.0]
    observation["radar"][0][1][1] = 0.0
    guidance = ResilientGuidance()
    ready = guidance.update(observation, wall_time_s=0.0, running=False)
    acceleration, _ = guidance.command(observation, None, {})
    assert ready.can_command and ready.lease_sim_s == 0.75
    assert guidance.report["advisory_flow_encounters"] > 0
    assert guidance.report["directly_closing_traffic_deadline_s"] is None
    assert "minimum_measured_exit_speed_mps" not in guidance.report
    assert math.hypot(*acceleration) <= 0.5
