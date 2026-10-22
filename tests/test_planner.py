from __future__ import annotations

import numpy as np

from wsorb.geometry import Bounds, Vec2
from wsorb.planner import PathPlanner
from wsorb.terrain import Obstacle, Terrain


def test_astar_and_simplification_keep_segments_clear() -> None:
    bounds = Bounds(0, 100, 0, 100)
    heights = np.zeros((21, 21))
    terrain = Terrain(
        bounds,
        5,
        heights,
        heights.copy(),
        (Obstacle(Vec2(50, 50), 15),),
    )
    planner = PathPlanner(terrain, 20)
    path = planner.plan(Vec2(10, 50), Vec2(90, 50))
    assert path
    points = [Vec2(10, 50), *path]
    assert all(terrain.path_clear(a, b, 20) for a, b in zip(points, points[1:], strict=False))


def test_plan_reaches_valid_goal_without_obstacles() -> None:
    bounds = Bounds(0, 100, 0, 100)
    heights = np.zeros((21, 21))
    terrain = Terrain(bounds, 5, heights, heights.copy())
    assert PathPlanner(terrain, 20).plan(Vec2(5, 5), Vec2(95, 95)) == [Vec2(95, 95)]


def test_off_grid_endpoints_do_not_create_obstacle_shortcuts() -> None:
    bounds = Bounds(0, 100, 0, 100)
    heights = np.zeros((21, 21))
    terrain = Terrain(
        bounds,
        5,
        heights,
        heights.copy(),
        (Obstacle(Vec2(50, 50), 12),),
    )
    start, goal = Vec2(12.3, 50.2), Vec2(88.4, 50.1)
    path = PathPlanner(terrain, 20).plan(start, goal)
    assert path
    points = [start, *path]
    assert all(
        terrain.path_clear(first, second, 20)
        for first, second in zip(points, points[1:], strict=False)
    )
    assert PathPlanner(terrain, 20).plan(Vec2(50, 50), goal) == []
