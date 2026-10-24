"""Conservative 3D segment/capsule clearance and a NumPy box broadphase.

Tangency is blocked. Capsules already include the caller's clearance and
uncertainty. This is geometry, not a contact or dynamics model.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

from ._validation import Vec3, difference, point, scalar

_EPSILON = float(np.finfo(np.float64).eps)


@dataclass(frozen=True, slots=True)
class Capsule:
    from_m: Vec3
    to_m: Vec3
    radius_m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_m", point(self.from_m, "capsule.from_m"))
        object.__setattr__(self, "to_m", point(self.to_m, "capsule.to_m"))
        object.__setattr__(
            self, "radius_m", scalar(self.radius_m, "capsule.radius_m", positive=True)
        )
        difference(self.to_m, self.from_m)


def _dot(left: Vec3, right: Vec3) -> float:
    return math.fma(left[0], right[0], math.fma(left[1], right[1], left[2] * right[2]))


def _cross(left: Vec3, right: Vec3) -> Vec3:
    def subtract_products(a: float, b: float, c: float, d: float) -> float:
        product = c * d
        return math.fma(a, b, -product) + math.fma(-c, d, product)

    return (
        subtract_products(left[1], right[2], left[2], right[1]),
        subtract_products(left[2], right[0], left[0], right[2]),
        subtract_products(left[0], right[1], left[1], right[0]),
    )


def _separated_projection(
    base: float, along_u: float, along_v: float, axis_length: float, radius: float
) -> bool:
    if not all(math.isfinite(value) for value in (base, along_u, along_v, axis_length, radius)):
        return False
    minimum = base + min(along_u, 0.0) - max(along_v, 0.0)
    maximum = base + max(along_u, 0.0) - min(along_v, 0.0)
    # Scaled projection arithmetic allowance, not a probabilistic confidence bound.
    return max(minimum, -maximum) > radius * axis_length + 256.0 * _EPSILON


def _separated_axis(axis: Vec3, u: Vec3, v: Vec3, w: Vec3, radius: float) -> bool:
    scale = max(map(abs, axis))
    if not math.isfinite(scale) or scale == 0.0:
        return False
    axis = tuple(component / scale for component in axis)
    return _separated_projection(
        _dot(w, axis), _dot(u, axis), _dot(v, axis), math.hypot(*axis), radius
    )


def _point_segment_residual(offset: Vec3, direction: Vec3) -> Vec3:
    scale = max(map(abs, direction))
    if scale == 0.0:
        return offset
    axis = tuple(component / scale for component in direction)
    along = min(1.0, max(0.0, _dot(offset, axis) / _dot(direction, axis)))
    return tuple(
        math.fma(-component, along, value)
        for component, value in zip(direction, offset, strict=True)
    )


def _separated_scaled(u: Vec3, v: Vec3, w: Vec3, radius: float) -> bool:
    for axis in range(3):
        if _separated_projection(w[axis], u[axis], v[axis], 1.0, radius):
            return True

    # A closest pair has an endpoint, or lies in both segment interiors.
    # Residuals only propose separating axes. They are never unchecked distances.
    residuals = (
        _point_segment_residual(w, v),
        _point_segment_residual(tuple(a + b for a, b in zip(w, u, strict=True)), v),
        _point_segment_residual(tuple(-a for a in w), u),
        _point_segment_residual(tuple(a - b for a, b in zip(v, w, strict=True)), u),
    )
    closest = min(residuals, key=lambda value: _dot(value, value))
    if _separated_axis(closest, u, v, w, radius):
        return True

    normal = _cross(u, v)
    normal_scale = max(map(abs, normal))
    if normal_scale == 0.0:
        return False
    normal_axis = tuple(component / normal_scale for component in normal)
    angular_roundoff = 128.0 * _EPSILON * math.hypot(*u) * math.hypot(*v)
    if normal_scale > angular_roundoff:
        denominator = _dot(normal, normal_axis)
        along_u = _dot(_cross(v, w), normal_axis) / denominator
        along_v = _dot(_cross(u, w), normal_axis) / denominator
        if 0.0 <= along_u <= 1.0 and 0.0 <= along_v <= 1.0:
            residual = tuple(
                math.fma(a, along_u, math.fma(-b, along_v, c))
                for a, b, c in zip(u, v, w, strict=True)
            )
            if _separated_axis(residual, u, v, w, radius):
                return True
    # A rounded normal is not exactly perpendicular. Project both segments.
    return _separated_axis(normal_axis, u, v, w, radius)


def _capsule_clear(start: Vec3, end: Vec3, obstacle: Capsule) -> bool:
    try:
        u = difference(end, start)
        v = difference(obstacle.to_m, obstacle.from_m)
        w = difference(start, obstacle.from_m)
    except ValueError:
        return False
    scale = max(*map(abs, u), *map(abs, v), *map(abs, w), obstacle.radius_m)
    return _separated_scaled(
        tuple(value / scale for value in u),
        tuple(value / scale for value in v),
        tuple(value / scale for value in w),
        obstacle.radius_m / scale,
    )


def segment_clear(from_m: Vec3, to_m: Vec3, obstacles: Iterable[Capsule]) -> bool:
    """Check the entire edge. Invalid or unrepresentable geometry fails closed."""
    try:
        start, end = point(from_m, "from_m"), point(to_m, "to_m")
        difference(end, start)
    except ValueError:
        return False
    return all(
        isinstance(obstacle, Capsule) and _capsule_clear(start, end, obstacle)
        for obstacle in obstacles
    )


class CollisionIndex:
    """Padded AABB filtering, with exact segment/capsule checks for overlaps.

    Every query still performs O(obstacles) NumPy work. It does not run a Python
    capsule loop over distant obstacles, and never indexes only their endpoints.
    """

    def __init__(self, obstacles: Iterable[Capsule]):
        self.obstacles = tuple(obstacles)
        if not all(isinstance(obstacle, Capsule) for obstacle in self.obstacles):
            raise ValueError("obstacles must contain Capsules")
        self._from = np.array([obstacle.from_m for obstacle in self.obstacles]).reshape(-1, 3)
        self._to = np.array([obstacle.to_m for obstacle in self.obstacles]).reshape(-1, 3)
        radii = np.array([obstacle.radius_m for obstacle in self.obstacles]).reshape(-1, 1)
        magnitude = np.maximum(
            np.maximum(abs(self._from), abs(self._to)).max(axis=1, keepdims=True), radii
        )
        with np.errstate(over="ignore", invalid="ignore"):
            padding = radii + magnitude * (1024.0 * _EPSILON)
            self._minimum = np.minimum(self._from, self._to) - padding
            self._maximum = np.maximum(self._from, self._to) + padding
        if not np.isfinite(self._minimum).all() or not np.isfinite(self._maximum).all():
            raise ValueError("Obstacle bounds are not representable")

    def clear(self, from_m: Vec3, to_m: Vec3) -> bool:
        try:
            start, end = point(from_m, "from_m"), point(to_m, "to_m")
            difference(end, start)
        except ValueError:
            return False
        magnitude = max(*map(abs, start), *map(abs, end))
        padding = magnitude * (1024.0 * _EPSILON)
        with np.errstate(over="ignore", invalid="ignore"):
            minimum = np.minimum(start, end) - padding
            maximum = np.maximum(start, end) + padding
            offsets = np.concatenate((start - self._from, end - self._to))
        if not all(np.isfinite(array).all() for array in (minimum, maximum, offsets)):
            return False
        overlaps = np.all(self._minimum <= maximum, axis=1) & np.all(
            minimum <= self._maximum, axis=1
        )
        return all(
            _capsule_clear(start, end, self.obstacles[index]) for index in np.flatnonzero(overlaps)
        )
