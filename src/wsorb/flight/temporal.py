"""Conservative clearance for declared quadratic paths and moving spheres.

Every obstacle is retained, and its centre and radius coefficients start at
this decision's t=0 in the same frame as the path. The caller supplies all
clearance and uncertainty allowances. These calculations add only numeric and
chord-curvature padding, not a dynamics or measurement model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, point, real_array, scalar

__all__ = ["MotionSegment", "TemporalObstacles", "motion_clear"]

_MAX_OBSTACLES = 4099
_MAX_SEGMENTS = 4
_MAX_INTERVALS = 4096
_INTERVAL_BATCH = 64
_MAX_DURATION_S = 60.0
_CONTINUITY_ATOL = 1e-8
# Cover the per-axis continuity tolerance in 3D, as well as small coordinates.
_ABS_PADDING_M = 4.0 * _CONTINUITY_ATOL
_REL_PADDING = 1024.0 * float(np.finfo(np.float64).eps)


def _array(value: Any, name: str) -> npt.NDArray[np.float64]:
    # Preserve mixed booleans before NumPy can coerce a list to a float dtype.
    # Numeric ndarrays have already made that conversion at the caller boundary.
    if not isinstance(value, np.ndarray):
        try:
            raw = np.asarray(value, dtype=object)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"{name} must contain finite real numbers") from error
        if any(isinstance(item, (bool, np.bool_)) for item in raw.flat):
            raise ValueError(f"{name} must contain real numbers, not booleans")
    return real_array(value, name)


@dataclass(frozen=True, slots=True)
class MotionSegment:
    """One constant-acceleration piece, with position and velocity at its start."""

    duration_s: float
    position_m: Vec3
    velocity_mps: Vec3
    acceleration_mps2: Vec3

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "duration_s", scalar(self.duration_s, "segment.duration_s", positive=True)
        )
        for name in ("position_m", "velocity_mps", "acceleration_mps2"):
            value = point(_array(getattr(self, name), f"segment.{name}"), f"segment.{name}")
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True, eq=False)
class TemporalObstacles:
    """Independent read-only arrays for 1..4099 linearly moving spheres.

    Radius at decision-relative time t is ``radii_m + growth_mps*t +
    growth_mps2*t**2``. The quadratic coefficient is not halved. Nonnegative
    growth makes the radius at an interval's end a bound for that whole interval.
    """

    centers_m: npt.NDArray[np.float64]
    velocities_mps: npt.NDArray[np.float64]
    radii_m: npt.NDArray[np.float64]
    growth_mps: npt.NDArray[np.float64]
    growth_mps2: npt.NDArray[np.float64]

    def __post_init__(self) -> None:
        centers = _array(self.centers_m, "obstacles.centers_m")
        if (
            centers.ndim != 2
            or centers.shape[1:] != (3,)
            or not 1 <= len(centers) <= _MAX_OBSTACLES
        ):
            raise ValueError(
                f"obstacles.centers_m must have shape (N, 3), 1 <= N <= {_MAX_OBSTACLES}"
            )
        arrays = {"centers_m": centers}
        for name in ("velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
            value = _array(getattr(self, name), f"obstacles.{name}")
            shape = centers.shape if name == "velocities_mps" else (len(centers),)
            if value.shape != shape:
                raise ValueError(f"obstacles.{name} must have shape {shape}")
            if name == "radii_m" and np.any(value <= 0.0):
                raise ValueError("obstacles.radii_m must be positive")
            if name in ("growth_mps", "growth_mps2") and np.any(value < 0.0):
                raise ValueError(f"obstacles.{name} must be nonnegative")
            arrays[name] = value
        for name, value in arrays.items():
            # Immutable backing also prevents re-enabling writes with setflags.
            readonly = np.frombuffer(value.tobytes(), dtype=np.float64).reshape(value.shape)
            object.__setattr__(self, name, readonly)


def _interval_times(duration: float, maximum: float) -> npt.NDArray[np.float64]:
    ratio = duration / maximum
    if not math.isfinite(ratio) or ratio > _MAX_INTERVALS:
        raise ValueError("Motion exceeds the interval budget")
    count = max(1, math.ceil(ratio))
    times = np.linspace(0.0, duration, count + 1)
    # Equal-step boundaries can differ by an ulp more than their nominal step.
    # Add a subdivision instead of accepting a longer interval or trimming time.
    if np.any(np.diff(times) > maximum):
        count += 1
        if count > _MAX_INTERVALS:
            raise ValueError("Motion exceeds the interval budget")
        times = np.linspace(0.0, duration, count + 1)
    if np.any(np.diff(times) > maximum):
        raise ValueError("Interval lengths are not representable within the bound")
    return times


def _chord_clear(
    start: npt.NDArray[np.float64],
    end: npt.NDArray[np.float64],
    radii: npt.NDArray[np.float64],
) -> npt.NDArray[np.bool_]:
    if not all(np.isfinite(value).all() for value in (start, end, radii)):
        raise FloatingPointError("Unrepresentable chord bound")
    direction = end - start
    length_squared = np.sum(direction * direction, axis=1)
    along = np.divide(
        -np.sum(start * direction, axis=1),
        length_squared,
        out=np.zeros_like(length_squared),
        where=length_squared > 0.0,
    )
    np.clip(along, 0.0, 1.0, out=along)
    closest = start + along[:, None] * direction
    distances = np.hypot.reduce(closest, axis=1)
    if not np.isfinite(distances).all():
        raise FloatingPointError("Unrepresentable chord distance")
    return distances > radii


def _active_obstacles(segments, obstacles, horizon, obstacle_magnitude, maximum_radii):
    """Exclude only rows with a positive bound over the entire continuous path.

    A quadratic lies in the convex hull of its three Bezier control points.
    Their maximum distance from the first position therefore encloses every
    ownship position, including turns. A sphere's complete linear sweep must
    stay beyond that enclosure plus its maximum growing radius and padding.
    """
    anchor = np.asarray(segments[0].position_m)
    controls = []
    previous_position = previous_velocity = None
    own_scale = 0.0
    for segment in segments:
        position = np.asarray(segment.position_m)
        velocity = np.asarray(segment.velocity_mps)
        acceleration = np.asarray(segment.acceleration_mps2)
        duration = segment.duration_s
        if not math.isfinite(math.hypot(*segment.acceleration_mps2)):
            raise FloatingPointError("Unrepresentable acceleration norm")
        if previous_position is not None and (
            not np.allclose(position, previous_position, rtol=0.0, atol=_CONTINUITY_ATOL)
            or not np.allclose(velocity, previous_velocity, rtol=0.0, atol=_CONTINUITY_ATOL)
        ):
            raise ValueError("Discontinuous motion")
        middle = position + velocity * (0.5 * duration)
        previous_position = position + velocity * duration + acceleration * (0.5 * duration**2)
        previous_velocity = velocity + acceleration * duration
        controls.extend((position, middle, previous_position))
        own_scale = max(
            own_scale,
            math.fsum(
                (
                    float(np.max(abs(position))),
                    float(np.max(abs(velocity))) * duration,
                    float(np.max(abs(acceleration))) * (0.5 * duration**2),
                )
            ),
        )
    reach = float(np.max(np.hypot.reduce(np.asarray(controls) - anchor, axis=1)))
    travel = obstacles.velocities_mps * horizon
    relative = obstacles.centers_m - anchor
    squared = np.sum(travel * travel, axis=1)
    along = np.divide(
        -np.sum(relative * travel, axis=1), squared, out=np.zeros_like(squared), where=squared > 0.0
    )
    np.clip(along, 0.0, 1.0, out=along)
    closest = np.hypot.reduce(relative + along[:, None] * travel, axis=1)
    padding = _ABS_PADDING_M + _REL_PADDING * (
        own_scale + obstacle_magnitude + maximum_radii + reach
    )
    if not math.isfinite(reach) or not np.isfinite(closest).all() or not np.isfinite(padding).all():
        raise FloatingPointError("Unrepresentable all-path obstacle bound")
    return closest <= maximum_radii + reach + padding


def _motion_clear(
    segments: tuple[MotionSegment, ...],
    obstacles: TemporalObstacles,
    maximum: float,
) -> bool:
    durations = [segment.duration_s for segment in segments]
    # Subtract within fsum so even 60 seconds plus a tiny positive piece fails.
    if (
        any(duration > _MAX_DURATION_S for duration in durations)
        or math.fsum([*durations, -_MAX_DURATION_S]) > 0.0
    ):
        return False
    times = [_interval_times(duration, maximum) for duration in durations]
    interval_work = sum(len(boundaries) - 1 for boundaries in times)
    if interval_work > _MAX_INTERVALS:
        return False
    horizon = math.fsum(durations)
    obstacle_magnitude = (
        np.max(abs(obstacles.centers_m), axis=1)
        + np.max(abs(obstacles.velocities_mps), axis=1) * horizon
    )
    maximum_radii = (
        obstacles.radii_m
        + obstacles.growth_mps * horizon
        + obstacles.growth_mps2 * (horizon * horizon)
    )
    active = _active_obstacles(segments, obstacles, horizon, obstacle_magnitude, maximum_radii)
    if not np.any(active):
        return True
    if not np.all(active):
        obstacles = TemporalObstacles(
            obstacles.centers_m[active],
            obstacles.velocities_mps[active],
            obstacles.radii_m[active],
            obstacles.growth_mps[active],
            obstacles.growth_mps2[active],
        )
        obstacle_magnitude = obstacle_magnitude[active]
        maximum_radii = maximum_radii[active]
    previous_position = previous_velocity = None
    for index, (segment, boundaries) in enumerate(zip(segments, times, strict=True)):
        position = np.asarray(segment.position_m)
        velocity = np.asarray(segment.velocity_mps)
        acceleration = np.asarray(segment.acceleration_mps2)
        if previous_position is not None and (
            not np.allclose(position, previous_position, rtol=0.0, atol=_CONTINUITY_ATOL)
            or not np.allclose(velocity, previous_velocity, rtol=0.0, atol=_CONTINUITY_ATOL)
        ):
            return False
        local = boundaries[:, None]
        positions = position + velocity * local + acceleration * (0.5 * local * local)
        previous_position = positions[-1]
        previous_velocity = velocity + acceleration * segment.duration_s
        acceleration_norm = math.hypot(*segment.acceleration_mps2)
        own_magnitude = math.fsum(
            [
                float(np.max(abs(position))),
                float(np.max(abs(velocity))) * segment.duration_s,
                float(np.max(abs(acceleration))) * (0.5 * segment.duration_s**2),
            ]
        )
        if not math.isfinite(own_magnitude) or not math.isfinite(acceleration_norm):
            return False
        # Arithmetic allowance uses absolute terms before cancellation, including
        # world coordinates, travel and growth. It is not sensor uncertainty.
        padding = _ABS_PADDING_M + _REL_PADDING * (
            own_magnitude + obstacle_magnitude + maximum_radii
        )
        elapsed = math.fsum(durations[:index])
        starts, ends = boundaries[:-1], boundaries[1:]
        while len(starts):
            unresolved = []
            for first in range(0, len(starts), _INTERVAL_BATCH):
                last = min(first + _INTERVAL_BATCH, len(starts))
                local_start, local_end = starts[first:last, None], ends[first:last, None]
                start_time, end_time = elapsed + local_start, elapsed + local_end
                start_positions = (
                    position + velocity * local_start + acceleration * (0.5 * local_start**2)
                )
                end_positions = (
                    position + velocity * local_end + acceleration * (0.5 * local_end**2)
                )
                relative_start = start_positions[:, None, :] - (
                    obstacles.centers_m + obstacles.velocities_mps * start_time[:, :, None]
                )
                relative_end = end_positions[:, None, :] - (
                    obstacles.centers_m + obstacles.velocities_mps * end_time[:, :, None]
                )
                start_radii = (
                    obstacles.radii_m
                    + obstacles.growth_mps * start_time
                    + obstacles.growth_mps2 * start_time**2
                    + padding
                )
                end_radii = (
                    obstacles.radii_m
                    + obstacles.growth_mps * end_time
                    + obstacles.growth_mps2 * end_time**2
                    + padding
                )
                if np.any(np.hypot.reduce(relative_start, axis=2) <= start_radii) or np.any(
                    np.hypot.reduce(relative_end, axis=2) <= end_radii
                ):
                    return False
                # End radius bounds monotone growth throughout each interval;
                # ||a|| dt²/8 bounds the complete quadratic's chord deviation.
                curvature = acceleration_norm * (local_end - local_start) ** 2 / 8.0
                clear = _chord_clear(
                    relative_start.reshape(-1, 3),
                    relative_end.reshape(-1, 3),
                    (end_radii + curvature).reshape(-1),
                ).reshape(last - first, len(obstacles.radii_m))
                unresolved.extend(first + np.flatnonzero(~np.all(clear, axis=1)))
            if not unresolved:
                break
            # A failed coarse enclosure is not necessarily a collision. Refine
            # only unresolved intervals, retaining every obstacle and allowance.
            # A clear parent already proves its full interval, not just samples.
            interval_work += 2 * len(unresolved)
            if interval_work > _MAX_INTERVALS:
                return False
            left, right = starts[unresolved], ends[unresolved]
            middle = left + 0.5 * (right - left)
            if np.any(middle <= left) or np.any(middle >= right):
                return False
            starts = np.column_stack((left, middle)).reshape(-1)
            ends = np.column_stack((middle, right)).reshape(-1)
    return True


def motion_clear(
    segments: tuple[MotionSegment, ...],
    obstacles: TemporalObstacles,
    *,
    max_interval_s: float = 0.25,
) -> bool:
    """Check the whole continuous piecewise-quadratic path, with tangency blocked.

    Limits are four pieces, 60 seconds and 4096 total bounded intervals. Position
    and velocity continuity use rtol=0 and per-axis atol=1e-8. Clearance includes
    4e-8 metres plus 1024 float64 epsilons times an absolute arithmetic scale.
    Conservative chord failures are subdivided within the same 4096-check
    budget. No interval is accepted without a continuous enclosure proof.
    Far rows use a continuous all-path reach proof with their maximum radius;
    every other row retains the interval checks and all supplied growth.
    Invalid inputs, discontinuities, exhausted budgets and overflow return False.
    """
    if (
        not isinstance(segments, tuple)
        or not 1 <= len(segments) <= _MAX_SEGMENTS
        or not all(isinstance(segment, MotionSegment) for segment in segments)
        or not isinstance(obstacles, TemporalObstacles)
    ):
        return False
    try:
        maximum = scalar(max_interval_s, "max_interval_s", positive=True)
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            return _motion_clear(segments, obstacles, maximum)
    except (ValueError, TypeError, OverflowError, FloatingPointError):
        return False
