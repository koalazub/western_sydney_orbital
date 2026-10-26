"""Pure numeric contracts, not simulated flight, SDK or backend validation.

The dictionaries below are constructed mathematical inputs. They are not saved
sensor observations and do not establish local or authoritative flight safety.
"""

from __future__ import annotations

import ast
import copy
import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import numpy as np
import pytest

from wsorb.flight import (
    PlannerConfig,
    ResilienceConfig,
    ResilientGuidance,
    RoutePlan,
    basic_command,
    coast_command,
    plan_route,
    predict_obstacles,
)
from wsorb.flight.control import reference_to_body
from wsorb.flight.estimation import NavigationSample, StateEstimator
from wsorb.flight.geometry import Capsule, CollisionIndex, segment_clear
from wsorb.flight.planning import MAX_PREVIEW_POINTS, _Corridor, search_route


def public_input() -> dict:
    """An arbitrary numeric scene, not an observed or emulated flight."""
    return {
        "time_s": 10.0,
        "seed": 100,
        "frame": "constructed Phobos-centred reference frame, SI",
        "rotation_convention": "active_xyzw",
        "environment": {"mars_position_m": [0.0, 1e6, 0.0], "mars_radius_m": 1000.0},
        "geometry": {
            "phobos_enclosing_radius_m": 100.0,
            "arrival_radius_m": 50.0,
            "debris_count": 500,
        },
        "sensor_model": {
            "range_noise_m": 2.0,
            "range_rate_noise_mps": 0.02,
            "diameter_noise_fraction": 0.05,
        },
        "shuttle": {
            "position": [1000.0, 0.0, 0.0],
            "velocity": [2.0, 3.0, 0.0],
            "rotation": [0.0, 0.0, 0.0, 1.0],
            "radius_m": 4.0,
            "status": "coasting",
        },
        "target": {"position": [1500.0, 0.0, 0.0], "radius_m": 20.0},
        "mission": {"phase": "transit"},
        "radar": [[[0.0, 300.0, 0.0], [299.0, -2.0, 2.0], [9.0, 0.0, 0.0]]],
        "max_accel_mps2": 0.5,
        "max_angular_accel_rad_s2": 0.05,
    }


def small_config(**changes) -> PlannerConfig:
    return replace(
        PlannerConfig(
            cell_size_m=1.0,
            corridor_half_width_m=4.0,
            max_nodes=4000,
            max_expansions=2000,
            max_heap_entries=8000,
            clearance_m=0.0,
            forecast_horizon_s=0.0,
            unknown_tangential_speed_mps=0.0,
            sigma_multiplier=0.0,
            range_noise_m=0.0,
            range_rate_noise_mps=0.0,
            diameter_noise_fraction=0.0,
            observer_accel_allowance_mps2=0.0,
        ),
        **changes,
    )


def small_input() -> dict:
    observation = public_input()
    observation["shuttle"].update(position=[10.0, 0.0, 0.0], velocity=[0.0, 0.0, 0.0], radius_m=0.1)
    observation["target"].update(position=[18.35, 0.0, 0.0], radius_m=0.1)
    observation["geometry"].update(phobos_enclosing_radius_m=1.0, arrival_radius_m=1.0)
    observation["sensor_model"] = {
        "range_noise_m": 0.0,
        "range_rate_noise_mps": 0.0,
        "diameter_noise_fraction": 0.0,
    }
    observation["radar"] = [[[0.0, 100.0, 0.0], [100.0, 0.0, 2.0], [10.0, 0.0, 0.0]]]
    return observation


def sphere(center, radius) -> Capsule:
    return Capsule(center, center, radius)


def assert_clear_path(result, start, goal, obstacles) -> None:
    assert result.points_world_m[0] == tuple(start)
    assert result.points_world_m[-1] == tuple(goal)
    assert all(
        segment_clear(left, right, obstacles)
        for left, right in zip(result.points_world_m[:-1], result.points_world_m[1:], strict=True)
    )


def test_defaults_are_versioned_frozen_algorithm_values() -> None:
    config = PlannerConfig()
    assert (config.cell_size_m, config.corridor_half_width_m) == (40.0, 800.0)
    assert (config.max_nodes, config.max_expansions, config.max_heap_entries) == (
        50_000,
        12_000,
        100_000,
    )
    assert (config.clearance_m, config.forecast_horizon_s, config.max_measurement_age_s) == (
        8.0,
        3.6,
        5.4,
    )
    assert (config.unknown_tangential_speed_mps, config.sigma_multiplier) == (12.0, 3.0)
    assert (config.range_noise_m, config.range_rate_noise_mps, config.diameter_noise_fraction) == (
        2.0,
        0.02,
        0.05,
    )
    assert config.observer_accel_allowance_mps2 == 0.5
    with pytest.raises(FrozenInstanceError):
        config.cell_size_m = 1.0


@pytest.mark.parametrize(
    "changes",
    [
        {"cell_size_m": 0.0},
        {"corridor_half_width_m": math.inf},
        {"max_nodes": True},
        {"max_expansions": 0},
        {"max_heap_entries": -1},
        {"clearance_m": -1.0},
        {"forecast_horizon_s": math.nan},
        {"max_measurement_age_s": -0.1},
        {"diameter_noise_fraction": 1.0 / 3.0},
    ],
)
def test_config_rejects_invalid_values(changes) -> None:
    with pytest.raises(ValueError):
        replace(PlannerConfig(), **changes)


def test_segment_checks_whole_edge_tangency_and_degenerate_points() -> None:
    obstacles = [sphere((0.0, 0.0, 0.0), 2.0)]
    assert segment_clear((0, 0, 0), (1, 1, 1), ())
    assert not segment_clear((-10, 0, 0), (10, 0, 0), obstacles)
    assert not segment_clear((-10, 2, 0), (10, 2, 0), obstacles)
    assert segment_clear((-10, 2.001, 0), (10, 2.001, 0), obstacles)
    assert segment_clear((3, 0, 0), (3, 0, 0), obstacles)
    assert not segment_clear((2, 0, 0), (2, 0, 0), obstacles)
    assert not segment_clear((0, 0, 0), (0, 0, 0), obstacles)


def test_capsule_parallel_interior_skew_and_endpoints() -> None:
    capsule = Capsule((0, -4, 0), (0, 4, 0), 1.0)
    assert not segment_clear((-4, 0, 0), (4, 0, 0), [capsule])
    assert not segment_clear((1, -3, 0), (1, 3, 0), [capsule])
    assert segment_clear((1.01, -3, 0), (1.01, 3, 0), [capsule])
    assert not segment_clear((-3, 5, 0), (3, 5, 0), [capsule])
    assert segment_clear((-3, 5.01, 0), (3, 5.01, 0), [capsule])
    skew = Capsule((0, -5, 1), (0, 5, 1), 1.0)
    assert not segment_clear((-5, 0, 0), (5, 0, 0), [skew])
    crossing = Capsule((-10, -1e-7, 0), (10, 1e-7, 0), 1e-10)
    assert not segment_clear((-10, 0, 0), (10, 0, 0), [crossing])


@pytest.mark.parametrize("exponent", [-8, -12, -20, -32, -44])
def test_near_parallel_scalar_triple_tangency_regression(exponent) -> None:
    start, goal = (0.0, 0.0, 0.0), (64.0, 32.0, 48.0)
    delta = 2.0**exponent
    for noise in (-(2.0**-47), 0.0, 2.0**-47):
        left = (-35.0, -16.0 - 3.0 * delta - noise, -20.0)
        right = (29.0, 16.0 + delta, 28.0)
        # The exact interior normal residual is (3, 0, -4), of length five.
        for query_start, query_end in ((start, goal), (goal, start)):
            for capsule_start, capsule_end in ((left, right), (right, left)):
                for radius, clear in ((5.0, False), (5.000001, False), (4.999999, True)):
                    assert (
                        segment_clear(
                            query_start, query_end, [Capsule(capsule_start, capsule_end, radius)]
                        )
                        is clear
                    )


def test_long_near_parallel_and_large_finite_geometry() -> None:
    for tilt in (0.0, 1e-6, 1e-10, 1e-14):
        capsule = Capsule((-6000.0, 2.0, -tilt), (6000.0, 2.0, tilt), 1.5)
        assert segment_clear((-5000, 0, 0), (5000, 0, 0), [capsule])
        assert segment_clear((5000, 0, 0), (-5000, 0, 0), [capsule])
    scale = 1e150
    obstacle = sphere((0, 0, 0), scale)
    assert segment_clear((-2 * scale, 1.1 * scale, 0), (2 * scale, 1.1 * scale, 0), [obstacle])
    assert not segment_clear((-2 * scale, scale, 0), (2 * scale, scale, 0), [obstacle])


