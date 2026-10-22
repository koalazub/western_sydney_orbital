"""Coverage planning, assignment, route ordering and A* navigation."""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from wsorb.geometry import Bounds, Vec2
from wsorb.model import AntennaSpec, AssetSpec, Scenario, Strategy
from wsorb.terrain import Terrain


@dataclass(frozen=True, slots=True)
class SearchPlan:
    routes: dict[str, list[Vec2]]
    relay_asset: str | None = None
    relay_position: Vec2 | None = None
    relay_source: AntennaSpec | None = None


class PathPlanner:
    """A* over the terrain grid with a direct-line fast path."""

    def __init__(self, terrain: Terrain, max_slope_deg: float):
        self.terrain = terrain
        self.max_slope_deg = max_slope_deg
        self._ny, self._nx = terrain.shape
        self._walkable = self._build_walkable()
        self._cache: dict[tuple[int, int, int, int], tuple[Vec2, ...]] = {}

    def _build_walkable(self) -> np.ndarray:
        xs, ys = self._axes()
        mask = np.ones(self._walkable_shape, dtype=bool)
        mask &= self.terrain.slopes_deg <= self.max_slope_deg
        for obstacle in self.terrain.obstacles:
            distance = np.hypot(
                xs[np.newaxis, :] - obstacle.center.x,
                ys[:, np.newaxis] - obstacle.center.y,
            )
            mask &= distance > obstacle.radius_m
        return mask

    @property
    def _walkable_shape(self) -> tuple[int, int]:
        return (self._ny, self._nx)

    def _axes(self) -> tuple[np.ndarray, np.ndarray]:
        xs = np.linspace(self.terrain.bounds.min_x, self.terrain.bounds.max_x, self._nx)
        ys = np.linspace(self.terrain.bounds.min_y, self.terrain.bounds.max_y, self._ny)
        return xs, ys

    def _cell_center(self, ix: int, iy: int) -> Vec2:
        xs, ys = self._axes()
        return Vec2(float(xs[ix]), float(ys[iy]))

    def _nearest_cell(self, point: Vec2) -> tuple[int, int] | None:
        xs, ys = self._axes()
        ix = int(np.argmin(np.abs(xs - point.x)))
        iy = int(np.argmin(np.abs(ys - point.y)))
        if self._walkable[iy, ix]:
            return ix, iy
        # Search outward in Chebyshev rings until a walkable cell is found.
        for radius in range(1, 12):
            best: tuple[float, int, int] | None = None
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue
                    nx, ny = ix + dx, iy + dy
                    if not (0 <= nx < self._nx and 0 <= ny < self._ny):
                        continue
                    if not self._walkable[ny, nx]:
                        continue
                    center = self._cell_center(nx, ny)
                    distance = center.distance_to(point)
                    if best is None or distance < best[0]:
                        best = (distance, nx, ny)
            if best is not None:
                return best[1], best[2]
        return None

    def plan(self, start: Vec2, goal: Vec2) -> list[Vec2]:
        """Return a walkable polyline from ``start`` to ``goal`` (or nearest reachable)."""

        if not self.terrain.walkable(start, self.max_slope_deg):
            return []
        if self.terrain.path_clear(start, goal, self.max_slope_deg):
            return [goal]

        goal_cell = self._nearest_cell(goal)
        if goal_cell is None:
            return []
        start_cell = self._nearest_cell(start)
        if start_cell is None:
            return []

        cache_key = (*start_cell, *goal_cell)
        cached = self._cache.get(cache_key)
        if cached is not None:
            path = list(cached)
        else:
            path = self._astar(start_cell, goal_cell)
            self._cache[cache_key] = tuple(path)

        if not path:
            return []
        cell_goal = path[-1]
        route_points = [start, *path]
        if self.terrain.path_clear(cell_goal, goal, self.max_slope_deg):
            route_points.append(goal)
        full_path = self._simplify(route_points)
        if not all(
            self.terrain.path_clear(first, second, self.max_slope_deg)
            for first, second in zip(full_path, full_path[1:], strict=False)
        ):
            return []
        return full_path[1:]

    def _astar(self, start: tuple[int, int], goal: tuple[int, int]) -> list[Vec2]:
        sx, sy = start
        gx, gy = goal
        if not self._walkable[sy, sx] or not self._walkable[gy, gx]:
            return []
        if start == goal:
            return [self._cell_center(sx, sy)]

        resolution = self.terrain.resolution_m
        open_heap: list[tuple[float, int, int, float]] = []
        heapq.heappush(open_heap, (0.0, sx, sy, 0.0))
        came_from: dict[tuple[int, int], tuple[int, int]] = {}
        best_cost: dict[tuple[int, int], float] = {(sx, sy): 0.0}
        directions = [
            (1, 0, resolution),
            (-1, 0, resolution),
            (0, 1, resolution),
            (0, -1, resolution),
            (1, 1, resolution * math.sqrt(2)),
            (1, -1, resolution * math.sqrt(2)),
            (-1, 1, resolution * math.sqrt(2)),
            (-1, -1, resolution * math.sqrt(2)),
        ]

        while open_heap:
            _, cx, cy, cost = heapq.heappop(open_heap)
            if cost > best_cost.get((cx, cy), math.inf) + 1e-9:
                continue
            if (cx, cy) == (gx, gy):
                return self._reconstruct(came_from, (cx, cy))

            for dx, dy, step in directions:
                nx, ny = cx + dx, cy + dy
                if not (0 <= nx < self._nx and 0 <= ny < self._ny):
                    continue
                if not self._walkable[ny, nx]:
                    continue
                # Do not cut diagonal corners.
                if (
                    dx != 0
                    and dy != 0
                    and (not self._walkable[cy, nx] or not self._walkable[ny, cx])
                ):
                    continue
                if not self.terrain.path_clear(
                    self._cell_center(cx, cy), self._cell_center(nx, ny), self.max_slope_deg
                ):
                    continue
                slope = float(self.terrain.slopes_deg[ny, nx])
                slope_penalty = 1.0 + (slope / 30.0) ** 2
                new_cost = cost + step * slope_penalty
                if new_cost >= best_cost.get((nx, ny), math.inf):
                    continue
                best_cost[(nx, ny)] = new_cost
                came_from[(nx, ny)] = (cx, cy)
                heuristic = math.hypot(gx - nx, gy - ny) * resolution
                heapq.heappush(open_heap, (new_cost + heuristic, nx, ny, new_cost))

        return []

    def _reconstruct(
        self, came_from: dict[tuple[int, int], tuple[int, int]], current: tuple[int, int]
    ) -> list[Vec2]:
        cells = [current]
        while current in came_from:
            current = came_from[current]
            cells.append(current)
        cells.reverse()
        return [self._cell_center(ix, iy) for ix, iy in cells]

    def _simplify(self, path: Sequence[Vec2]) -> list[Vec2]:
        """Remove waypoints only when the replacement segment is traversable."""
        if len(path) <= 2:
            return list(path)
        result = [path[0]]
        anchor = 0
        while anchor < len(path) - 1:
            next_index = anchor + 1
            for candidate in range(len(path) - 1, anchor, -1):
                if self.terrain.path_clear(path[anchor], path[candidate], self.max_slope_deg):
                    next_index = candidate
                    break
            result.append(path[next_index])
            anchor = next_index
        return result


