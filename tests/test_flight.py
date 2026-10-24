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
    RoutePlan,
    basic_command,
    coast_command,
    plan_route,
    predict_obstacles,
)
from wsorb.flight.control import reference_to_body
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