@pytest.mark.parametrize("radius", [0.0, -1.0, math.inf, math.nan])
def test_invalid_capsules_are_rejected(radius) -> None:
    with pytest.raises(ValueError):
        sphere((0, 0, 0), radius)


def test_unrepresentable_geometry_fails_closed_even_without_obstacles() -> None:
    maximum = float(np.finfo(float).max)
    assert not segment_clear((-maximum, 0, 0), (maximum, 0, 0), ())
    assert not segment_clear((math.nan, 0, 0), (1, 0, 0), ())
    assert not CollisionIndex(()).clear((-maximum, 0, 0), (maximum, 0, 0))


def test_broadphase_matches_narrowphase_for_large_crossing_capsules() -> None:
    obstacles = [
        Capsule((4, -10000, 0), (4, 10000, 0), 1.25),
        sphere((4, 0, 1000), 999.0),
        sphere((-1000, -1000, -1000), 1.0),
    ]
    index = CollisionIndex(obstacles)
    for y in range(-4, 5):
        for z in range(-4, 5):
            start, end = (0.0, float(y), float(z)), (8.0, float(-z), float(y))
            assert index.clear(start, end) == segment_clear(start, end, obstacles)


def test_prediction_radial_projection_backdating_and_inverted_diameter_noise() -> None:
    observation = public_input()
    config = PlannerConfig()
    frame = predict_obstacles(observation, config)
    capsule = frame.obstacles[3]
    # Observer y velocity is 3, reported relative radial velocity is -2.
    # The inferred world-radial component is +1, not a full debris velocity.
    assert capsule.from_m == pytest.approx((998.0, 298.0, 0.0))
    assert capsule.to_m == pytest.approx((998.0, 301.6, 0.0))
    expected_radius = 1.0 / (1.0 - 3.0 * 0.05) + 12.0 + 6.0 + 0.25 + (12.0 + 0.06 + 0.5) * 4.6
    assert capsule.radius_m == pytest.approx(expected_radius)
    assert frame.max_measurement_age_s == 1.0
    assert frame.oldest_measurement_time_s == frame.newest_measurement_time_s == 9.0
    assert frame.measured_obstacles == 1
    assert frame.unmodelled_obstacles_lower_bound == 499


def test_repeated_rows_keep_times_and_forecast_advances_without_tracking() -> None:
    observation = public_input()
    first = predict_obstacles(observation, PlannerConfig())
    observation["time_s"] += 1.0
    later = predict_obstacles(observation, PlannerConfig())
    assert later.oldest_measurement_time_s == first.oldest_measurement_time_s
    assert later.newest_measurement_time_s == first.newest_measurement_time_s
    assert later.max_measurement_age_s == 2.0
    assert later.obstacles[3].from_m != first.obstacles[3].from_m
    assert later.obstacles[3].radius_m > first.obstacles[3].radius_m


def test_mixed_measurement_times_report_the_actual_oldest_newest_and_maximum_age() -> None:
    observation = public_input()
    observation["radar"].append([[0.0, -300.0, 0.0], [300.0, 0.0, 2.0], [8.0, 0.0, 0.0]])
    frame = predict_obstacles(observation, PlannerConfig())
    assert frame.oldest_measurement_time_s == 8.0
    assert frame.newest_measurement_time_s == 9.0
    assert frame.max_measurement_age_s == 2.0
    assert frame.measured_obstacles == 2
    assert frame.unmodelled_obstacles_lower_bound == 498


def test_prediction_uses_public_known_body_and_target_inflations() -> None:
    frame = predict_obstacles(public_input(), PlannerConfig())
    assert [capsule.radius_m for capsule in frame.obstacles[:3]] == [112.0, 1012.0, 32.0]
    assert frame.obstacles[0].from_m == (0.0, 0.0, 0.0)
    assert frame.obstacles[1].from_m == (0.0, 1e6, 0.0)
    assert math.dist(frame.goal_world_m, frame.goal_center_m) == pytest.approx(49.99)


def test_public_sensor_noise_cannot_be_silently_understated_by_config() -> None:
    observation = public_input()
    config = replace(
        PlannerConfig(), range_noise_m=0.0, range_rate_noise_mps=0.0, diameter_noise_fraction=0.0
    )
    assert (
        predict_obstacles(observation, config).obstacles
        == predict_obstacles(observation, PlannerConfig()).obstacles
    )
    observation["sensor_model"]["diameter_noise_fraction"] = 0.5
    with pytest.raises(ValueError, match="diameter_noise_fraction"):
        predict_obstacles(observation, config)


def test_private_data_and_identifiers_are_not_inputs_and_nothing_is_mutated() -> None:
    observation = public_input()
    before = copy.deepcopy(observation)
    expected = predict_obstacles(observation, PlannerConfig())
    assert observation == before
    changed = copy.deepcopy(observation)
    changed["assets"] = [{"position": [math.nan] * 3, "velocity": [1e300] * 3}]
    changed["observations"] = [{"id": "private", "relative_velocity_mps": [math.inf] * 3}]
    changed["shuttle"]["id"] = "not used"
    changed["target"]["velocity"] = [math.nan] * 3
    changed["mission"]["shuttle_id"] = "not used"
    assert predict_obstacles(changed, PlannerConfig()) == expected
    command = basic_command(changed)
    original_command = basic_command(observation)
    np.testing.assert_array_equal(command[0], original_command[0])
    np.testing.assert_array_equal(command[1], original_command[1])
    assert observation == before


@pytest.mark.parametrize("measurement_time", [-1.0, 10.0 + 1e-9, 4.5])
def test_prediction_rejects_negative_future_and_stale_timestamps(measurement_time) -> None:
    observation = public_input()
    observation["radar"][0][2][0] = measurement_time
    with pytest.raises(ValueError):
        predict_obstacles(observation, PlannerConfig())


@pytest.mark.parametrize(
    "radar",
    [
        [],
        [[0.0, 1.0, 0.0], [1.0, 0.0, 2.0], [10.0, 0.0, 0.0]],
        [[[math.nan, 1.0, 0.0], [1.0, 0.0, 2.0], [10.0, 0.0, 0.0]]],
        [[[0.0, 0.0, 0.0], [1.0, 0.0, 2.0], [10.0, 0.0, 0.0]]],
        [[[0.0, 1.0, 0.0], [1.0, 0.0, 0.0], [10.0, 0.0, 0.0]]],
        [[[0.0, 1.0, 0.0], [1.0, 0.0, 2.0], [10.0, 1.0, 0.0]]],
        np.zeros((4097, 3, 3)),
    ],
)
def test_prediction_rejects_missing_or_malformed_radar(radar) -> None:
    observation = public_input()
    observation["radar"] = radar
    with pytest.raises(ValueError):
        predict_obstacles(observation, PlannerConfig())


def test_signed_noisy_closest_range_is_preserved_instead_of_rejected_or_clipped() -> None:
    observation = public_input()
    observation["radar"][0][1][0] = -1.0
    original = copy.deepcopy(observation)
    frame = predict_obstacles(observation, PlannerConfig())
    assert frame.measured_obstacles == 1
    assert observation == original
    accel, angular = basic_command(observation)
    assert np.isfinite(accel).all() and np.isfinite(angular).all()


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("geometry", "phobos_enclosing_radius_m", 0.0),
        ("geometry", "arrival_radius_m", 32.0),
        ("geometry", "debris_count", True),
        ("environment", "mars_radius_m", -1.0),
        ("environment", "mars_position_m", [math.nan, 0.0, 0.0]),
        ("shuttle", "position", [True, False, True]),
        ("shuttle", "velocity", [0.0, 0.0]),
        ("target", "radius_m", math.inf),
    ],
)
def test_prediction_rejects_bad_public_geometry_and_state(section, key, value) -> None:
    observation = public_input()
    observation[section][key] = value
    with pytest.raises(ValueError):
        predict_obstacles(observation, PlannerConfig())


def test_missing_geometry_and_invalid_seed_do_not_fall_back_to_scenario_files() -> None:
    observation = public_input()
    del observation["geometry"]
    with pytest.raises(ValueError, match="geometry"):
        predict_obstacles(observation, PlannerConfig())
    observation = public_input()
    observation["seed"] = 2**32
    with pytest.raises(ValueError, match="seed"):
        predict_obstacles(observation, PlannerConfig())


