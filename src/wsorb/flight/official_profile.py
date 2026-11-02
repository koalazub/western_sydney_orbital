"""Official competition-profile beeline with fresh-radar avoidance.

The published BasicController example commands a 10 m/s beeline toward the
target and aligns the body +X axis with that direction. This module keeps that
speed reference, uses genuine observed radar rows for a lateral potential and
gates only the current command lease. It is deliberately separate from the
``transit_stream`` path, whose complete active-stop and fresh continuous
clearance proof remains unchanged.

The gate here is a current-lease sweep against the same observed-radar
clearance allowances used to build the potential. It is not a stopping
certificate and does not claim that an unobserved body or a later manoeuvre is
clear. No debris truth, synthetic track, reduced population or inherited route
clearance is used.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import count, field, mapping, point, scalar, vector
from .config import ResilienceConfig
from .control import (
    MAX_ACCEL_MPS2,
    MAX_ANGULAR_ACCEL_RAD_S2,
    _bounded,
    reference_to_body,
)
from .estimation import StateEstimate
from .temporal import MotionSegment, TemporalObstacles, motion_clear

__all__ = [
    "OFFICIAL_BEELINE_SPEED_MPS",
    "OFFICIAL_FLIGHT_PRESET",
    "OfficialMotion",
    "official_attitude_command",
    "official_basic_command",
    "official_command",
    "official_profile",
]

OFFICIAL_FLIGHT_PRESET = "competition_reference"
OFFICIAL_BEELINE_SPEED_MPS = 10.0
_EPSILON = 1e-12


@dataclass(frozen=True, slots=True)
class OfficialMotion:
    """One official-profile decision; all vectors use the public reference frame."""

    acceleration_mps2: tuple[float, float, float]
    angular_acceleration_rad_s2: tuple[float, float, float]
    report: dict


def official_profile(observation: Mapping[str, Any]) -> bool:
    """Select the official policy by its new flight preset, never by debris truth."""
    return isinstance(observation, Mapping) and (
        observation.get("flight_preset") == OFFICIAL_FLIGHT_PRESET
    )


def official_basic_command(
    observation: Mapping[str, Any],
    *,
    lease_s: float = 0.75,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Port the published BasicController beeline without debris avoidance.

    The published example commands a 10 m/s goal velocity with unity gain, then
    rotates that acceleration into the shuttle frame. This function keeps that
    translation exactly and applies the observed acceleration cap. Attitude uses
    the same bounded shortest-arc law as the official profile, so a comparison
    isolates guidance and avoidance rather than a second attitude controller.
    No radar row is read. This is a baseline, not a safe flight policy.
    """
    observation = mapping(observation, "observation")
    ship = mapping(field(observation, "shuttle"), "shuttle")
    position = np.asarray(point(field(ship, "position"), "shuttle.position"))
    velocity = np.asarray(point(field(ship, "velocity"), "shuttle.velocity"))
    rotation = vector(field(ship, "rotation"), "shuttle.rotation", length=4)
    target = mapping(field(observation, "target"), "target")
    goal = np.asarray(point(field(target, "position"), "target.position"))
    maximum = min(
        MAX_ACCEL_MPS2,
        scalar(field(observation, "max_accel_mps2"), "max_accel_mps2"),
    )
    angular_limit = min(
        MAX_ANGULAR_ACCEL_RAD_S2,
        scalar(
            field(observation, "max_angular_accel_rad_s2"),
            "max_angular_accel_rad_s2",
        ),
    )
    delta = goal - position
    distance = math.hypot(*delta)
    if distance <= _EPSILON:
        return np.zeros(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
    goal_velocity = OFFICIAL_BEELINE_SPEED_MPS * delta / distance
    acceleration = _bounded(goal_velocity - velocity, maximum)
    body = reference_to_body(
        acceleration,
        rotation,
        field(observation, "rotation_convention"),
    )
    time_s = scalar(field(observation, "time_s"), "time_s")
    source_time_s = scalar(
        observation.get("navigation_sample_time_s", time_s),
        "navigation_sample_time_s",
    )
    angular_velocity = ship.get("angular_velocity_rad_s")
    estimate = StateEstimate(
        time_s=time_s,
        source_time_s=source_time_s,
        position_m=tuple(float(value) for value in position),
        velocity_mps=tuple(float(value) for value in velocity),
        rotation_xyzw=tuple(float(value) for value in rotation),
        position_uncertainty_m=0.25,
        velocity_uncertainty_mps=0.05,
        angular_velocity_rad_s=(
            None
            if angular_velocity is None
            else tuple(
                float(value)
                for value in vector(
                    angular_velocity,
                    "shuttle.angular_velocity_rad_s",
                    length=3,
                )
            )
        ),
    )
    angular = official_attitude_command(estimate, delta, lease_s, angular_limit)
    return np.asarray(_bounded(body, maximum)), np.asarray(angular)


def _normalize(value: npt.ArrayLike) -> np.ndarray | None:
    array = np.asarray(value, dtype=float)
    length = math.hypot(*array)
    return None if length <= _EPSILON else array / length


def _perpendicular(direction: np.ndarray, velocity: np.ndarray) -> np.ndarray:
    """A deterministic lateral axis, not a fixed mission corridor."""
    side = np.cross(direction, velocity)
    if math.hypot(*side) <= _EPSILON:
        side = np.cross(direction, np.asarray((0.0, 1.0, 0.0)))
    if math.hypot(*side) <= _EPSILON:
        side = np.cross(direction, np.asarray((0.0, 0.0, 1.0)))
    normalized = _normalize(side)
    return np.asarray((0.0, 1.0, 0.0)) if normalized is None else normalized


def _body_x_world(rotation_xyzw: npt.ArrayLike) -> np.ndarray:
    """Return R(q)[:, 0] for the local active body-to-reference convention."""
    quaternion = np.asarray(rotation_xyzw, dtype=float)
    scale = float(np.max(np.abs(quaternion)))
    if scale == 0.0 or not math.isfinite(scale):
        raise ValueError("Navigation rotation must be a finite, nonzero quaternion")
    quaternion /= scale
    quaternion /= math.hypot(*quaternion)
    x, y, z, w = quaternion
    return np.asarray(
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y + z * w),
            2.0 * (x * z - y * w),
        )
    )


