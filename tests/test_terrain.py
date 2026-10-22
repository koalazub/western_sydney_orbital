"""Tests for procedural terrain and line-of-sight queries."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.geometry import Bounds, Vec2
from wsorb.terrain import Obstacle, Terrain, build_terrain, grid_cell_centers


def flat_terrain(
    obstacles: tuple[Obstacle, ...] = (),
    *,
    width: float = 100.0,
    height: float = 100.0,
) -> Terrain:
    bounds = Bounds(0.0, width, 0.0, height)
    return Terrain(
        bounds=bounds,
        resolution_m=10.0,
        heights=np.zeros((11, 11), dtype=float),
        slopes_deg=np.zeros((11, 11), dtype=float),
        obstacles=obstacles,
    )


def test_build_terrain_shape_and_determinism() -> None:
    bounds = Bounds(0.0, 100.0, 0.0, 50.0)
    first = build_terrain(bounds, 10.0, seed=123)
    second = build_terrain(bounds, 10.0, seed=123)

    assert first.shape == (8, 11)
    assert first.cell_count == 88
    assert np.array_equal(first.heights, second.heights)
    assert np.array_equal(first.slopes_deg, second.slopes_deg)
    assert np.isfinite(first.heights).all()
    assert (first.slopes_deg >= 0.0).all()


def test_build_terrain_rejects_bad_resolution() -> None:
    with pytest.raises(ValueError, match="resolution"):
        build_terrain(Bounds(0.0, 10.0, 0.0, 10.0), 0.0, seed=1)


def test_terrain_validation() -> None:
    with pytest.raises(ValueError, match="same shape"):
        Terrain(
            bounds=Bounds(0.0, 1.0, 0.0, 1.0),
            resolution_m=1.0,
            heights=np.zeros((2, 2)),
            slopes_deg=np.zeros((3, 2)),
        )
    with pytest.raises(ValueError, match="two-dimensional"):
        Terrain(
            bounds=Bounds(0.0, 1.0, 0.0, 1.0),
            resolution_m=1.0,
            heights=np.zeros(2),
            slopes_deg=np.zeros(2),
        )


def test_height_at_bilinear_and_bounds() -> None:
    terrain = flat_terrain()
    terrain.heights[0, 0] = 10.0
    terrain.heights[0, 10] = 20.0
    terrain.heights[10, 0] = 30.0
    terrain.heights[10, 10] = 40.0

    assert terrain.height_at(Vec2(0.0, 0.0)) == pytest.approx(10.0)
    assert terrain.height_at(Vec2(50.0, 50.0)) == pytest.approx(0.0)
    assert terrain.height_at(Vec2(100.0, 100.0)) == pytest.approx(40.0)
    assert terrain.in_bounds(Vec2(50.0, 50.0))
    assert not terrain.in_bounds(Vec2(-1.0, 50.0))
    assert not terrain.in_bounds(Vec2(0.5, 50.0), margin=1.0)
    assert terrain.in_bounds(Vec2(1.0, 50.0), margin=1.0)


def test_obstacles_and_walkability() -> None:
    obstacle = Obstacle(center=Vec2(50.0, 50.0), radius_m=10.0)
    terrain = flat_terrain((obstacle,))

    assert obstacle.contains(Vec2(50.0, 50.0))
    assert not obstacle.contains(Vec2(70.0, 50.0))
    assert terrain.blocked_by_obstacle(Vec2(55.0, 50.0))
    assert terrain.blocked_by_obstacle(Vec2(62.0, 50.0), clearance_m=3.0)
    assert not terrain.walkable(Vec2(50.0, 50.0), max_slope_deg=30.0)
    assert terrain.walkable(Vec2(80.0, 50.0), max_slope_deg=30.0)


def test_walkable_slope_limit() -> None:
    terrain = flat_terrain()
    terrain.slopes_deg[5, 5] = 20.0

    assert terrain.walkable(Vec2(50.0, 50.0), max_slope_deg=25.0)
    assert not terrain.walkable(Vec2(50.0, 50.0), max_slope_deg=10.0)


def test_path_clear() -> None:
    obstacle = Obstacle(center=Vec2(50.0, 50.0), radius_m=10.0)
    terrain = flat_terrain((obstacle,))

    assert terrain.path_clear(Vec2(10.0, 10.0), Vec2(20.0, 20.0), max_slope_deg=30.0)
    assert not terrain.path_clear(Vec2(10.0, 50.0), Vec2(90.0, 50.0), max_slope_deg=30.0)

    terrain.slopes_deg[1, 5] = 60.0
    assert not terrain.path_clear(Vec2(10.0, 10.0), Vec2(90.0, 10.0), max_slope_deg=30.0)


def test_line_of_sight_on_flat_ground() -> None:
    terrain = flat_terrain()
    assert terrain.line_of_sight(Vec2(0.0, 0.0), Vec2(100.0, 100.0))
    assert terrain.line_of_sight(Vec2(50.0, 50.0), Vec2(50.0, 50.0))
    assert terrain.comms_visible(Vec2(10.0, 10.0), Vec2(90.0, 90.0), 15.0)


def test_line_of_sight_is_blocked_by_a_ridge() -> None:
    bounds = Bounds(0.0, 100.0, 0.0, 20.0)
    heights = np.zeros((21, 101), dtype=float)
    heights[:, 50] = 50.0
    terrain = Terrain(
        bounds=bounds,
        resolution_m=1.0,
        heights=heights,
        slopes_deg=np.zeros_like(heights),
    )

    assert not terrain.line_of_sight(Vec2(10.0, 10.0), Vec2(90.0, 10.0))
    assert terrain.line_of_sight(Vec2(10.0, 10.0), Vec2(20.0, 10.0))


def test_grid_cell_centers_subset() -> None:
    terrain = build_terrain(Bounds(0.0, 100.0, 0.0, 100.0), 10.0, seed=4)
    all_points = grid_cell_centers(terrain)
    subset = grid_cell_centers(terrain, Bounds(20.0, 40.0, 20.0, 40.0))

    assert len(all_points) == 121
    assert len(subset) == 9
    assert all(Bounds(20.0, 40.0, 20.0, 40.0).contains(point) for point in subset)