def test_outward_landed_departure_omits_only_the_phobos_enclosure() -> None:
    observation = small_input()
    observation["geometry"]["phobos_enclosing_radius_m"] = 10.0
    observation["shuttle"].update(position=[9.9, 0.0, 0.0], status="landed")
    observation["mission"]["phase"] = "on_pad"
    config = small_config()
    frame = predict_obstacles(observation, config)
    assert frame.has_surface_departure
    assert frame.departure_world_m[0] == (9.9, 0.0, 0.0)
    assert frame.start_world_m == pytest.approx((11.1, 0.0, 0.0))
    assert segment_clear(*frame.departure_world_m, frame.obstacles[1:])
    assert not segment_clear(*frame.departure_world_m, frame.obstacles)
    route = plan_route(frame, config)
    assert route.status == "candidate"
    np.testing.assert_array_equal(route.points_world_m[0], frame.departure_world_m[0])
    assert "not verified terrain" in route.detail
    assert all(
        segment_clear(a, b, frame.obstacles)
        for a, b in zip(route.points_world_m[1:-1], route.points_world_m[2:], strict=True)
    )


@pytest.mark.parametrize(
    ("position", "landed"),
    [([0.0, 0.0, 0.0], True), ([5.0, 0.0, 0.0], True), ([9.9, 0.0, 0.0], False)],
)
def test_obvious_or_unreported_interior_starts_are_rejected(position, landed) -> None:
    observation = small_input()
    observation["geometry"]["phobos_enclosing_radius_m"] = 10.0
    observation["shuttle"].update(position=position, status="landed" if landed else "coasting")
    with pytest.raises(ValueError, match="terrain is unknown"):
        predict_obstacles(observation, small_config())


def test_departure_is_blocked_by_a_radar_envelope() -> None:
    observation = small_input()
    observation["geometry"]["phobos_enclosing_radius_m"] = 10.0
    observation["shuttle"].update(position=[9.9, 0.0, 0.0], status="landed")
    observation["radar"] = [[[0.4, 0.0, 0.0], [0.4, 0.0, 0.2], [10.0, 0.0, 0.0]]]
    with pytest.raises(ValueError, match="departure is blocked"):
        predict_obstacles(observation, small_config())


def test_empty_space_direct_visibility_keeps_exact_endpoints_without_expansion() -> None:
    start, goal = (1.25, -2.5, 0.125), (7.3, 4.1, -1.9)
    result = search_route(start, goal, (), small_config())
    assert result.points_world_m == (start, goal)
    assert (result.expanded_nodes, result.stored_nodes, result.peak_heap_entries) == (0, 0, 0)
    assert search_route(start, start, (), small_config()).points_world_m == (start,)


def test_sphere_detour_and_checked_smoothing_preserve_the_exact_goal() -> None:
    start, goal = (0.0, 0.0, 0.0), (8.35, 0.0, 0.0)
    obstacles = [sphere((4, 0, 0), 1.3)]
    result = search_route(start, goal, obstacles, small_config())
    assert len(result.points_world_m) > 2
    assert_clear_path(result, start, goal, obstacles)
    length = sum(
        math.dist(a, b)
        for a, b in zip(result.points_world_m[:-1], result.points_world_m[1:], strict=True)
    )
    assert length > math.dist(start, goal)
    assert result.expanded_nodes <= 2000
    assert result.stored_nodes <= 4000
    assert result.peak_heap_entries <= 8000


def test_wall_requires_a_three_dimensional_detour() -> None:
    start, goal = (0.0, 0.0, 0.0), (8.0, 0.0, 0.0)
    obstacles = [Capsule((4, -20, 0), (4, 20, 0), 1.25)]
    result = search_route(start, goal, obstacles, small_config())
    assert any(abs(value[2]) > 1.25 for value in result.points_world_m)
    assert_clear_path(result, start, goal, obstacles)


def test_corridor_rotates_with_the_start_to_goal_direction() -> None:
    start = np.array([20.0, -10.0, 3.0])
    direction = np.array([1.0, 2.0, 3.0]) / math.sqrt(14.0)
    goal = start + direction * 8.35
    obstacles = [sphere(start + direction * 4.0, 1.3)]
    config = small_config()
    result = search_route(start, goal, obstacles, config)
    corridor = _Corridor.create(tuple(start), tuple(goal), config)
    for value in result.points_world_m:
        offset = np.array(value) - start
        assert -2.000001 <= np.dot(offset, corridor.forward) <= 10.000001
        assert abs(np.dot(offset, corridor.across)) <= 4.000001
        assert abs(np.dot(offset, corridor.up)) <= 4.000001
    assert_clear_path(result, start, goal, obstacles)


def test_exact_endpoint_blockage_is_not_rounded_to_lattice() -> None:
    start, goal = (0.0, 0.0, 0.0), (8.35, 0.0, 0.0)
    blocked_start = search_route(start, goal, [sphere(start, 0.1)], small_config())
    blocked_goal = search_route(start, goal, [sphere(goal, 0.1)], small_config())
    assert not blocked_start.points_world_m and "start is blocked" in blocked_start.detail
    assert not blocked_goal.points_world_m and "goal is blocked" in blocked_goal.detail
    obstacles = [sphere((8, 0, 0), 0.2)]
    result = search_route(start, goal, obstacles, small_config())
    assert_clear_path(result, start, goal, obstacles)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"max_nodes": 1}, "stored-node limit"),
        ({"max_expansions": 1}, "expansion limit"),
        ({"max_heap_entries": 1}, "heap-entry storage limit"),
    ],
)
def test_search_budgets_stop_without_partial_routes(changes, reason) -> None:
    config = small_config(**changes)
    result = search_route((0, 0, 0), (8, 0, 0), [sphere((4, 0, 0), 1.3)], config)
    assert not result.points_world_m
    assert reason in result.detail
    assert result.stored_nodes <= config.max_nodes
    assert result.expanded_nodes <= config.max_expansions
    assert result.peak_heap_entries <= config.max_heap_entries


def test_sealed_corridor_and_huge_sparse_corridor_terminate() -> None:
    wall = [Capsule((4, -100, 0), (4, 100, 0), 1.25)]
    sealed = search_route((0, 0, 0), (8, 0, 0), wall, small_config(corridor_half_width_m=1.0))
    assert not sealed.points_world_m
    assert "bounded corridor" in sealed.detail
    sparse = search_route(
        (0, 0, 0),
        (3500, 0, 0),
        [sphere((1750, 0, 0), 100)],
        small_config(cell_size_m=40.0, corridor_half_width_m=40_000_000.0, max_nodes=4),
    )
    assert sparse.stored_nodes <= 4
    assert not sparse.points_world_m
    assert "stored-node limit" in sparse.detail


def test_repeated_searches_are_deterministic() -> None:
    obstacles = [sphere((4, 0, 0), 1.3), sphere((7, 1, 0), 1.1)]
    one = search_route((0, 0, 0), (10.35, 0, 0), obstacles, small_config())
    two = search_route((0, 0, 0), (10.35, 0, 0), obstacles, small_config())
    assert one == two
    assert one.points_world_m


def test_cell_index_and_unrepresentable_world_geometry_are_rejected() -> None:
    with pytest.raises(ValueError, match="cell indices"):
        search_route((0, 0, 0), (8, 0, 0), (), small_config(cell_size_m=1e-300))
    maximum = float(np.finfo(float).max)
    with pytest.raises(ValueError, match="subtraction"):
        search_route((-maximum, 0, 0), (maximum, 0, 0), (), small_config())


def test_plan_preview_exact_schema_contains_json_numbers_and_no_commands() -> None:
    observation = small_input()
    before = copy.deepcopy(observation)
    config = small_config()
    route = plan_route(predict_obstacles(observation, config), config)
    assert route.status == "candidate"
    assert route.points_world_m.ndim == 2 and route.points_world_m.shape[1] == 3
    assert not route.points_world_m.flags.writeable
    preview = route.to_preview()
    assert set(preview) == {
        "source_seed",
        "source_time_s",
        "status",
        "detail",
        "points_world_m",
        "goal_center_m",
        "goal_radius_m",
        "measured_obstacles",
        "unmodelled_obstacles_lower_bound",
        "forecast_horizon_s",
        "max_measurement_age_s",
        "oldest_measurement_time_s",
        "newest_measurement_time_s",
        "assumed_tangential_speed_allowance_mps",
        "expanded_nodes",
    }
    assert json.loads(json.dumps(preview, allow_nan=False)) == preview
    assert preview["points_world_m"][0] == observation["shuttle"]["position"]
    assert (
        math.dist(preview["points_world_m"][-1], preview["goal_center_m"])
        <= preview["goal_radius_m"]
    )
    assert observation == before
    with pytest.raises(ValueError, match="Config changed"):
        plan_route(route.frame, replace(config, clearance_m=0.1))