def coverage_points(bounds: Bounds, spacing_m: float) -> list[Vec2]:
    """Boustrophedon coverage points inset by half a track spacing."""

    if spacing_m <= 0:
        raise ValueError("spacing must be positive")
    nx = max(1, int(math.floor(bounds.width / spacing_m)))
    ny = max(1, int(math.floor(bounds.height / spacing_m)))
    dx = bounds.width / nx
    dy = bounds.height / ny
    points: list[Vec2] = []
    for row in range(ny):
        y = bounds.min_y + (row + 0.5) * dy
        columns = range(nx) if row % 2 == 0 else range(nx - 1, -1, -1)
        for column in columns:
            x = bounds.min_x + (column + 0.5) * dx
            points.append(Vec2(x, y))
    return points


def _work_weights(assets: Sequence[AssetSpec]) -> list[float]:
    raw = [asset.max_speed_mps * asset.sensor_radius_m for asset in assets]
    total = sum(raw)
    if total <= 0:
        return [1.0 / len(assets)] * len(assets)
    return [value / total for value in raw]


def _split_weighted(points: Sequence[Vec2], weights: Sequence[float]) -> list[list[Vec2]]:
    """Split an ordered list into contiguous, capacity-weighted chunks."""

    count = len(points)
    cumulative = 0.0
    boundaries: list[int] = [0]
    for weight in weights[:-1]:
        cumulative += weight
        boundaries.append(min(count, int(round(cumulative * count))))
    boundaries.append(count)
    return [list(points[boundaries[i] : boundaries[i + 1]]) for i in range(len(weights))]


def _boustrophedon_columns(points: Sequence[Vec2]) -> list[Vec2]:
    """Sweep vertical columns, reversing direction each column."""

    ordered = sorted(points, key=lambda point: (point.x, point.y))
    if not ordered:
        return []
    columns: list[list[Vec2]] = []
    current_x = ordered[0].x
    column: list[Vec2] = []
    for point in ordered:
        if abs(point.x - current_x) > 1e-6 and column:
            columns.append(column)
            column = []
            current_x = point.x
        column.append(point)
    if column:
        columns.append(column)
    result: list[Vec2] = []
    for index, column in enumerate(columns):
        result.extend(column if index % 2 == 0 else reversed(column))
    return result