def official_attitude_command(
    estimate: StateEstimate,
    goal_direction: npt.ArrayLike,
    lease_s: float,
    angular_limit_rad_s2: float,
) -> np.ndarray:
    """Command shortest-arc body +X alignment with bounded angular acceleration.

    The desired angular velocity and acceleration both use the current decision
    lease as their response time. The only authority is the observed angular
    acceleration cap. Missing direct angular velocity yields no fabricated state
    and no attitude command.
    """
    lease = scalar(lease_s, "official attitude lease", positive=True)
    limit = scalar(angular_limit_rad_s2, "official angular limit", positive=True)
    goal = _normalize(goal_direction)
    if goal is None:
        return np.zeros(3)
    if estimate.angular_velocity_rad_s is None:
        return np.zeros(3)
    body_x = _body_x_world(estimate.rotation_xyzw)
    cross = np.cross(body_x, goal)
    sine = float(math.hypot(*cross))
    cosine = float(np.clip(np.dot(body_x, goal), -1.0, 1.0))
    angle = math.atan2(sine, cosine)
    if angle <= _EPSILON:
        desired_rate = np.zeros(3)
    else:
        # A time-optimal rate envelope uses only the observed angular cap and
        # the remaining angle. It avoids building a rate that cannot be braked
        # before alignment, without a separate attitude gain or mission constant.
        magnitude = min(angle / lease, math.sqrt(2.0 * limit * angle))
        axis = (
            _perpendicular(body_x, np.asarray(estimate.angular_velocity_rad_s))
            if sine <= _EPSILON
            else cross / sine
        )
        desired_rate = axis * magnitude
    rate_error = desired_rate - np.asarray(estimate.angular_velocity_rad_s)
    return _bounded(rate_error / lease, limit)