def test_plan_budget_failure_exports_no_partial_candidate() -> None:
    observation = small_input()
    observation["radar"] = [[[4.0, 0.0, 0.0], [4.0, 0.0, 2.6], [10.0, 0.0, 0.0]]]
    config = small_config(max_nodes=1)
    route = plan_route(predict_obstacles(observation, config), config)
    assert route.status == "no_candidate"
    assert "stored-node limit" in route.detail
    assert route.points_world_m.shape == (0, 3)
    assert route.to_preview()["points_world_m"] == []
    assert route.expanded_nodes <= config.max_expansions


def test_preview_rejects_oversize_paths_without_truncating_or_shortcutting() -> None:
    config = small_config()
    frame = predict_obstacles(small_input(), config)
    points = tuple((float(index), 0.0, 0.0) for index in range(MAX_PREVIEW_POINTS + 1))
    route = RoutePlan(
        frame, points, "candidate", "Constructed numeric path for schema size validation"
    )
    with pytest.raises(ValueError, match="no points were truncated"):
        route.to_preview()
    assert len(route.points_world_m) == MAX_PREVIEW_POINTS + 1


def test_preview_rejects_bad_status_times_and_unfinished_endpoints() -> None:
    config = small_config()
    route = plan_route(predict_obstacles(small_input(), config), config)
    for bad in (
        replace(route, status="safe"),
        replace(route, status="no_candidate"),
        replace(route, detail=""),
        replace(route, points_world_m=((10.0, 0.0, 0.0), (11.0, 0.0, 0.0))),
        replace(route, frame=replace(route.frame, source_time_s=math.nan)),
        replace(route, frame=replace(route.frame, newest_measurement_time_s=11.0)),
    ):
        with pytest.raises(ValueError):
            bad.to_preview()


def test_at_waypoint_is_not_a_command_and_still_requires_fresh_radar() -> None:
    observation = small_input()
    observation["shuttle"]["position"] = [17.5, 0.0, 0.0]
    config = small_config()
    route = plan_route(predict_obstacles(observation, config), config)
    assert route.status == "at_waypoint"
    assert route.points_world_m.shape == (0, 3)
    assert route.to_preview()["points_world_m"] == []
    observation["radar"] = []
    with pytest.raises(ValueError, match="Waiting for radar"):
        predict_obstacles(observation, config)


def test_basic_command_ports_position_velocity_control_and_goal_override() -> None:
    observation = public_input()
    observation["shuttle"].update(position=[0.0, 0.0, 0.0], velocity=[1.0, 2.0, 0.0])
    observation["target"]["position"] = [100.0, 0.0, 0.0]
    before = copy.deepcopy(observation)
    accel, angular = basic_command(observation)
    np.testing.assert_allclose(accel, [0.05, -0.14, 0.0], atol=1e-15)
    np.testing.assert_array_equal(angular, np.zeros(3))
    accel, _ = basic_command(observation, goal_world_m=[0.0, 100.0, 0.0])
    np.testing.assert_allclose(accel, [-0.07, -0.02, 0.0], atol=1e-15)
    assert observation == before


def test_basic_radar_repulsion_and_only_first_24_readings() -> None:
    observation = public_input()
    observation["shuttle"]["velocity"] = [0.0, 0.0, 0.0]
    close = [[10.0, 0.0, 0.0], [10.0, 0.0, 2.0], [10.0, 0.0, 0.0]]
    observation["radar"] = [close]
    accel, angular = basic_command(observation, position_gain_s2=0.0)
    np.testing.assert_allclose(accel, [-0.5, 0.0, 0.0])
    np.testing.assert_array_equal(angular, np.zeros(3))
    far = [[0.0, 100.0, 0.0], [100.0, 0.0, 2.0], [10.0, 0.0, 0.0]]
    observation["radar"] = [far] * 24 + [close]
    np.testing.assert_array_equal(basic_command(observation, position_gain_s2=0.0)[0], np.zeros(3))


@pytest.mark.parametrize(
    ("reported_limit", "expected_limit"), [(0.0, 0.0), (0.2, 0.2), (0.5, 0.5), (100.0, 0.5)]
)
def test_body_frame_commands_respect_reference_and_public_caps(
    reported_limit, expected_limit
) -> None:
    observation = public_input()
    observation["max_accel_mps2"] = reported_limit
    accel, angular = basic_command(observation, goal_world_m=[1e100, -1e100, 1e100])
    assert accel.shape == angular.shape == (3,)
    assert np.isfinite(accel).all() and np.isfinite(angular).all()
    assert math.hypot(*accel) <= expected_limit
    assert math.hypot(*accel) == pytest.approx(expected_limit)
    assert math.hypot(*angular) == 0.0


def test_passive_numeric_matrix_matches_the_pinned_official_helper_sign() -> None:
    # Numeric source-contract test, not a fabricated SDK reading or runtime check.
    sine, cosine = math.sin(math.pi / 4.0), math.cos(math.pi / 4.0)
    active = [0.0, 0.0, sine, cosine]
    np.testing.assert_allclose(
        reference_to_body([1.0, 0.0, 0.0], active, "active_xyzw"), [0.0, -1.0, 0.0], atol=1e-15
    )
    np.testing.assert_allclose(
        reference_to_body([1.0, 0.0, 0.0], active, "passive_xyzw"),
        [0.0, -1.0, 0.0],
        atol=1e-15,
    )
    np.testing.assert_allclose(
        reference_to_body([1.0, 0.0, 0.0], [0.0, 0.0, -sine, cosine], "passive_xyzw"),
        [0.0, 1.0, 0.0],
        atol=1e-15,
    )
    observation = small_input()
    observation["shuttle"]["rotation"] = active
    local_command = basic_command(observation)[0]
    observation["rotation_convention"] = "passive_xyzw"
    np.testing.assert_allclose(basic_command(observation)[0], local_command, atol=1e-15)


@pytest.mark.parametrize("rotation", [[0.0] * 4, [0.0, 0.0, math.nan, 1.0], [0.0, 0.0, 1.0]])
def test_bad_quaternions_are_rejected(rotation) -> None:
    observation = public_input()
    observation["shuttle"]["rotation"] = rotation
    with pytest.raises(ValueError, match="rotation"):
        basic_command(observation)


def test_controller_requires_a_convention_and_aligned_fresh_measurement_times() -> None:
    observation = public_input()
    del observation["rotation_convention"]
    with pytest.raises(ValueError, match="rotation_convention"):
        basic_command(observation)
    observation["rotation_convention"] = "unknown"
    with pytest.raises(ValueError, match="rotation_convention"):
        basic_command(observation)
    observation["rotation_convention"] = "active_xyzw"
    observation["radar"][0][2][0] = 10.1
    with pytest.raises(ValueError, match="clock origin"):
        basic_command(observation)
    observation["radar"][0][2][0] = 4.5
    with pytest.raises(ValueError, match="Stale radar"):
        basic_command(observation)


def test_empty_radar_does_not_invent_repulsion_and_coast_is_only_zero_vectors() -> None:
    observation = public_input()
    observation["radar"] = []
    accel, angular = basic_command(observation, position_gain_s2=0.0, velocity_gain_s=0.0)
    np.testing.assert_array_equal(accel, np.zeros(3))
    np.testing.assert_array_equal(angular, np.zeros(3))
    first, second = coast_command()
    first[0] = 1.0
    np.testing.assert_array_equal(second, np.zeros(3))
    np.testing.assert_array_equal(coast_command(observation)[0], np.zeros(3))


def test_algorithm_modules_do_not_import_backend_sdk_notebook_or_simulation() -> None:
    directory = Path(__file__).parents[1] / "src" / "wsorb" / "flight"
    prohibited = {"local_api", "stp_api", "marimo", "spaceteams", "simulation", "scenario"}
    for path in directory.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(not prohibited.intersection(name.split(".")) for name in names), path


