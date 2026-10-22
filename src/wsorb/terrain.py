"""Procedural lunar terrain with slope, obstacle and line-of-sight queries.

This is deliberately a *surrogate* for STP's GMRAD/LOLA terrain stack. It keeps
the properties that matter for campaign-level strategy — relief, impassable
slopes, blocked radio lines — without pretending to be a flight-qualified DEM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from wsorb.geometry import Bounds, Vec2


@dataclass(frozen=True, slots=True)
class Obstacle:
    """A circular keep-out region (rock field, lander debris, crevasse)."""

    center: Vec2
    radius_m: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.radius_m) or self.radius_m < 0:
            raise ValueError("obstacle radius must be finite and non-negative")

    def contains(self, point: Vec2) -> bool:
        return self.center.distance_to(point) <= self.radius_m


@dataclass
class Terrain:
    """A sampled height field covering a world rectangle."""

    bounds: Bounds
    resolution_m: float
    heights: np.ndarray
    slopes_deg: np.ndarray
    obstacles: tuple[Obstacle, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.heights.shape != self.slopes_deg.shape:
            raise ValueError("height and slope grids must have the same shape")
        if self.heights.ndim != 2:
            raise ValueError("terrain grids must be two-dimensional")
        if self.resolution_m <= 0:
            raise ValueError("terrain resolution must be positive")

    @property
    def shape(self) -> tuple[int, int]:
        return self.heights.shape

    @property
    def cell_count(self) -> int:
        return int(self.heights.size)

    def _grid_axes(self) -> tuple[np.ndarray, np.ndarray]:
        ny, nx = self.heights.shape
        xs = np.linspace(self.bounds.min_x, self.bounds.max_x, nx)
        ys = np.linspace(self.bounds.min_y, self.bounds.max_y, ny)
        return xs, ys

    def _bilinear(self, grid: np.ndarray, point: Vec2) -> float:
        ny, nx = grid.shape
        xs, ys = self._grid_axes()
        fx = (point.x - self.bounds.min_x) / self.bounds.width * (nx - 1)
        fy = (point.y - self.bounds.min_y) / self.bounds.height * (ny - 1)
        fx = min(max(fx, 0.0), nx - 1.0)
        fy = min(max(fy, 0.0), ny - 1.0)
        x0 = int(math.floor(fx))
        y0 = int(math.floor(fy))
        tx = fx - x0
        ty = fy - y0
        x1 = min(x0 + 1, nx - 1)
        y1 = min(y0 + 1, ny - 1)
        return float(
            grid[y0, x0] * (1 - tx) * (1 - ty)
            + grid[y0, x1] * tx * (1 - ty)
            + grid[y1, x0] * (1 - tx) * ty
            + grid[y1, x1] * tx * ty
        )

    def height_at(self, point: Vec2) -> float:
        return self._bilinear(self.heights, point)

    def slope_at(self, point: Vec2) -> float:
        return self._bilinear(self.slopes_deg, point)

    def in_bounds(self, point: Vec2, margin: float = 0.0) -> bool:
        return self.bounds.contains(point, margin)

    def blocked_by_obstacle(self, point: Vec2, clearance_m: float = 0.0) -> bool:
        return any(
            obstacle.center.distance_to(point) <= obstacle.radius_m + clearance_m
            for obstacle in self.obstacles
        )

    def walkable(self, point: Vec2, max_slope_deg: float, clearance_m: float = 0.0) -> bool:
        return (
            self.in_bounds(point, margin=clearance_m)
            and self.slope_at(point) <= max_slope_deg
            and not self.blocked_by_obstacle(point, clearance_m)
        )

    def line_of_sight(
        self,
        start: Vec2,
        end: Vec2,
        start_height_m: float = 2.0,
        end_height_m: float = 2.0,
        samples: int = 48,
    ) -> bool:
        """True when the straight segment is not occluded by terrain."""

        distance = start.distance_to(end)
        if distance < 1e-9:
            return True
        start_alt = self.height_at(start) + start_height_m
        end_alt = self.height_at(end) + end_height_m
        for index in range(1, samples):
            t = index / samples
            point = start.lerp(end, t)
            ray_alt = start_alt + (end_alt - start_alt) * t
            if self.height_at(point) > ray_alt:
                return False
        return True

    def path_clear(
        self,
        start: Vec2,
        end: Vec2,
        max_slope_deg: float,
        clearance_m: float = 0.0,
    ) -> bool:
        if not self.in_bounds(start, clearance_m) or not self.in_bounds(end, clearance_m):
            return False
        # Check circular keep-outs analytically so narrow obstacles cannot fall
        # between terrain samples.
        segment = end - start
        length_squared = segment.dot(segment)
        for obstacle in self.obstacles:
            offset = obstacle.center - start
            t = (
                0.0
                if length_squared == 0
                else min(1.0, max(0.0, offset.dot(segment) / length_squared))
            )
            closest = start + segment * t
            if closest.distance_to(obstacle.center) <= obstacle.radius_m + clearance_m:
                return False
        distance = start.distance_to(end)
        steps = max(2, int(math.ceil(distance / (self.resolution_m * 0.5))) + 1)
        for index in range(steps + 1):
            point = start.lerp(end, index / steps)
            if not self.walkable(point, max_slope_deg, clearance_m):
                return False
        return True

    def comms_visible(self, point: Vec2, antenna: Vec2, antenna_height_m: float) -> bool:
        return self.line_of_sight(point, antenna, 2.0, antenna_height_m)


def build_terrain(
    bounds: Bounds,
    resolution_m: float,
    seed: int,
    obstacles: tuple[Obstacle, ...] = (),
) -> Terrain:
    """Generate a deterministic, cratered height field.

    Relief is a sum of long-wavelength ridges, mid-scale undulation and a small
    number of impact craters with raised rims. Values are in metres.
    """

    if resolution_m <= 0:
        raise ValueError("resolution must be positive")

    nx = max(8, int(round(bounds.width / resolution_m)) + 1)
    ny = max(8, int(round(bounds.height / resolution_m)) + 1)
    xs = np.linspace(bounds.min_x, bounds.max_x, nx)
    ys = np.linspace(bounds.min_y, bounds.max_y, ny)
    xx, yy = np.meshgrid(xs, ys)

    rng = np.random.default_rng(seed)
    heights = np.zeros((ny, nx), dtype=float)

    # Ridges and undulation: wavelengths are tuned to the ~1.5 km search box.
    for wavelength_m, amplitude_m in ((2400.0, 18.0), (1100.0, 10.0), (540.0, 5.0)):
        phase_x = rng.uniform(0.0, 2.0 * math.pi)
        phase_y = rng.uniform(0.0, 2.0 * math.pi)
        heights += (
            amplitude_m
            * np.sin(2.0 * math.pi * xx / wavelength_m + phase_x)
            * np.cos(2.0 * math.pi * yy / wavelength_m + phase_y)
        )

    # Craters: a depression with a subtle raised rim.
    crater_count = 7
    for _ in range(crater_count):
        cx = rng.uniform(bounds.min_x, bounds.max_x)
        cy = rng.uniform(bounds.min_y, bounds.max_y)
        radius = rng.uniform(90.0, 220.0)
        depth = rng.uniform(8.0, 22.0)
        distance = np.hypot(xx - cx, yy - cy) / radius
        bowl = -depth * np.exp(-(distance**2) * 1.8)
        rim = 0.35 * depth * np.exp(-((distance - 1.05) ** 2) * 18.0)
        heights += bowl + rim

    # A little fine-scale roughness keeps straight-line paths from being free.
    roughness = rng.normal(0.0, 0.8, size=heights.shape)
    heights += roughness

    # Slope from the sampled field (degrees). np.gradient uses cell spacing.
    dz_dy, dz_dx = np.gradient(heights, bounds.height / (ny - 1), bounds.width / (nx - 1))
    slopes = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))

    return Terrain(
        bounds=bounds,
        resolution_m=resolution_m,
        heights=heights,
        slopes_deg=slopes,
        obstacles=obstacles,
    )


def grid_cell_centers(terrain: Terrain, bounds: Bounds | None = None) -> list[Vec2]:
    """Return world coordinates of grid cell centres inside ``bounds``."""

    target = bounds or terrain.bounds
    ny, nx = terrain.shape
    xs = np.linspace(terrain.bounds.min_x, terrain.bounds.max_x, nx)
    ys = np.linspace(terrain.bounds.min_y, terrain.bounds.max_y, ny)
    points: list[Vec2] = []
    for y in ys:
        for x in xs:
            point = Vec2(float(x), float(y))
            if target.contains(point):
                points.append(point)
    return points
