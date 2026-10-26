"""Single-reading radial forecasts, not tracking or multi-frame fusion.

All positions, velocities and radar row-0 vectors must use one Phobos-centred
reference frame. Measurement timestamps MUST already share ``time_s``'s origin.
The local clock has that property. The official radar-loop and mission clocks
need an explicit adapter; this module cannot discover or normalize their offset.

Range rate constrains one component of debris motion. Unknown sideways motion,
measurement noise and observer backdating inflate swept capsules. These chosen
allowances do not bound Gaussian tails, gravity, contacts or debris acceleration,
and the forecast horizon is not the route's travel time.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, count, distance, field, mapping, point, real_array, scalar, vector
from .config import PlannerConfig
from .geometry import Capsule, CollisionIndex, segment_clear

MAX_RADAR_ROWS = 4096


@dataclass(frozen=True, slots=True)
class PlanningFrame:
    """Frozen public-data forecast consumed by one explicit search.

    The first obstacle is the inflated enclosing Phobos sphere, not its terrain.
    Only a checked outward departure prefix can omit it. A prefix inside that
    sphere carries a terrain warning, not a claim of surface clearance.
    """

    config: PlannerConfig
    source_seed: int
    source_time_s: float
    reference_frame: str
    start_world_m: Vec3
    goal_world_m: Vec3
    goal_center_m: Vec3
    goal_radius_m: float
    at_waypoint: bool
    obstacles: tuple[Capsule, ...]
    departure_world_m: tuple[Vec3, ...]
    measured_obstacles: int
    unmodelled_obstacles_lower_bound: int
    forecast_horizon_s: float
    max_measurement_age_s: float
    oldest_measurement_time_s: float | None
    newest_measurement_time_s: float | None
    assumed_tangential_speed_allowance_mps: float
    has_surface_departure: bool = False
    # Python-derived provenance of the complete anonymous input array, not IDs.
    # It is deliberately absent from the external renderer/SDK schema.
    radar_fingerprint: bytes = b""


def radar_rows(value: Any) -> npt.NDArray[np.float64]:
    """Validate the actual UserAPI Nx3x3 layout without adding identity/velocity.

    Row 0 is a relative vector. Row 1 is range, range rate, diameter. Row 2 is
    measurement time, zero, zero. Range may describe the closest point and need
    not equal the length of row 0. Ordering is not a persistent identity.
    """
    rows = real_array(value, "radar")
    if rows.shape == (0,):
        rows = rows.reshape(0, 3, 3)
    if rows.ndim != 3 or rows.shape[1:] != (3, 3) or len(rows) > MAX_RADAR_ROWS:
        raise ValueError(f"radar must have shape (N, 3, 3), with N <= {MAX_RADAR_ROWS}")
    if np.any(rows[:, 1, 2] <= 0.0) or np.any(rows[:, 2, 0] < 0.0) or np.any(rows[:, 2, 1:] != 0.0):
        raise ValueError(
            "radar requires nonnegative time, positive diameter and zero spare columns"
        )
    with np.errstate(over="ignore", invalid="ignore"):
        lengths = np.hypot(np.hypot(rows[:, 0, 0], rows[:, 0, 1]), rows[:, 0, 2])
    if np.any(lengths <= 0.0) or not np.isfinite(lengths).all():
        raise ValueError("radar row-0 relative vectors must have finite, positive lengths")
    return rows


def checked_radar(observation: Mapping[str, Any], max_age_s: float) -> npt.NDArray[np.float64]:
    now = scalar(field(observation, "time_s"), "time_s")
    rows = radar_rows(field(observation, "radar"))
    if np.any(rows[:, 2, 0] > now):
        raise ValueError(
            "Future-dated radar; time_s and measurement times must share one clock origin"
        )
    if np.any(now - rows[:, 2, 0] > max_age_s):
        raise ValueError("Stale radar exceeds max_measurement_age_s; request fresh measurements")
    return rows


def _noise_assumptions(
    observation: Mapping[str, Any], config: PlannerConfig
) -> tuple[float, float, float]:
    names = ("range_noise_m", "range_rate_noise_mps", "diameter_noise_fraction")
    assumed = tuple(float(getattr(config, name)) for name in names)
    if "sensor_model" in observation:
        sensor = mapping(observation["sensor_model"], "sensor_model")
        assumed = tuple(
            max(value, scalar(field(sensor, name), f"sensor_model.{name}"))
            for name, value in zip(names, assumed, strict=True)
        )
    if config.sigma_multiplier * assumed[2] >= 1.0:
        raise ValueError("sigma_multiplier * effective diameter_noise_fraction must be < 1")
    return assumed


def _goal_approach(position: Vec3, center: Vec3, radius: float, index: CollisionIndex) -> Vec3:
    approach = np.array(position) - center
    length = math.hypot(*approach)
    approach = approach / length if length > 0.0 else np.array([1.0, 0.0, 0.0])
    side = np.cross(approach, [0.0, 1.0, 0.0])
    side_length = math.hypot(*side)
    side = side / side_length if side_length > 1e-12 else np.array([0.0, 0.0, 1.0])
    up = np.cross(approach, side)
    up /= math.hypot(*up)
    candidates = (
        approach,
        approach + side,
        approach - side,
        approach + up,
        approach - up,
        side,
        -side,
        up,
        -up,
        -approach,
    )
    clear_points = []
    for direction in candidates:
        with np.errstate(over="ignore", invalid="ignore"):
            candidate = point(
                np.array(center) + direction / math.hypot(*direction) * radius, "goal approach"
            )
        if index.clear(candidate, candidate):
            clear_points.append(candidate)
    if not clear_points:
        raise ValueError("No sampled waypoint approach is clear of inflated obstacles")
    return min(clear_points, key=lambda candidate: distance(position, candidate))


def predict_obstacles(observation: Mapping[str, Any], config: PlannerConfig) -> PlanningFrame:
    """Build uncertainty-inflated capsules from public data, without commands.

    Empty, stale, future or malformed radar raises ValueError, even at a waypoint.
    Geometry comes from the observation. No mesh, scenario file or private
    debris fields are read. Config noise assumptions may exceed public sensor
    noise, but cannot silently reduce it.
    """
    observation = mapping(observation, "observation")
    if not isinstance(config, PlannerConfig):
        raise ValueError("config must be a PlannerConfig")
    now = scalar(field(observation, "time_s"), "time_s")
    seed = count(field(observation, "seed"), "seed", maximum=0xFFFFFFFF)
    reference = field(observation, "frame")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("frame must name the shared Phobos-centred reference frame")
    ship = mapping(field(observation, "shuttle"), "shuttle")
    target = mapping(field(observation, "target"), "target")
    geometry = mapping(field(observation, "geometry"), "geometry")
    environment = mapping(field(observation, "environment"), "environment")
    position = point(field(ship, "position"), "shuttle.position")
    velocity = vector(field(ship, "velocity"), "shuttle.velocity")
    center = point(field(target, "position"), "target.position")
    ship_radius = scalar(field(ship, "radius_m"), "shuttle.radius_m", positive=True)
    target_radius = scalar(field(target, "radius_m"), "target.radius_m", positive=True)
    body_radius = scalar(
        field(geometry, "phobos_enclosing_radius_m"), "phobos_enclosing_radius_m", positive=True
    )
    goal_radius = scalar(field(geometry, "arrival_radius_m"), "arrival_radius_m", positive=True)
    debris_count = count(field(geometry, "debris_count"), "debris_count")
    mars = point(field(environment, "mars_position_m"), "mars_position_m")
    mars_radius = scalar(field(environment, "mars_radius_m"), "mars_radius_m", positive=True)
    clearance = ship_radius + config.clearance_m
    if not math.isfinite(clearance) or goal_radius <= target_radius + clearance + 0.01:
        raise ValueError("Waypoint region cannot fit target radius, shuttle radius and clearance")
    at_waypoint = distance(position, center) <= goal_radius
    obstacles = [
        Capsule((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), body_radius + clearance),
        Capsule(mars, mars, mars_radius + clearance),
        Capsule(center, center, target_radius + clearance),
    ]
    rows = checked_radar(observation, config.max_measurement_age_s)
    if len(rows) == 0:
        raise ValueError("Waiting for radar measurements; empty input is not assumed clear")
    range_noise, rate_noise, diameter_fraction = _noise_assumptions(observation, config)
    observer_accel = max(
        config.observer_accel_allowance_mps2,
        scalar(field(observation, "max_accel_mps2"), "max_accel_mps2"),
    )
    times = rows[:, 2, 0]
    ages = now - times
    relative = rows[:, 0, :]
    lengths = np.hypot(np.hypot(relative[:, 0], relative[:, 1]), relative[:, 2])
    directions = relative / lengths[:, None]
    # Remove observer radial motion only. The perpendicular velocity is unknown.
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        radial_velocity = directions * (rows[:, 1, 1] + directions @ velocity)[:, None]
        observer_then = np.array(position) - ages[:, None] * velocity
        measured_centers = observer_then + relative
        starts = measured_centers + ages[:, None] * radial_velocity
        ends = starts + config.forecast_horizon_s * radial_velocity
        uncertainty = (
            config.sigma_multiplier * range_noise
            + 0.5 * observer_accel * ages * ages
            + (
                config.unknown_tangential_speed_mps
                + config.sigma_multiplier * rate_noise
                + observer_accel * ages
            )
            * (ages + config.forecast_horizon_s)
        )
        # Invert diameter noise relative to unknown true size, not the reading.
        radii = (
            rows[:, 1, 2] * 0.5 / (1.0 - config.sigma_multiplier * diameter_fraction)
            + clearance
            + uncertainty
        )
    if not all(np.isfinite(array).all() for array in (starts, ends, radii)):
        raise ValueError("Radar forecast or uncertainty is not representable")
    obstacles.extend(
        Capsule(start, end, radius) for start, end, radius in zip(starts, ends, radii, strict=True)
    )
    index = CollisionIndex(obstacles)
    goal = center if at_waypoint else _goal_approach(position, center, goal_radius - 0.01, index)

    start = position
    departure = [position]
    has_departure = False
    radial_distance = math.hypot(*position)
    if not at_waypoint and radial_distance <= obstacles[0].radius_m:
        # Prefer the known surface radius supplied by the simulation. An
        # enclosing sphere alone cannot locate an irregular body's surface.
        landed = ship.get("status") == "landed"
        mission = mapping(observation.get("mission", {}), "mission")
        landed = landed or mission.get("phase") == "on_pad"
        surface = geometry.get("terrain_radius_at_shuttle_m")
        if surface is not None:
            surface = scalar(surface, "terrain_radius_at_shuttle_m", positive=True)
            inside_surface = radial_distance < surface
        else:
            # Without a known surface, keep the original shallow-start guard.
            inside_surface = radial_distance < body_radius and (
                not landed or body_radius - radial_distance > config.cell_size_m
            )
        if radial_distance <= ship_radius or inside_surface:
            raise ValueError(
                "Observer is too far inside the enclosing bound or lacks landed state; "
                "terrain is unknown"
            )
        direction = np.array(position) / radial_distance
        start = point(direction * (obstacles[0].radius_m + config.cell_size_m), "outward departure")
        if distance(start, (0.0, 0.0, 0.0)) <= radial_distance:
            raise ValueError("Outward departure is not representable")
        if not segment_clear(position, start, obstacles[1:]):
            raise ValueError("Outward departure is blocked by Mars, target or a radar envelope")
        departure.append(start)
        has_departure = True
    return PlanningFrame(
        config=config,
        source_seed=seed,
        source_time_s=now,
        reference_frame=reference,
        start_world_m=start,
        goal_world_m=goal,
        goal_center_m=center,
        goal_radius_m=goal_radius,
        at_waypoint=at_waypoint,
        obstacles=tuple(obstacles),
        departure_world_m=tuple(departure),
        measured_obstacles=len(rows),
        unmodelled_obstacles_lower_bound=max(0, debris_count - len(rows)),
        forecast_horizon_s=float(config.forecast_horizon_s),
        max_measurement_age_s=float(max(ages)),
        oldest_measurement_time_s=float(min(times)),
        newest_measurement_time_s=float(max(times)),
        assumed_tangential_speed_allowance_mps=float(config.unknown_tangential_speed_mps),
        has_surface_departure=has_departure,
        radar_fingerprint=rows.tobytes(),
    )