def _order_route(points: Iterable[Vec2], start: Vec2, max_2opt_points: int = 90) -> list[Vec2]:
    remaining = list(points)
    if not remaining:
        return []
    ordered: list[Vec2] = []
    current = start
    while remaining:
        nearest_index = min(
            range(len(remaining)),
            key=lambda index: current.distance_to(remaining[index]),
        )
        current = remaining.pop(nearest_index)
        ordered.append(current)

    if len(ordered) > max_2opt_points:
        return ordered

    # A small 2-opt pass removes the worst crossings.
    improved = True
    passes = 0
    while improved and passes < 4:
        improved = False
        passes += 1
        for i in range(len(ordered) - 2):
            a = start if i == 0 else ordered[i - 1]
            b = ordered[i]
            for j in range(i + 1, len(ordered)):
                c = ordered[j]
                d = ordered[j + 1] if j + 1 < len(ordered) else None
                before = a.distance_to(b) + (c.distance_to(d) if d else 0.0)
                after = a.distance_to(c) + (b.distance_to(d) if d else 0.0)
                if after + 1e-9 < before:
                    ordered[i : j + 1] = reversed(ordered[i : j + 1])
                    improved = True
                    break
            if improved:
                break
    return ordered


def assign_routes(
    scenario: Scenario,
    strategy: Strategy,
    points: Sequence[Vec2],
) -> dict[str, list[Vec2]]:
    """Assign coverage points to assets according to the strategy pattern."""

    assets = scenario.assets
    weights = _work_weights(assets)
    base = scenario.charger
    routes: dict[str, list[Vec2]] = {}

    if strategy.pattern == "sector":
        ordered = sorted(points, key=lambda point: point.x)
        chunks = _split_weighted(ordered, weights)
        for asset, chunk in zip(assets, chunks, strict=True):
            routes[asset.name] = _boustrophedon_columns(chunk)
        return routes

    if strategy.pattern == "sweep":
        # Keep the global boustrophedon row order and hand each asset a
        # contiguous band. This is the "lawnmower" baseline.
        ordered = list(points)
        chunks = _split_weighted(ordered, weights)
        for asset, chunk in zip(assets, chunks, strict=True):
            routes[asset.name] = chunk
        return routes

    # Nearest-workload: greedily assign each point to the asset that can reach it
    # with the least travel, normalised by capacity and current load.
    current = {asset.name: base for asset in assets}
    assigned: dict[str, list[Vec2]] = {asset.name: [] for asset in assets}
    remaining = sorted(points, key=lambda point: point.distance_to(base))
    while remaining:
        point = remaining.pop(0)
        best_name = min(
            (asset.name for asset in assets),
            key=lambda name: (
                current[name].distance_to(point)
                / max(weights[assets.index(scenario.asset(name))], 1e-6)
                + len(assigned[name]) * 12.0
            ),
        )
        assigned[best_name].append(point)
        current[best_name] = point
    for asset in assets:
        routes[asset.name] = _order_route(assigned[asset.name], base)
    return routes


def select_relay_position(
    scenario: Scenario,
    terrain: Terrain,
    source_name: str | None,
    candidates: Sequence[Vec2],
) -> Vec2:
    """Pick the candidate relay site that covers the most search cells."""

    if not candidates:
        return scenario.bounds.center

    source = scenario.antenna(source_name) if source_name else None
    fixed = [
        antenna for antenna in scenario.antennas if antenna.name != (source.name if source else "")
    ]
    if not fixed:
        return candidates[0]

    sample_points = coverage_points(scenario.bounds, max(120.0, terrain.resolution_m * 6))
    best_point: Vec2 | None = None
    best_score = -1
    for candidate in candidates:
        relay = AntennaSpec(name="relay", position=candidate, height_m=18.0)
        antennas = [*fixed, relay]
        score = sum(
            1
            for point in sample_points
            if any(
                terrain.comms_visible(point, antenna.position, antenna.height_m)
                for antenna in antennas
            )
        )
        if score > best_score:
            best_score = score
            best_point = candidate
    return best_point or candidates[0]


def build_search_plan(
    scenario: Scenario,
    strategy: Strategy,
    terrain: Terrain,
) -> SearchPlan:
    """Build the initial coverage routes and optional relay task."""

    min_sensor = min(asset.sensor_radius_m for asset in scenario.assets)
    spacing = min_sensor * scenario.search_spacing_factor
    points = coverage_points(scenario.bounds, spacing)
    routes = assign_routes(scenario, strategy, points)

    relay_asset: str | None = None
    relay_position: Vec2 | None = None
    relay_source: AntennaSpec | None = None
    if strategy.deploy_relay and scenario.relay_source_antenna and scenario.relay_candidates:
        capable = scenario.relay_capable_asset()
        if capable is not None:
            relay_asset = capable.name
            relay_source = scenario.antenna(scenario.relay_source_antenna)
            relay_position = select_relay_position(
                scenario,
                terrain,
                scenario.relay_source_antenna,
                scenario.relay_candidates,
            )

    return SearchPlan(
        routes=routes,
        relay_asset=relay_asset,
        relay_position=relay_position,
        relay_source=relay_source,
    )