def _radar_only(obstacles: TemporalObstacles) -> TemporalObstacles | None:
    """Keep every observed row; omit only the three known public bodies.

    The official path lifts away from Phobos and does not need the transit
    departure exception. Its current-lease gate is intentionally about observed
    radar occupancy, so it neither hides nor reinterprets the known-body rows.
    """
    if len(obstacles.radii_m) <= 3:
        return None
    return TemporalObstacles(
        obstacles.centers_m[3:],
        obstacles.velocities_mps[3:],
        obstacles.radii_m[3:],
        obstacles.growth_mps[3:],
        obstacles.growth_mps2[3:],
    )


def _lease_clear(
    acceleration: np.ndarray,
    position: np.ndarray,
    velocity: np.ndarray,
    gain: float,
    lease: float,
    radar: TemporalObstacles | None,
) -> bool:
    if radar is None:
        return True
    try:
        segment = MotionSegment(
            lease,
            tuple(float(value) for value in position),
            tuple(float(value) for value in velocity),
            tuple(float(value) for value in acceleration * gain),
        )
        return motion_clear((segment,), radar)
    except (ArithmeticError, ValueError):
        return False


def _candidate_accelerations(
    goal_direction: np.ndarray,
    desired_direction: np.ndarray,
    speed: float,
    velocity: np.ndarray,
    maximum: float,
    gain: float,
    lease: float,
) -> list[tuple[str, np.ndarray]]:
    """A small bounded set, never the 40 s kinodynamic beam.

    The preferred potential-field command is tried first. Lateral alternatives
    exist only so the current-lease gate can reject a blocked preferred burn
    without falling back to the transit stopping search.
    """
    candidates: list[tuple[str, np.ndarray]] = []
    seen: set[tuple[float, float, float]] = set()

    def add(label: str, acceleration: npt.ArrayLike) -> None:
        bounded = _bounded(acceleration, maximum)
        key = tuple(float(value) for value in np.round(bounded, 14))
        if key not in seen:
            seen.add(key)
            candidates.append((label, bounded))

    add("potential_field", desired_direction * speed - velocity)
    add("direct_beeline", goal_direction * speed - velocity)
    side = _perpendicular(goal_direction, velocity)
    second = np.cross(goal_direction, side)
    deflection = math.pi / 3.0
    for index, angle in enumerate(np.linspace(0.0, 2.0 * math.pi, 8, endpoint=False)):
        lateral = side * math.cos(angle) + second * math.sin(angle)
        direction = _normalize(
            goal_direction * math.cos(deflection) + lateral * math.sin(deflection)
        )
        if direction is not None:
            add(f"lateral_{index}", direction * speed - velocity)
    if math.hypot(*velocity) > 0.0:
        add("brake", -velocity / (gain * lease))
    add("coast", np.zeros(3))
    return candidates