def resilient_numeric_input(time_s=10.0, sequence=100) -> dict:
    """Constructed versioned-contract inputs, never sensor or backend evidence."""
    observation = public_input()
    observation.update(
        time_s=time_s,
        trial_revision=1,
        tuning_revision=0,
        manual_interventions=0,
        radar_clock="simulation_elapsed_seconds",
        navigation_sample_time_s=time_s,
        ship_tuning={"mass_kg": 3388.7, "inertia_scale": 1.0, "thrust_scale": 1.0},
    )
    observation["shuttle"]["velocity"] = [0.0, 0.0, 0.0]
    observation["radar"][0][2][0] = time_s
    observation["radar_backup"] = [[[0.0, -300.0, 0.0], [299.0, -1.0, 2.0], [time_s, 0.0, 0.0]]]
    identities = ("radar_primary", "radar_backup", "camera_top", "camera_bottom", "navigation")
    observation["sensor_health"] = {
        "schema_version": 1,
        "revision": 0,
        "units": [
            {
                "id": identity,
                "delivery": "ready",
                "active_faults": [],
                "sample_time_s": time_s,
                "sequence": sequence,
                "provenance": f"Constructed numeric {identity} contract, not observed data",
                "noise": dict(observation["sensor_model"]),
                "shared_fault_domains": ["radar_bus", "all_avionics"]
                if identity.startswith("radar_")
                else ["all_avionics"],
            }
            for identity in identities
        ],
    }
    return observation


def numeric_unit(observation, identity):
    return next(unit for unit in observation["sensor_health"]["units"] if unit["id"] == identity)


def numeric_checked_route(guidance, receipt):
    settings = guidance.config.planner
    frame = guidance.frame_for_planning(predict_obstacles(receipt.observation, settings))
    return plan_route(frame, settings)


def test_resilience_defaults_are_immutable_bounded_and_respect_planner_freshness():
    config = ResilienceConfig()
    assert config.lease_sim_s == 0.75
    assert config.arrival_inset_m == 1.0
    assert config.planner.forecast_horizon_s == 6.0
    assert PlannerConfig().forecast_horizon_s == 3.6
    assert config.recovery_samples >= 2
    assert config.recovery_dwell_s > 0
    assert config.radar_age_s <= config.planner.max_measurement_age_s
    strict = replace(config, planner=replace(config.planner, max_measurement_age_s=0.2))
    assert strict.radar_age_s == 0.2
    with pytest.raises(FrozenInstanceError):
        config.lease_sim_s = 1.0


@pytest.mark.parametrize(
    "changes",
    [
        {"lease_sim_s": 0.049},
        {"lease_sim_s": 1.001},
        {"lease_sim_s": True},
        {"wall_no_progress_s": math.inf},
        {"recovery_samples": 1},
        {"recovery_dwell_s": 0.0},
        {"history_limit": 4097},
        {"history_window_s": 301.0},
        {"position_allowance_m": 21.0},
        {"velocity_allowance_mps": -1.0},
        {"max_accel_mps2": 0.51},
        {"degraded_max_accel_mps2": 0.6},
        {"degraded_max_speed_mps": 9.0},
        {"arrival_inset_m": 0.0},
        {"max_stopping_horizon_s": 0.0},
        {"max_stopping_horizon_s": 60.1},
        {"planner": None},
    ],
)
def test_resilience_rejects_invalid_assumptions_without_any_engine(changes):
    with pytest.raises(ValueError):
        ResilienceConfig(**changes)


def test_direct_state_estimator_corrects_only_new_samples_and_propagates_model_uncertainty():
    config = ResilienceConfig()
    estimator = StateEstimator(config)
    sample = NavigationSample(2.0, (10.0, 20.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    assert estimator.correct(sample)
    assert not estimator.correct(sample)
    later = estimator.estimate(3.0)
    assert later.position_m == (12.0, 20.0, 0.0)
    assert later.velocity_mps == sample.velocity_mps
    assert later.source_time_s == 2.0
    assert later.position_uncertainty_m > config.position_allowance_m
    assert later.velocity_uncertainty_mps > config.velocity_allowance_mps
    assert "model_allowances" in later.to_report()["uncertainty_kind"]
    assert estimator.at(1.0) is None
    with pytest.raises(ValueError, match="without a new source timestamp"):
        estimator.correct(replace(sample, position_m=(11.0, 20.0, 0.0)))
    with pytest.raises(ValueError, match="rewound"):
        estimator.correct(replace(sample, time_s=1.0))


def test_estimator_history_has_time_and_count_bounds():
    config = ResilienceConfig(history_limit=3, history_window_s=1.0)
    estimator = StateEstimator(config)
    for timestamp in (1.0, 1.4, 1.8, 2.2, 4.0):
        estimator.correct(
            NavigationSample(
                timestamp, (timestamp, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)
            )
        )
        assert len(estimator.history) <= 3
        assert timestamp - estimator.history[0].time_s <= config.history_window_s
    assert len(estimator.history) == 1


def test_resilient_update_preserves_actual_rows_and_raw_navigation_with_defensive_copies():
    observation = resilient_numeric_input()
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=1.0, running=False)
    assert receipt.can_command and receipt.requires_replan
    assert receipt.lease_sim_s == 0.75
    assert receipt.report["selected_channel"] == "radar_primary"
    assert receipt.report["next_waypoint_world_m"] is None
    assert receipt.report["source_time_s"] == observation["time_s"]
    assert receipt.observation["radar"] == observation["radar"]
    assert receipt.observation["shuttle"] == observation["shuttle"]
    assert receipt.observation["target"] == observation["target"]
    receipt.observation["shuttle"]["position"][0] = 99
    receipt.report["mode"] = "invented"
    assert receipt.observation["shuttle"]["position"][0] == 1000.0
    assert guidance.report["mode"] == "nominal"
    assert observation == before
    assert json.loads(json.dumps(guidance.report, allow_nan=False)) == guidance.report
    with pytest.raises(FrozenInstanceError):
        receipt.can_command = False


def test_resilient_planning_only_adds_uncertainty_and_requires_registered_frames():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    original = predict_obstacles(receipt.observation, guidance.config.planner)
    inflated = guidance.frame_for_planning(original)
    for before, after in zip(original.obstacles, inflated.obstacles, strict=True):
        assert before.from_m == after.from_m and before.to_m == after.to_m
        assert after.radius_m > before.radius_m
    assert receipt.observation["radar"] == observation["radar"]
    unregistered = plan_route(original, original.config)
    np.testing.assert_array_equal(guidance.command(observation, unregistered, {})[0], np.zeros(3))
    route = plan_route(inflated, inflated.config)
    acceleration, angular = guidance.command(observation, route, {})
    assert 0.0 < math.hypot(*acceleration) <= 0.5
    np.testing.assert_array_equal(angular, np.zeros(3))
    assert guidance.report["next_waypoint_world_m"] == route.points_world_m[1].tolist()


def test_primary_loss_selects_actual_backup_and_invalidates_old_route_immediately():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    initial = guidance.update(observation, wall_time_s=0.0, running=True)
    old_route = numeric_checked_route(guidance, initial)
    nominal, _ = guidance.command(observation, old_route, {})
    failed = copy.deepcopy(observation)
    failed["sensor_health"]["revision"] += 1
    primary = numeric_unit(failed, "radar_primary")
    primary.update(delivery="unavailable", active_faults=["radar_primary"])
    receipt = guidance.update(failed, wall_time_s=0.1, running=True)
    assert receipt.can_command and receipt.requires_replan
    assert receipt.report["selected_channel"] == "radar_backup"
    assert receipt.report["mode"] == "degraded"
    assert receipt.observation["radar"] == failed["radar_backup"]
    assert receipt.observation["sensor_model"] == numeric_unit(failed, "radar_backup")["noise"]
    np.testing.assert_array_equal(guidance.command(failed, old_route, {})[0], np.zeros(3))
    route = numeric_checked_route(guidance, receipt)
    degraded, _ = guidance.command(failed, route, {})
    assert 0.0 < math.hypot(*degraded) < math.hypot(*nominal)
    assert guidance.report["reference_accel_limit_mps2"] == 0.25


@pytest.mark.parametrize("fault", ["radar_bus", "all_avionics"])
def test_shared_failure_domains_block_ready_units_without_hidden_fallback(fault):
    observation = resilient_numeric_input()
    # Deliberately contradictory numeric metadata checks domain gating itself.
    numeric_unit(observation, "radar_primary")["active_faults"] = [fault]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    assert not receipt.can_command and receipt.lease_sim_s == 0.0
    assert receipt.report["selected_channel"] is None
    assert receipt.observation["radar"] == []
    assert receipt.report["navigation_redundancy"] == "none"
    assert receipt.report["camera_navigation"] == "not_implemented"
    np.testing.assert_array_equal(guidance.command(observation, None, {})[0], np.zeros(3))


def test_frozen_navigation_is_not_replaced_by_radar_camera_or_physics_truth():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=True)
    frozen = copy.deepcopy(observation)
    frozen["time_s"] += 0.5
    frozen["sensor_health"]["revision"] += 1
    numeric_unit(frozen, "navigation").update(delivery="frozen", active_faults=["navigation"])
    frozen["applied_physics"] = {"position_world_m": [1e6, 1e6, 1e6]}
    receipt = guidance.update(frozen, wall_time_s=0.5, running=True)
    assert not receipt.can_command
    assert receipt.observation["shuttle"] == observation["shuttle"]
    assert receipt.report["estimate"]["source_time_s"] == 10.0
    assert receipt.report["estimate"]["position_world_m"] == [1000.0, 0.0, 0.0]
    assert receipt.report["estimate"]["age_s"] == 0.5


