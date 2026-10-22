"""Tests for the small 2D geometry primitives."""

from __future__ import annotations

import math

import pytest

from wsorb.geometry import Bounds, Vec2, clamp


def test_vec2_arithmetic() -> None:
    a = Vec2(3.0, 4.0)
    b = Vec2(-1.0, 2.0)

    assert a + b == Vec2(2.0, 6.0)
    assert a - b == Vec2(4.0, 2.0)
    assert a * 2.0 == Vec2(6.0, 8.0)
    assert 2.0 * a == Vec2(6.0, 8.0)
    assert a / 2.0 == Vec2(1.5, 2.0)
    assert -a == Vec2(-3.0, -4.0)
    assert a.dot(b) == 5.0
    assert a.length == 5.0
    assert a.distance_to(b) == pytest.approx(math.sqrt(20.0))


def test_vec2_normalized_and_zero_vector() -> None:
    assert Vec2(3.0, 4.0).normalized() == Vec2(0.6, 0.8)
    assert Vec2(0.0, 0.0).normalized() == Vec2(0.0, 0.0)


def test_vec2_division_by_zero() -> None:
    with pytest.raises(ZeroDivisionError):
        _ = Vec2(1.0, 2.0) / 0.0


def test_vec2_lerp_and_rotation() -> None:
    assert Vec2(0.0, 0.0).lerp(Vec2(10.0, 20.0), 0.5) == Vec2(5.0, 10.0)
    rotated = Vec2(1.0, 0.0).rotated(math.pi / 2.0)
    assert rotated.x == pytest.approx(0.0, abs=1e-12)
    assert rotated.y == pytest.approx(1.0)


def test_bounds_geometry() -> None:
    bounds = Bounds(0.0, 10.0, 0.0, 20.0)
    assert bounds.width == 10.0
    assert bounds.height == 20.0
    assert bounds.center == Vec2(5.0, 10.0)
    assert bounds.contains(Vec2(5.0, 10.0))
    assert not bounds.contains(Vec2(-1.0, 10.0))
    assert bounds.contains(Vec2(1.0, 1.0), margin=1.0)
    assert not bounds.contains(Vec2(0.5, 1.0), margin=1.0)


def test_bounds_clamp_and_expand() -> None:
    bounds = Bounds(0.0, 10.0, 0.0, 20.0)
    assert bounds.clamp(Vec2(-5.0, 25.0)) == Vec2(0.0, 20.0)
    assert bounds.clamp(Vec2(5.0, 10.0)) == Vec2(5.0, 10.0)
    assert bounds.expanded(2.0) == Bounds(-2.0, 12.0, -2.0, 22.0)


@pytest.mark.parametrize(
    ("min_x", "max_x", "min_y", "max_y"),
    [
        (0.0, 0.0, 0.0, 1.0),
        (0.0, 1.0, 1.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
    ],
)
def test_bounds_reject_degenerate_extents(
    min_x: float, max_x: float, min_y: float, max_y: float
) -> None:
    with pytest.raises(ValueError, match="positive width and height"):
        Bounds(min_x, max_x, min_y, max_y)


def test_clamp() -> None:
    assert clamp(-1.0, 0.0, 1.0) == 0.0
    assert clamp(0.5, 0.0, 1.0) == 0.5
    assert clamp(2.0, 0.0, 1.0) == 1.0