def official_command(
    observation: Mapping[str, Any],
    estimate: StateEstimate,
    obstacles: TemporalObstacles,
    config: ResilienceConfig,
    lease: float,
    gains: Mapping[str, Any],
    *,
    degraded: bool,
) -> OfficialMotion:
    """Return one bounded official-profile decision from current public data.

    The speed reference is exactly the published example's 10 m/s. It is reduced
    near the arrival region with the existing local braking assumption so the
    beeline does not need a separate magic approach speed. Attitude and
    translation are independent; an unavailable angular sample suppresses only
    attitude.
    """
    observation = mapping(observation, "observation")
    if not isinstance(estimate, StateEstimate):
        raise ValueError("estimate must be a StateEstimate")
    if not isinstance(obstacles, TemporalObstacles):
        raise ValueError("obstacles must be TemporalObstacles")
    if not isinstance(config, ResilienceConfig):
        raise ValueError("config must be a ResilienceConfig")
    lease = scalar(lease, "lease", positive=True)
    mapping(gains, "gains")
    now = scalar(field(observation, "time_s"), "time_s")
    mission = mapping(field(observation, "mission"), "mission")
    target = mapping(field(observation, "target"), "target")
    geometry = mapping(field(observation, "geometry"), "geometry")
    tuning = mapping(field(observation, "ship_tuning"), "ship_tuning")
    target_position = np.asarray(point(field(target, "position"), "target.position"))
    position = np.asarray(estimate.position_m)
    velocity = np.asarray(estimate.velocity_mps)
    arrival_radius = scalar(field(geometry, "arrival_radius_m"), "arrival radius", positive=True)
    debris_count = count(field(geometry, "debris_count"), "debris_count")
    gain = scalar(field(tuning, "thrust_scale"), "thrust_scale", positive=True)
    maximum = min(
        0.5,
        config.max_accel_mps2,
        scalar(field(observation, "max_accel_mps2"), "max_accel_mps2"),
    )
    if degraded:
        maximum = min(maximum, config.degraded_max_accel_mps2)
    angular_limit = min(
        0.05,
        scalar(
            field(observation, "max_angular_accel_rad_s2"),
            "max_angular_accel_rad_s2",
            positive=True,
        ),
    )
    if maximum <= 0.0 or gain <= 0.0:
        return OfficialMotion(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            {
                "guidance_strategy": "official_competition_beeline",
                "reason": "Configured official-profile thrust authority is zero",
                "source_time_s": now,
                "official_stop_required": False,
            },
        )
    braking = min(config.local_braking_accel_mps2, 0.98 * maximum * min(1.0, gain))
    if braking <= 0.0:
        raise ValueError("No modeled official-profile braking authority")
    checked_rows = max(0, len(obstacles.radii_m) - 3)
    unmodelled = max(0, debris_count - checked_rows)
    angular = official_attitude_command(estimate, target_position - position, lease, angular_limit)
    if mission.get("status") != "running":
        return OfficialMotion(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            {
                "guidance_strategy": "official_competition_beeline",
                "reason": "Native mission is not running",
                "source_time_s": now,
                "official_stop_required": False,
            },
        )
    delta = target_position - position
    distance = float(math.hypot(*delta))
    if distance <= 0.0:
        return OfficialMotion(
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            {
                "guidance_strategy": "official_competition_beeline",
                "reason": "Inside the public waypoint region; native success is not inferred",
                "source_time_s": now,
                "official_stop_required": False,
            },
        )
    goal_direction = delta / distance
    remaining = max(0.0, distance - arrival_radius)
    speed = min(OFFICIAL_BEELINE_SPEED_MPS, math.sqrt(2.0 * braking * remaining))
    desired_direction = goal_direction
    avoidance_rows = 0
    avoidance_risk = 0.0
    if speed > 0.0 and checked_rows > 0:
        centers = np.asarray(obstacles.centers_m[3:])
        # Use the same observed-row allowance at the end of this lease as the
        # gate below, so steering does not aim at a smaller corridor than the
        # command must actually clear.
        radii = np.asarray(obstacles.radii_m[3:])
        radii = radii + obstacles.growth_mps[3:] * lease
        radii = radii + obstacles.growth_mps2[3:] * lease**2
        relative = centers - position
        forward = relative @ goal_direction
        lateral_vector = relative - forward[:, None] * goal_direction
        lateral = np.hypot.reduce(lateral_vector, axis=1)
        influence = speed * speed / max(maximum * min(1.0, gain), _EPSILON)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            weight = np.maximum(0.0, 1.0 - forward / influence)
            weight *= np.maximum(0.0, 1.0 - lateral / radii)
        mask = (forward > 0.0) & (forward < influence) & (lateral < radii)
        avoidance_rows = int(np.count_nonzero(mask))
        if avoidance_rows:
            avoidance_risk = float(np.max(weight[mask]))
            away = np.zeros_like(lateral_vector[mask])
            moving = lateral[mask] > _EPSILON
            away[moving] = -lateral_vector[mask][moving] / lateral[mask][moving, None]
            if np.any(~moving):
                away[~moving] = _perpendicular(goal_direction, velocity)
            combined = np.sum(away * weight[mask][:, None], axis=0)
            escape = _normalize(combined)
            if escape is None:
                escape = _perpendicular(goal_direction, velocity)
            preferred = goal_direction * (1.0 - min(1.0, avoidance_risk))
            preferred += escape * min(1.0, avoidance_risk)
            normalized = _normalize(preferred)
            if normalized is not None:
                desired_direction = normalized
    desired_velocity = desired_direction * speed
    radar = _radar_only(obstacles)
    candidates = _candidate_accelerations(
        goal_direction,
        desired_direction,
        speed,
        velocity,
        maximum,
        gain,
        lease,
    )
    selected_label = None
    selected = None
    clear_count = 0
    for label, acceleration in candidates:
        if _lease_clear(acceleration, position, velocity, gain, lease, radar):
            clear_count += 1
            if selected is None:
                selected_label, selected = label, acceleration
    report = {
        "guidance_strategy": "official_competition_beeline",
        "profile": OFFICIAL_FLIGHT_PRESET,
        "source_time_s": now,
        "decision_valid_until_s": now + lease,
        "lease_sim_s": lease,
        "official_profile": True,
        "official_stop_required": False,
        "official_stop_certificate": False,
        "command_gate": (
            "fresh_rolling_radar_publication_current_lease_sweep_without_stop_certificate"
        ),
        "radar_model": "genuine_rolling_observed_rows_per_row_age_and_nominal_radial_motion",
        "avoidance_model": "forward_lateral_potential_field_no_fabricated_tracks",
        "known_body_gate": "not_used_official_profile_outward_beeline",
        "avoidance_rows": avoidance_rows,
        "avoidance_risk": avoidance_risk,
        "checked_radar_rows": checked_rows,
        "unmodelled_obstacles_lower_bound": unmodelled,
        "official_beeline_speed_mps": OFFICIAL_BEELINE_SPEED_MPS,
        "speed_model": (
            "published_10_mps_capped_by_existing_local_braking_distance_to_arrival_region"
        ),
        "configured_speed_limit_mps": OFFICIAL_BEELINE_SPEED_MPS,
        "desired_speed_mps": speed,
        "desired_velocity_world_mps": point(desired_velocity, "official desired velocity"),
        "target_distance_m": distance,
        "remaining_to_arrival_region_m": remaining,
        "reference_accel_limit_mps2": maximum,
        "braking_model_accel_mps2": braking,
        "braking_model_kind": "assumed_active_braking_not_fault_coasting",
        "attitude_model": "shortest_arc_body_plus_x_to_target_with_bounded_angular_acceleration",
        "angular_accel_limit_rad_s2": angular_limit,
        "current_lease_clear": selected is not None,
        "admissible_local_candidates": clear_count,
        "local_candidates": len(candidates),
        "selected_candidate": selected_label,
        "next_waypoint_world_m": point(target_position, "official target waypoint"),
        "nominal_motion_used": False,
        "nominal_motion_status": None,
        "nominal_motion_pair_intervals": 0,
        "nominal_motion_beam_width": None,
        "nominal_motion_duration_wall_s": 0.0,
        "nominal_motion_safety_clearance": False,
        "nominal_motion_source_freshness": False,
        "reason": (
            "Current observed-radar lease sweep passed using per-row acquisition ages; "
            "no stopping certificate claimed"
        ),
    }
    if selected is None:
        report["reason"] = (
            "No official command is clear of the current observed-radar lease; "
            "coasting without a stopping or future-clearance claim"
        )
        return OfficialMotion(
            (0.0, 0.0, 0.0),
            tuple(float(value) for value in angular),
            report,
        )
    return OfficialMotion(
        tuple(float(value) for value in selected),
        tuple(float(value) for value in angular),
        report,
    )