def test_outage_recovery_requires_distinct_samples_dwell_and_a_new_checked_route():
    config = ResilienceConfig(recovery_samples=3, recovery_dwell_s=0.5)
    guidance = ResilientGuidance(config)
    observation = resilient_numeric_input()
    initial = guidance.update(observation, wall_time_s=0.0, running=True)
    old_route = numeric_checked_route(guidance, initial)
    failed = copy.deepcopy(observation)
    failed["sensor_health"]["revision"] = 1
    numeric_unit(failed, "radar_primary")["active_faults"] = ["radar_bus"]
    assert not guidance.update(failed, wall_time_s=0.1, running=True).can_command
    recovered = resilient_numeric_input(10.1, 101)
    recovered["sensor_health"]["revision"] = 2
    first = guidance.update(recovered, wall_time_s=0.2, running=True)
    assert not first.can_command and first.report["recovery_samples"] == 1
    for wall in (0.3, 0.4, 0.5):
        repeated = guidance.update(recovered, wall_time_s=wall, running=True)
        assert not repeated.can_command
        assert repeated.report["recovery_samples"] == 1
    second = resilient_numeric_input(10.2, 102)
    second["sensor_health"]["revision"] = 2
    assert not guidance.update(second, wall_time_s=0.6, running=True).can_command
    third = resilient_numeric_input(10.8, 103)
    third["sensor_health"]["revision"] = 2
    ready = guidance.update(third, wall_time_s=0.8, running=True)
    assert ready.can_command and ready.requires_replan
    np.testing.assert_array_equal(guidance.command(third, old_route, {})[0], np.zeros(3))
    fresh_route = numeric_checked_route(guidance, ready)
    assert math.hypot(*guidance.command(third, fresh_route, {})[0]) > 0.0


def test_primary_failback_has_its_own_distinct_sample_hysteresis():
    guidance = ResilientGuidance()
    observation = resilient_numeric_input()
    numeric_unit(observation, "radar_primary")["delivery"] = "unavailable"
    initial = guidance.update(observation, wall_time_s=0.0, running=True)
    assert initial.can_command and initial.report["selected_channel"] == "radar_backup"
    for index, timestamp in enumerate((10.1, 10.2, 10.7), start=1):
        restored = resilient_numeric_input(timestamp, 100 + index)
        receipt = guidance.update(restored, wall_time_s=float(index), running=True)
        expected = "radar_primary" if index == 3 else "radar_backup"
        assert receipt.report["selected_channel"] == expected
    assert receipt.requires_replan


def test_running_wall_stall_expires_even_when_simulation_time_is_held():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    assert guidance.update(observation, wall_time_s=0.0, running=True).can_command
    held = guidance.update(observation, wall_time_s=2.01, running=True)
    assert not held.can_command
    assert held.report["ownship_history_samples"] == 1
    paused = ResilientGuidance()
    assert paused.update(observation, wall_time_s=0.0, running=False).can_command
    assert paused.update(observation, wall_time_s=100.0, running=False).can_command


@pytest.mark.parametrize("change", ["missing", "version", "duplicate", "domain", "future"])
def test_malformed_health_fails_closed_despite_valid_legacy_radar(change):
    observation = resilient_numeric_input()
    if change == "missing":
        del observation["sensor_health"]
    elif change == "version":
        observation["sensor_health"]["schema_version"] = True
    elif change == "duplicate":
        observation["sensor_health"]["units"][1]["id"] = "radar_primary"
    elif change == "domain":
        numeric_unit(observation, "radar_backup")["shared_fault_domains"] = ["all_avionics"]
    else:
        numeric_unit(observation, "radar_primary")["sample_time_s"] += 1.0
    receipt = ResilientGuidance().update(observation, wall_time_s=0.0, running=True)
    assert not receipt.can_command and receipt.observation["radar"] == []
    json.dumps(receipt.report, allow_nan=False)


def test_ready_empty_scans_are_reported_but_never_assumed_clear():
    observation = resilient_numeric_input()
    observation["radar"] = observation["radar_backup"] = []
    receipt = ResilientGuidance().update(observation, wall_time_s=0.0, running=True)
    assert not receipt.can_command
    assert all(unit["delivery"] == "ready" for unit in receipt.report["units"])
    assert "Ready empty scan" in receipt.report["assessments"]["radar_primary"]["reason"]


def test_same_timestamp_mutation_or_sequence_only_progress_cannot_refresh_navigation():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=True)
    changed = copy.deepcopy(observation)
    changed["shuttle"]["position"][0] += 1.0
    assert not guidance.update(changed, wall_time_s=0.1, running=True).can_command
    sequence_only = copy.deepcopy(observation)
    numeric_unit(sequence_only, "navigation")["sequence"] += 1
    assert not guidance.update(sequence_only, wall_time_s=0.2, running=True).can_command
    assert guidance.report["ownship_history_samples"] == 1


@pytest.mark.parametrize("change", ["manual", "trial", "frame", "rewind"])
def test_context_discontinuities_clear_history_and_never_auto_resume_consent(change):
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=True)
    changed = resilient_numeric_input(11.0, 110)
    if change == "manual":
        changed["manual_interventions"] += 1
    elif change == "trial":
        changed["trial_revision"] += 1
    elif change == "frame":
        changed["frame"] = "another constructed reference frame"
    else:
        changed = resilient_numeric_input(9.0, 90)
    receipt = guidance.update(changed, wall_time_s=0.1, running=True)
    assert not receipt.can_command and receipt.report["requires_consent"]
    assert receipt.report["ownship_history_samples"] == 1
    repeated = guidance.update(changed, wall_time_s=0.2, running=True)
    assert not repeated.can_command and repeated.report["requires_consent"]


def test_lease_is_shortened_to_stricter_sample_horizon_or_disabled_by_uncertainty():
    observation = resilient_numeric_input()
    settings = replace(PlannerConfig(), max_measurement_age_s=0.2)
    guidance = ResilientGuidance(ResilienceConfig(planner=settings))
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    assert receipt.can_command and receipt.lease_sim_s == pytest.approx(0.2)
    blocked = ResilientGuidance(ResilienceConfig(max_position_uncertainty_m=0.25))
    result = blocked.update(observation, wall_time_s=0.0, running=True)
    assert not result.can_command and result.lease_sim_s == 0.0


def test_command_uses_cv_estimate_without_relabelling_the_held_raw_navigation():
    observation = resilient_numeric_input()
    observation["shuttle"]["velocity"] = [1.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=True)
    held = copy.deepcopy(observation)
    held["time_s"] = 10.2
    receipt = guidance.update(held, wall_time_s=0.2, running=True)
    assert receipt.can_command
    assert receipt.observation["shuttle"]["position"] == [1000.0, 0.0, 0.0]
    assert receipt.report["estimate"]["position_world_m"] == pytest.approx([1000.2, 0.0, 0.0])
    assert receipt.report["ownship_history_samples"] == 1
    route = numeric_checked_route(guidance, receipt)
    assert np.isfinite(guidance.command(held, route, {})[0]).all()


def test_route_progress_is_monotonic_and_reported_target_matches_the_controller():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    original = numeric_checked_route(guidance, receipt)
    route = replace(
        original,
        points_world_m=(
            (1000.0, 0.0, 0.0),
            (1100.0, 0.0, 0.0),
            (1200.0, 0.0, 0.0),
            tuple(original.points_world_m[-1]),
        ),
    )
    assert math.hypot(*guidance.command(observation, route, {})[0]) > 0
    for index, position in enumerate((1090.0, 1125.0), start=1):
        current = resilient_numeric_input(10.0 + 0.1 * index, 100 + index)
        current["shuttle"]["position"][0] = position
        guidance.update(current, wall_time_s=0.1 * index, running=True)
        assert math.hypot(*guidance.command(current, route, {})[0]) > 0
        assert guidance.report["waypoint_index"] == 2
        assert guidance.report["next_waypoint_world_m"] == [1200.0, 0.0, 0.0]
        assert guidance.report["source_time_s"] == current["time_s"]


