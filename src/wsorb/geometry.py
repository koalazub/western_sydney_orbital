"""Small, dependency-free 2D geometry primitives."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Vec2:
    """A point or vector in the local lunar surface frame (metres)."""

    x: float
    y: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.x) or not math.isfinite(self.y):
            raise ValueError("Vec2 coordinates must be finite")

    def __add__(self, other: Vec2) -> Vec2:
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vec2) -> Vec2:
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> Vec2:
        return Vec2(self.x * scalar, self.y * scalar)

    __rmul__ = __mul__

    def __truediv__(self, scalar: float) -> Vec2:
        if scalar == 0:
            raise ZeroDivisionError("cannot divide a Vec2 by zero")
        return Vec2(self.x / scalar, self.y / scalar)

    def __neg__(self) -> Vec2:
        return Vec2(-self.x, -self.y)

    def dot(self, other: Vec2) -> float:
        return self.x * other.x + self.y * other.y

    @property
    def length(self) -> float:
        return math.hypot(self.x, self.y)

    def distance_to(self, other: Vec2) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def normalized(self) -> Vec2:
        length = self.length
        if length == 0:
            return Vec2(0.0, 0.0)
        return Vec2(self.x / length, self.y / length)

    def lerp(self, other: Vec2, t: float) -> Vec2:
        return Vec2(self.x + (other.x - self.x) * t, self.y + (other.y - self.y) * t)

    def rotated(self, angle_rad: float) -> Vec2:
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        return Vec2(self.x * cos_a - self.y * sin_a, self.x * sin_a + self.y * cos_a)

    def as_tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass(frozen=True, slots=True)
class Bounds:
    """An axis-aligned rectangle."""

    min_x: float
    max_x: float
    min_y: float
    max_y: float

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value) for value in (self.min_x, self.max_x, self.min_y, self.max_y)
        ):
            raise ValueError("bounds coordinates must be finite")
        if self.max_x <= self.min_x or self.max_y <= self.min_y:
            raise ValueError("bounds must have positive width and height")

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Vec2:
        return Vec2((self.min_x + self.max_x) / 2.0, (self.min_y + self.max_y) / 2.0)

    def contains(self, point: Vec2, margin: float = 0.0) -> bool:
        return (
            self.min_x + margin <= point.x <= self.max_x - margin
            and self.min_y + margin <= point.y <= self.max_y - margin
        )

    def clamp(self, point: Vec2) -> Vec2:
        return Vec2(
            min(max(point.x, self.min_x), self.max_x),
            min(max(point.y, self.min_y), self.max_y),
        )

    def expanded(self, margin: float) -> Bounds:
        return Bounds(
            self.min_x - margin,
            self.max_x + margin,
            self.min_y - margin,
            self.max_y + margin,
        )


def clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)
