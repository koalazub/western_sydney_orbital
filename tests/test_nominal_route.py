"""Constructed numerical geometry only, not sensor data or native flight outcomes."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from wsorb.flight.config import PlannerConfig
from wsorb.flight.geometry import Capsule, CollisionIndex
from wsorb.flight.nominal_route import NominalRouteAdviser, nominal_escape_waypoint
from wsorb.flight.temporal import TemporalObstacles


def numeric_geometry(centers, radii, velocities=None, growth=0.0):
    centers = np.asarray(centers, dtype=float)
    return TemporalObstacles(
        centers,
        np.zeros_like(centers) if velocities is None else np.asarray(velocities, dtype=float),
        np.asarray(radii, dtype=float),
        np.full(len(centers), growth),
        np.full(len(centers), growth),
    )


def nominal_index(obstacles, horizon=1.5):
    return CollisionIndex(
        Capsule(center, center + velocity * horizon, radius)
        for center, velocity, radius in zip(
            obstacles.centers_m, obstacles.velocities_mps, obstacles.radii_m, strict=True
        )
    )


def numeric_two_stage_barrier():
    # The large sphere blocks the direct line. Each of its four first portals
    # then has a separate small sphere on its connector to the local goal.
    return numeric_geometry(
        [
            (2100.0, 0.0, 0.0),
            (2150.0, 29.0, 0.0),
            (2150.0, -29.0, 0.0),
            (2150.0, 0.0, 29.0),
            (2150.0, 0.0, -29.0),
        ],
        [38.0, 15.0, 15.0, 15.0, 15.0],
    )


def finish_numeric_search(adviser, position, target, obstacles, config, **kwargs):
    """Advance calculated geometry, without constructing observations or leases."""
    results = []
    for index in range(40):
        advice = adviser.choose(position, target, obstacles, 10.0 + index * 0.1, config, **kwargs)
        results.append(advice)
        assert advice.expanded_nodes <= min(4, config.max_expansions)
        assert advice.search_expanded_nodes <= min(32, config.max_expansions)
        assert advice.collision_checks <= 24
        assert advice.stored_nodes <= min(64, config.max_nodes)
        assert advice.peak_heap_entries <= min(64, config.max_heap_entries)
        if advice.status != "planning":
            return advice, results
    pytest.fail("Finite constructed search did not terminate within its operation budgets")


def test_escape_waypoint_has_200_m_side_and_40_m_radial_offset():
    config = PlannerConfig()
    start = np.array((-13047.0, 0.0, 0.0))
    goal = (50000.0, 0.0, 0.0)
    waypoint = np.asarray(nominal_escape_waypoint(start, goal, config))
    np.testing.assert_array_equal(waypoint, (-13087.0, 200.0, 0.0))
    assert math.dist(start, waypoint) < 205.0
    np.testing.assert_array_equal(
        nominal_escape_waypoint(
            start, goal, replace(config, cell_size_m=20.0, corridor_half_width_m=80.0)
        ),
        (-13067.0, 80.0, 0.0),
    )


def test_nearly_radial_target_uses_a_stable_side_within_spatial_planning_resolution():
    position = np.array((-13028.892478202539, 0.0, 0.3077012401614041))
    target = (-16566.504725408508, 0.0, 0.0)
    waypoint = np.asarray(
        nominal_escape_waypoint(
            position,
            target,
            PlannerConfig(),
            radial_tolerance_m=4.0,
        )
    )
    normal = position / np.linalg.norm(position)
    delta = waypoint - position
    transverse = delta - normal * np.dot(delta, normal)
    np.testing.assert_allclose(transverse, (0.0, 200.0, 0.0), atol=1e-9)
    assert np.dot(delta, normal) == pytest.approx(40.0)


def test_inside_enclosure_has_no_sideways_geometric_exception():
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [13045.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    advice = adviser.choose(
        (-13028.0, 0.0, 0.3),
        (50000.0, 0.0, 0.0),
        geometry,
        0.0,
        PlannerConfig(),
        primary_goal=(-13068.0, 800.0, 0.3),
    )
    assert advice.status == "no_nominal_path"
    assert advice.target_world_m is None and advice.points_world_m == ()
    assert "radial departure belongs to the caller" in advice.detail


def test_outside_enclosure_can_follow_a_near_lateral_prefix_of_the_callers_bypass():
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [13045.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    start = (-13047.0, 0.0, 0.0)
    advice = adviser.choose(
        start,
        (50000.0, 0.0, 0.0),
        geometry,
        13.0,
        PlannerConfig(),
        primary_goal=(-13068.0, 800.0, 0.3),
    )
    assert advice.status == "local_advice"
    assert advice.target_world_m[1] > 199.0
    assert abs(advice.target_world_m[0] - start[0]) < 6.0
    assert math.dist(start, advice.local_goal_world_m) == pytest.approx(200.0)
    assert nominal_index(geometry).clear(start, advice.target_world_m)
    assert not advice.safety_clearance and not advice.complete_route
    assert "not_safety_clearance" in advice.kind


def test_local_endpoint_is_fixed_until_passed_not_pushed_forward_by_each_scan():
    config = PlannerConfig()
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [1000.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    first = adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, 10.0, config)
    second = adviser.choose((2005.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, 11.0, config)
    assert first.target_world_m == second.target_world_m == (2200.0, 0.0, 0.0)
    assert second.reused and second.expanded_nodes == 0 and second.collision_checks == 1
    assert second.source_time_s == 11.0 and second.planned_time_s == 10.0
    third = adviser.choose((2200.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, 12.0, config)
    assert third.target_world_m == (2400.0, 0.0, 0.0)
    assert not third.reused and third.planned_time_s == 12.0
    assert not third.complete_route


def test_visibility_search_routes_around_a_parked_sphere_with_bounded_work():
    config = PlannerConfig()
    geometry = numeric_geometry([(0.0, 0.0, 0.0), (2100.0, 0.0, 0.0)], [1000.0, 38.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    advice, _ = finish_numeric_search(
        adviser, (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, config
    )
    assert advice.status == "local_advice"
    assert 3 <= len(advice.points_world_m) <= 10
    assert advice.points_world_m[-1] == (2200.0, 0.0, 0.0)
    assert advice.planned_time_s == 10.0
    collisions = nominal_index(geometry)
    assert not collisions.clear(advice.points_world_m[0], advice.points_world_m[-1])
    assert all(
        collisions.clear(left, right)
        for left, right in zip(advice.points_world_m[:-1], advice.points_world_m[1:], strict=True)
    )


def test_occupied_local_ray_endpoint_selects_a_checked_intermediate_frontier():
    config = PlannerConfig()
    start = (2000.0, 0.0, 0.0)
    geometry = numeric_geometry([(0.0, 0.0, 0.0), (2200.0, 0.0, 0.0)], [1000.0, 38.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    advice, _ = finish_numeric_search(adviser, start, (10000.0, 0.0, 0.0), geometry, config)
    assert advice.status == "local_advice"
    assert advice.local_goal_world_m != (2200.0, 0.0, 0.0)
    assert advice.local_goal_world_m == advice.points_world_m[-1]
    assert 0.0 < math.dist(start, advice.local_goal_world_m) <= 200.0 + 1e-12
    assert advice.local_goal_world_m[0] > start[0]
    collisions = nominal_index(geometry)
    assert all(
        collisions.clear(left, right)
        for left, right in zip(
            advice.points_world_m[:-1],
            advice.points_world_m[1:],
            strict=True,
        )
    )
    assert not advice.complete_route and not advice.safety_clearance


def test_visibility_search_keeps_its_frontier_between_four_node_slices():
    config = PlannerConfig()
    geometry = numeric_two_stage_barrier()
    advice, calls = finish_numeric_search(
        NominalRouteAdviser(work_budget_s=None),
        (2000.0, 0.0, 0.0),
        (10000.0, 0.0, 0.0),
        geometry,
        config,
    )
    assert advice.status == "local_advice"
    assert len(calls) > 1 and calls[0].status == "planning"
    assert calls[0].expanded_nodes == calls[0].search_expanded_nodes == 4
    assert advice.search_expanded_nodes > 4
    assert advice.planned_time_s == 10.0
    collisions = nominal_index(geometry)
    assert all(
        collisions.clear(left, right)
        for left, right in zip(advice.points_world_m[:-1], advice.points_world_m[1:], strict=True)
    )


def test_cached_corners_are_not_smoothed_away_when_the_obstacle_moves_away():
    config = PlannerConfig()
    geometry = numeric_geometry([(0.0, 0.0, 0.0), (2100.0, 0.0, 0.0)], [1000.0, 38.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    original, _ = finish_numeric_search(
        adviser, (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, config
    )
    assert original.status == "local_advice"
    moved = numeric_geometry([(0.0, 0.0, 0.0), (2100.0, 500.0, 0.0)], [1000.0, 38.0])
    current = adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), moved, 20.0, config)
    assert current.reused and current.target_world_m == original.target_world_m
    assert current.points_world_m == original.points_world_m
    reached = adviser.choose(original.target_world_m, (10000.0, 0.0, 0.0), moved, 21.0, config)
    assert reached.reused and reached.target_world_m == original.points_world_m[2]
    assert reached.planned_time_s == original.planned_time_s


def test_connector_overshoot_advances_only_with_small_cross_track_error():
    config = PlannerConfig()
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [1000.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, 10.0, config)
    off_lane = adviser.choose((2201.0, 30.0, 0.0), (10000.0, 0.0, 0.0), geometry, 11.0, config)
    assert off_lane.reused and off_lane.target_world_m == (2200.0, 0.0, 0.0)
    passed = adviser.choose((2201.0, 0.5, 0.0), (10000.0, 0.0, 0.0), geometry, 12.0, config)
    assert not passed.reused
    assert passed.local_goal_world_m[0] > 2400.0


def test_a_last_supplied_row_blocks_reuse_even_at_the_same_input_timestamp():
    config = PlannerConfig()
    centers = np.zeros((4099, 3))
    centers[:, 1] = 10000.0 + np.arange(len(centers))
    radii = np.ones(len(centers))
    initial = numeric_geometry(centers, radii)
    adviser = NominalRouteAdviser(work_budget_s=None)
    first = adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), initial, 10.0, config)
    assert first.status == "local_advice"
    centers[-1] = (2100.0, 0.0, 0.0)
    radii[-1] = 38.0
    blocked = numeric_geometry(centers, radii)
    second = adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), blocked, 10.0, config)
    assert second.supplied_obstacles == 4099
    assert not second.reused
    assert second.target_world_m != first.target_world_m
    if second.target_world_m is not None:
        assert nominal_index(blocked).clear((2000.0, 0.0, 0.0), second.target_world_m)


def test_long_forecast_velocity_changes_do_not_replace_a_clear_spatial_target():
    config = PlannerConfig()
    start = (-13047.0, 0.0, 0.0)
    target = (50000.0, 0.0, 0.0)
    macro = (-13068.0, 800.0, 0.0)
    adviser = NominalRouteAdviser(work_budget_s=None)
    original = None
    velocities = ((8.0, 0.0, 2.0), (8.0, -2.0, 2.0), (8.0, -2.0, 0.0))
    for index, velocity in enumerate(velocities):
        geometry = numeric_geometry(
            [(0.0, 0.0, 0.0), (-13250.0, 100.0, 0.0)],
            [13045.0, 38.0],
            [(0.0, 0.0, 0.0), velocity],
        )
        advice = adviser.choose(start, target, geometry, 13.0 + index, config, primary_goal=macro)
        assert advice.status == "local_advice"
        if original is None:
            original = advice.target_world_m
        else:
            assert advice.reused and advice.target_world_m == original
            assert advice.planned_time_s == 13.0


def test_short_moving_capsules_block_crossings_between_their_endpoints():
    config = PlannerConfig()
    geometry = numeric_geometry(
        [(0.0, 0.0, 0.0), (2100.0, -60.0, 0.0)],
        [1000.0, 10.0],
        [(0.0, 0.0, 0.0), (0.0, 80.0, 0.0)],
        growth=100.0,
    )
    names = ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2")
    before = {name: getattr(geometry, name).copy() for name in names}
    start, target = (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0)
    assert nominal_index(geometry, horizon=0.0).clear(start, (2200.0, 0.0, 0.0))
    assert not nominal_index(geometry).clear(start, (2200.0, 0.0, 0.0))
    advice, _ = finish_numeric_search(
        NominalRouteAdviser(work_budget_s=None), start, target, geometry, config
    )
    assert advice.status == "local_advice"
    assert all(
        nominal_index(geometry).clear(left, right)
        for left, right in zip(advice.points_world_m[:-1], advice.points_world_m[1:], strict=True)
    )
    for name in names:
        np.testing.assert_array_equal(getattr(geometry, name), before[name])
    assert not advice.safety_clearance, "Ignoring future growth is nominal advice, not clearance"


@pytest.mark.parametrize("budget", ["max_nodes", "max_expansions", "max_heap_entries"])
def test_config_can_tighten_each_search_budget_without_unchecked_fallback(budget):
    config = replace(PlannerConfig(), **{budget: 1})
    geometry = numeric_geometry([(2100.0, 0.0, 0.0)], [38.0])
    advice, _ = finish_numeric_search(
        NominalRouteAdviser(work_budget_s=None),
        (2000.0, 0.0, 0.0),
        (10000.0, 0.0, 0.0),
        geometry,
        config,
    )
    assert advice.status == "no_nominal_path"
    assert advice.target_world_m is None and not advice.points_world_m


def test_excess_nearby_geometry_fails_without_truncating_the_supplied_map():
    centers = np.tile((2100.0, 0.0, 0.0), (129, 1))
    geometry = numeric_geometry(centers, np.ones(len(centers)))
    adviser = NominalRouteAdviser(work_budget_s=None)
    advice = adviser.choose(
        (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), geometry, 10.0, PlannerConfig()
    )
    assert advice.status == "no_nominal_path" and advice.target_world_m is None
    assert advice.supplied_obstacles == 129
    assert "no supplied rows were discarded" in advice.detail
    np.testing.assert_array_equal(geometry.centers_m, centers)
    # A transient dense snapshot must not trap future calls in that old map.
    later = numeric_geometry([(0.0, 0.0, 0.0)], [1000.0])
    recovered = adviser.choose(
        (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), later, 11.0, PlannerConfig()
    )
    assert recovered.status == "local_advice"
    assert recovered.target_world_m == (2200.0, 0.0, 0.0)


def test_anonymous_row_order_does_not_choose_a_different_bypass():
    config = PlannerConfig()
    original = numeric_two_stage_barrier()
    reverse = numeric_geometry(original.centers_m[::-1], original.radii_m[::-1])
    first, _ = finish_numeric_search(
        NominalRouteAdviser(work_budget_s=None),
        (2000.0, 0.0, 0.0),
        (10000.0, 0.0, 0.0),
        original,
        config,
    )
    second, _ = finish_numeric_search(
        NominalRouteAdviser(work_budget_s=None),
        (2000.0, 0.0, 0.0),
        (10000.0, 0.0, 0.0),
        reverse,
        config,
    )
    assert first.status == second.status == "local_advice"
    assert first.points_world_m == second.points_world_m


def test_unfinished_frozen_search_is_validated_against_current_geometry():
    config = PlannerConfig()
    first = numeric_two_stage_barrier()
    adviser = NominalRouteAdviser(work_budget_s=None)
    initial = adviser.choose((2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), first, 10.0, config)
    assert initial.status == "planning"
    changed = numeric_geometry(
        np.vstack((first.centers_m, (2200.0, 0.0, 0.0))),
        np.append(first.radii_m, 20.0),
    )
    advice, _ = finish_numeric_search(
        adviser, (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0), changed, config
    )
    assert advice.status == "no_nominal_path" and advice.target_world_m is None
    assert "blocked by current geometry" in advice.detail


def test_goal_config_and_explicit_context_changes_discard_only_spatial_cache():
    config = PlannerConfig()
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [1000.0])
    adviser = NominalRouteAdviser(work_budget_s=None)
    start, target = (2000.0, 0.0, 0.0), (10000.0, 0.0, 0.0)
    adviser.choose(start, target, geometry, 10.0, config, context_key="first frame")
    changed = adviser.choose(
        (2005.0, 0.0, 0.0), target, geometry, 11.0, config, context_key="second frame"
    )
    assert not changed.reused and changed.target_world_m == (2205.0, 0.0, 0.0)
    new_goal = adviser.choose(
        start, target, geometry, 12.0, config, primary_goal=(2000.0, 1000.0, 0.0)
    )
    assert not new_goal.reused and new_goal.target_world_m == (2000.0, 200.0, 0.0)
    new_config = adviser.choose(
        (2000.0, 5.0, 0.0),
        target,
        geometry,
        13.0,
        replace(config, cell_size_m=20.0),
        primary_goal=(2000.0, 1000.0, 0.0),
    )
    assert not new_config.reused and new_config.target_world_m == (2000.0, 205.0, 0.0)
    adviser.reset()
    regressed = adviser.choose(start, target, geometry, 1.0, config)
    assert regressed.status == "no_nominal_path" and regressed.source_time_s == 1.0
    assert "no clock reset" in regressed.detail


def test_macro_goal_arrival_is_not_reported_as_native_success():
    geometry = numeric_geometry([(0.0, 0.0, 0.0)], [1000.0])
    advice = NominalRouteAdviser(work_budget_s=None).choose(
        (2000.0, 0.0, 0.0),
        (10000.0, 0.0, 0.0),
        geometry,
        10.0,
        PlannerConfig(),
        primary_goal=(2000.0, 0.0, 0.0),
    )
    assert advice.status == "at_local_goal" and advice.target_world_m is None
    assert "no native outcome inferred" in advice.detail
    assert not advice.complete_route


@pytest.mark.parametrize(
    "kwargs",
    [{"lookahead_m": 201.0}, {"sweep_horizon_s": 4.0}, {"work_budget_s": 0.0}],
)
def test_constructor_rejects_unbounded_or_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        NominalRouteAdviser(**kwargs)