def test_stale_route_or_mismatched_decision_source_returns_only_zero_vectors():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    route = numeric_checked_route(guidance, receipt)
    mismatched = copy.deepcopy(observation)
    mismatched["time_s"] += 0.1
    np.testing.assert_array_equal(guidance.command(mismatched, route, {})[0], np.zeros(3))
    later = resilient_numeric_input(10.3, 103)
    receipt = guidance.update(later, wall_time_s=0.3, running=True)
    assert receipt.can_command and receipt.requires_replan
    np.testing.assert_array_equal(guidance.command(later, route, {})[0], np.zeros(3))
    assert "whole lease" in guidance.report["reason"]


def test_current_obstacle_envelopes_can_veto_a_previously_clear_route():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    route = numeric_checked_route(guidance, receipt)
    current = resilient_numeric_input(10.1, 101)
    current["radar"][0][:2] = [[20.0, 0.0, 0.0], [20.0, -1.0, 2.0]]
    guidance.update(current, wall_time_s=0.1, running=True)
    np.testing.assert_array_equal(guidance.command(current, route, {})[0], np.zeros(3))
    assert "intersects an uncertainty envelope" in guidance.report["reason"]


def test_immediate_swept_motion_is_checked_even_when_waypoint_connector_is_clear():
    settings = replace(
        PlannerConfig(),
        forecast_horizon_s=2.0,
        clearance_m=0.0,
        unknown_tangential_speed_mps=0.0,
        sigma_multiplier=0.0,
    )
    guidance = ResilientGuidance(ResilienceConfig(planner=settings))
    observation = resilient_numeric_input()
    observation["shuttle"]["velocity"] = [0.0, 20.0, 0.0]
    observation["radar"][0][:2] = [[0.0, 20.0, 0.0], [20.0, 0.0, 2.0]]
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    route = numeric_checked_route(guidance, receipt)
    assert route.status == "candidate"
    np.testing.assert_array_equal(guidance.command(observation, route, {})[0], np.zeros(3))
    assert "Immediate swept command path" in guidance.report["reason"]


def test_planning_provenance_cannot_reuse_primary_rows_after_backup_selection():
    observation = resilient_numeric_input()
    settings = PlannerConfig()
    primary_frame = predict_obstacles(observation, settings)
    numeric_unit(observation, "radar_primary")["delivery"] = "unavailable"
    guidance = ResilientGuidance()
    assert guidance.update(observation, wall_time_s=0.0, running=True).can_command
    with pytest.raises(ValueError, match="selected actual radar input"):
        guidance.frame_for_planning(primary_frame)


def test_history_anchoring_adds_uncertainty_without_moving_anonymous_radar_capsules():
    observation = resilient_numeric_input()
    observation["shuttle"]["velocity"] = [1.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    guidance.update(observation, wall_time_s=0.0, running=True)
    current = resilient_numeric_input(10.2, 102)
    current["shuttle"].update(position=[1001.0, 0.0, 0.0], velocity=[3.0, 0.0, 0.0])
    current["radar"] = copy.deepcopy(observation["radar"])
    unit = numeric_unit(current, "radar_primary")
    unit.update(sample_time_s=10.0, sequence=100)
    receipt = guidance.update(current, wall_time_s=0.2, running=True)
    assert receipt.can_command
    frame = predict_obstacles(receipt.observation, guidance.config.planner)
    inflated = guidance.frame_for_planning(frame)
    assert inflated.obstacles[3].from_m == frame.obstacles[3].from_m
    assert inflated.obstacles[3].to_m == frame.obstacles[3].to_m
    assert inflated.obstacles[3].radius_m > frame.obstacles[3].radius_m
    assert receipt.observation["radar"] == observation["radar"]
    assert receipt.report["ownship_history_samples"] == 2


def test_changed_sensor_revision_invalidates_route_even_without_a_clock_tick():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    route = numeric_checked_route(guidance, receipt)
    changed = copy.deepcopy(observation)
    changed["sensor_health"]["revision"] = 1
    numeric_unit(changed, "camera_top")["delivery"] = "unavailable"
    numeric_unit(changed, "camera_top")["active_faults"] = ["camera_top"]
    receipt = guidance.update(changed, wall_time_s=0.1, running=True)
    assert receipt.can_command and receipt.requires_replan
    np.testing.assert_array_equal(guidance.command(changed, route, {})[0], np.zeros(3))


def test_engine_lower_command_cap_and_edited_velocity_gain_reach_resilient_control():
    observation = resilient_numeric_input()
    observation["max_accel_mps2"] = 0.1
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=True)
    route = numeric_checked_route(guidance, receipt)
    slow, angular = guidance.command(observation, route, {"velocity_gain_s": 0.01})
    fast, _ = guidance.command(observation, route, {"velocity_gain_s": 10.0})
    assert 0.0 < math.hypot(*slow) < math.hypot(*fast) <= 0.1
    np.testing.assert_array_equal(angular, np.zeros(3))
    assert guidance.report["reference_accel_limit_mps2"] == 0.1


@pytest.mark.parametrize("horizon, fresh_cap", [(3.6, 0.6625), (6.0, 1.2625)])
@pytest.mark.parametrize("navigation_age", [0.0, 0.5])
def test_speed_budget_reports_forecast_and_real_source_age_limits(
    horizon, fresh_cap, navigation_age
):
    observation = resilient_numeric_input()
    settings = replace(PlannerConfig(), forecast_horizon_s=horizon)
    guidance = ResilientGuidance(ResilienceConfig(planner=settings, max_stopping_horizon_s=horizon))
    guidance.update(observation, wall_time_s=0.0, running=False)
    held = copy.deepcopy(observation)
    held["time_s"] += navigation_age
    receipt = guidance.update(held, wall_time_s=navigation_age, running=False)
    route = numeric_checked_route(guidance, receipt)
    accel, _ = guidance.command(held, route, {})
    cap = fresh_cap - 0.5 * navigation_age
    report = guidance.report
    assert report["speed_limiter"] == "forecast_braking_time"
    assert report["desired_speed_mps"] == pytest.approx(cap)
    assert accel[0] == pytest.approx(0.07 * cap)
    assert report["constant_speed_route_time_s"] == pytest.approx(
        report["route_remaining_distance_m"] / cap
    )
    assert report["braking_model_kind"] == "assumed_active_braking_not_fault_coasting"
    assert receipt.lease_sim_s == 0.75
    assert receipt.report["estimate"]["source_time_s"] == observation["time_s"]
    assert receipt.observation["radar"] == observation["radar"]


@pytest.mark.parametrize("range_m", [50.01, 50.24])
def test_checkpoint_interior_goal_does_not_coast_just_outside_the_real_region(range_m):
    observation = resilient_numeric_input()
    observation["shuttle"]["position"][0] = observation["target"]["position"][0] - range_m
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    assert route.status == "candidate"
    assert route.frame.goal_radius_m == observation["geometry"]["arrival_radius_m"] == 50.0
    assert math.dist(route.points_world_m[-1], observation["target"]["position"]) == pytest.approx(
        49.0
    )
    accel, _ = guidance.command(observation, route, {})
    assert accel[0] > 0.0
    assert guidance.report["arrival_goal_kind"] == "interior_candidate_not_mission_success"
    assert guidance.report["arrival_goal_inset_m"] == pytest.approx(1.0)


def test_checkpoint_inset_keeps_obstacles_and_rejects_an_unfittable_approach():
    observation = resilient_numeric_input()
    settings = PlannerConfig()
    guidance = ResilientGuidance(ResilienceConfig(arrival_inset_m=15.0))
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    frame = predict_obstacles(receipt.observation, settings)
    with pytest.raises(ValueError, match="Interior checkpoint approach intersects"):
        guidance.frame_for_planning(frame)
    assert frame.goal_radius_m == 50.0
    assert frame.goal_center_m == tuple(observation["target"]["position"])
    assert frame.goal_world_m[0] == pytest.approx(1450.01)


def test_default_resilient_forecast_raises_cruise_budget_without_extending_source_leases():
    observation = resilient_numeric_input()
    # Check the original short-forecast fallback independently of adaptive cruise.
    guidance = ResilientGuidance(ResilienceConfig(max_stopping_horizon_s=6.0))
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    accel, _ = guidance.command(observation, route, {})
    assert route.frame.forecast_horizon_s == 6.0
    assert route.frame.obstacles[2].radius_m == pytest.approx(41.55)
    assert guidance.report["desired_speed_mps"] == pytest.approx(1.2625)
    assert accel[0] == pytest.approx(0.088375)
    assert receipt.lease_sim_s == 0.75
    assert guidance.report["route_valid_until_s"] == observation["time_s"] + 1.0
    assert guidance.report["decision_valid_until_s"] == observation["time_s"] + 0.75
    # This is a speed-budget calculation, not an observed flight or arrival.
    assert (3537.6122605878836 - 50.0) / guidance.report["desired_speed_mps"] < 2800.0


def test_refreshed_identical_route_suffix_does_not_restart_a_passed_waypoint():
    observation = resilient_numeric_input()
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    original = numeric_checked_route(guidance, receipt)
    route = replace(
        original,
        points_world_m=(
            (1000.0, 0.0, 0.0),
            (1100.0, 0.0, 0.0),
            (1200.0, 0.0, 0.0),
            tuple(original.points_world_m[-1]),
        ),
    )
    guidance.command(observation, route, {})
    near_corner = resilient_numeric_input(10.1, 101)
    near_corner["shuttle"]["position"][0] = 1090.0
    guidance.update(near_corner, wall_time_s=0.1, running=False)
    guidance.command(near_corner, route, {})
    assert guidance.report["waypoint_index"] == 2
    current = resilient_numeric_input(10.2, 102)
    current["shuttle"]["position"][0] = 1125.0
    receipt = guidance.update(current, wall_time_s=0.2, running=False)
    fresh = numeric_checked_route(guidance, receipt)
    refreshed = replace(
        fresh, points_world_m=(tuple(current["shuttle"]["position"]), *route.points_world_m[1:])
    )
    assert math.hypot(*guidance.command(current, refreshed, {})[0]) > 0
    assert guidance.report["waypoint_index"] == 2
    assert guidance.report["next_waypoint_world_m"] == [1200.0, 0.0, 0.0]
    changed = replace(
        fresh,
        points_world_m=(
            tuple(current["shuttle"]["position"]),
            (1160.0, 0.0, 0.0),
            (1300.0, 0.0, 0.0),
            tuple(original.points_world_m[-1]),
        ),
    )
    assert math.hypot(*guidance.command(current, changed, {})[0]) > 0
    assert guidance.report["waypoint_index"] == 1
    assert guidance.report["next_waypoint_world_m"] == [1160.0, 0.0, 0.0]


@pytest.mark.parametrize("lateral_speed, allowed", [(0.5, True), (2.0, False)])
def test_radial_departure_cancels_sideways_velocity_without_relaxing_its_gate(
    lateral_speed, allowed
):
    observation = resilient_numeric_input()
    observation["geometry"]["phobos_enclosing_radius_m"] = 1000.0
    observation["shuttle"].update(status="landed", velocity=[0.0, lateral_speed, 0.0])
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    assert route.frame.has_surface_departure
    accel, _ = guidance.command(observation, route, {})
    if not allowed:
        np.testing.assert_array_equal(accel, np.zeros(3))
        assert "supported outward radial segment" in guidance.report["reason"]
        return
    assert 0.0 < math.hypot(*accel) <= 0.5
    assert accel[0] > 0.0 and accel[1] < 0.0
    lease = receipt.lease_sim_s
    lateral_travel = lateral_speed * lease + 0.5 * accel[1] * lease * lease
    assert lateral_travel <= receipt.report["estimate"]["position_uncertainty_m"] + 0.01
    assert lateral_speed * lease > receipt.report["estimate"]["position_uncertainty_m"] + 0.01
    assert guidance.report["terrain_clearance_verified"] is False


def cruise_numeric_input():
    """A spacious constructed numeric scene, not observed flight evidence."""
    observation = resilient_numeric_input()
    observation["shuttle"]["position"] = [2000.0, 0.0, 0.0]
    observation["target"]["position"] = [10000.0, 0.0, 0.0]
    observation["radar"][0][:2] = [[0.0, 2000.0, 0.0], [2000.0, 0.0, 2.0]]
    return observation


def test_adaptive_cruise_checks_full_long_forecast_only_on_finite_stopping_prefix():
    observation = cruise_numeric_input()
    before = copy.deepcopy(observation)
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    accel, angular = guidance.command(observation, route, {})
    report = guidance.report
    assert report["desired_speed_mps"] == 8.0
    assert report["short_forecast_speed_mps"] == pytest.approx(1.2625)
    assert report["speed_limiter"] == "adaptive_stopping_corridor"
    assert report["planning_forecast_horizon_s"] == 6.0
    assert report["command_stopping_horizon_s"] == pytest.approx(32.95)
    assert report["command_stop_position_allowance_m"] > 270.0
    assert report["short_forecast_speed_limits_mps"]["forecast_braking_time"] == pytest.approx(
        1.2625
    )
    assert report["speed_limits_mps"]["forecast_braking_time"] == pytest.approx(8.0)
    assert len(report["stopping_candidates"]) == 1
    assert accel[0] == pytest.approx(0.5)
    np.testing.assert_array_equal(angular, np.zeros(3))
    settings = replace(guidance.config.planner, forecast_horizon_s=32.95)
    long = guidance._inflate(predict_obstacles(receipt.observation, settings))
    assert long.obstacles[2].radius_m > route.frame.goal_radius_m
    # The distant final goal is blocked under this horizon, but the whole
    # finite cruise/stop prefix remains clear of the unchanged inflated target.
    assert not segment_clear(
        tuple(route.points_world_m[-1]), tuple(route.points_world_m[-1]), long.obstacles
    )
    connector = np.array(observation["shuttle"]["position"])
    connector[0] += report["stopping_candidates"][0]["distance_m"]
    assert segment_clear(
        tuple(observation["shuttle"]["position"]), tuple(connector), long.obstacles
    )
    for short, extended in zip(route.frame.obstacles, long.obstacles, strict=True):
        assert extended.radius_m > short.radius_m
    assert long.oldest_measurement_time_s == route.frame.oldest_measurement_time_s == 10.0
    assert report["decision_valid_until_s"] == 10.75
    assert report["route_valid_until_s"] == 11.0
    assert receipt.lease_sim_s == 0.75
    assert observation == before


@pytest.mark.parametrize("radar_distance, expected_speed", [(200.0, 2.0), (130.0, 1.2625)])
def test_adaptive_cruise_falls_back_when_full_long_radar_uncertainty_blocks_it(
    radar_distance, expected_speed
):
    observation = cruise_numeric_input()
    observation["radar"][0][:2] = [[0.0, radar_distance, 0.0], [radar_distance, 0.0, 2.0]]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    accel, _ = guidance.command(observation, route, {})
    report = guidance.report
    assert report["desired_speed_mps"] == pytest.approx(expected_speed)
    assert len(report["stopping_candidates"]) == 3
    assert report["stopping_candidates"][0]["reason"] == "long forecast stopping/swept envelope"
    assert report["stopping_candidates"][1]["reason"] == "long forecast stopping/swept envelope"
    assert accel[0] == pytest.approx(0.07 * expected_speed)
    assert receipt.observation["radar"] == observation["radar"]


def test_adaptive_cruise_keeps_long_known_body_inflation_and_finite_work_budget():
    observation = cruise_numeric_input()
    observation["shuttle"]["position"] = [200.0, 0.0, 0.0]
    guidance = ResilientGuidance()
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    guidance.command(observation, route, {})
    report = guidance.report
    assert report["desired_speed_mps"] == 4.0
    assert report["stopping_candidates"][0]["reason"] == "long forecast known-body envelope"
    assert len(report["stopping_candidates"]) == 2
    assert report["command_stopping_horizon_s"] == pytest.approx(16.95)


def test_long_horizon_budget_failure_requests_active_braking_not_unchecked_cruise():
    observation = cruise_numeric_input()
    observation["shuttle"]["velocity"] = [8.0, 0.0, 0.0]
    guidance = ResilientGuidance(ResilienceConfig(max_stopping_horizon_s=6.0))
    receipt = guidance.update(observation, wall_time_s=0.0, running=False)
    route = numeric_checked_route(guidance, receipt)
    accel, _ = guidance.command(observation, route, {})
    report = guidance.report
    assert report["desired_speed_mps"] == pytest.approx(1.2625)
    assert report["active_braking_to_short_forecast"] is True
    assert -0.5 <= accel[0] <= -guidance.config.braking_accel_mps2
    assert len(report["stopping_candidates"]) == 3
    assert all(
        item["reason"] == "stopping horizon budget" for item in report["stopping_candidates"]
    )
    assert receipt.lease_sim_s == 0.75
