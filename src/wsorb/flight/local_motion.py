"""Receding-horizon motion from current public radar, not a second physics engine.

The frozen A* preview unions obstacle positions over time. Here each commanded
motion is checked against occupancy at the same time, including a modeled active
stop. Radar noise, unknown sideways motion and ownship/history allowances remain.
Future braking requires eligible control; sensor loss still requests coasting.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass
from itertools import product

import numpy as np

from ._validation import distance, field, mapping, point, scalar, vector
from .config import ResilienceConfig
from .control import _bounded
from .estimation import StateEstimator
from .geometry import Capsule, CollisionIndex
from .nominal_motion import search_nominal_motion
from .nominal_route import NominalRouteAdviser, nominal_escape_waypoint
from .planning import RoutePlan
from .prediction import _goal_approach, _noise_assumptions, checked_radar
from .temporal import MotionSegment, TemporalObstacles, motion_clear

_DIRECTIONS = tuple(
    np.asarray(value, dtype=float) / math.hypot(*value)
    for value in product((-1, 0, 1), repeat=3)
    if value != (0, 0, 0)
)


@dataclass(frozen=True, slots=True)
class LocalDecision:
    acceleration_mps2: tuple[float, float, float]
    report: dict


def transit_motion(observation) -> bool:
    distribution = observation.get("debris_distribution")
    return isinstance(distribution, dict) and distribution.get("kind") == "transit_stream"


def temporal_obstacles(observation, estimator: StateEstimator, config: ResilienceConfig):
    """The original radial model and additive margins, evaluated at future time t."""
    now = scalar(field(observation, "time_s"), "time_s")
    ship = mapping(field(observation, "shuttle"), "shuttle")
    target = mapping(field(observation, "target"), "target")
    geometry = mapping(field(observation, "geometry"), "geometry")
    environment = mapping(field(observation, "environment"), "environment")
    raw_position = np.asarray(point(field(ship, "position"), "shuttle.position"))
    raw_velocity = vector(field(ship, "velocity"), "shuttle.velocity")
    estimate = estimator.estimate(now)
    clearance = scalar(field(ship, "radius_m"), "shuttle.radius_m", positive=True)
    clearance += config.planner.clearance_m
    known_centers = np.asarray(
        [
            (0.0, 0.0, 0.0),
            point(field(environment, "mars_position_m"), "mars_position_m"),
            point(field(target, "position"), "target.position"),
        ]
    )
    known_radii = (
        np.asarray(
            [
                scalar(
                    field(geometry, "phobos_enclosing_radius_m"), "phobos radius", positive=True
                ),
                scalar(field(environment, "mars_radius_m"), "mars radius", positive=True),
                scalar(field(target, "radius_m"), "target radius", positive=True),
            ]
        )
        + clearance
    )
    rows = checked_radar(observation, config.radar_age_s)
    if not len(rows):
        raise ValueError("No eligible radar occupancy")
    range_noise, rate_noise, diameter_fraction = _noise_assumptions(observation, config.planner)
    ages = now - rows[:, 2, 0]
    relative = rows[:, 0, :]
    lengths = np.linalg.norm(relative, axis=1)
    directions = relative / lengths[:, None]
    velocities = directions * (rows[:, 1, 1] + directions @ raw_velocity)[:, None]
    centers = raw_position - ages[:, None] * raw_velocity + relative
    centers += ages[:, None] * velocities
    observer_allowance = max(
        config.planner.observer_accel_allowance_mps2,
        scalar(field(observation, "max_accel_mps2"), "max_accel_mps2"),
    )
    growth = (
        config.planner.unknown_tangential_speed_mps
        + config.planner.sigma_multiplier * rate_noise
        + observer_allowance * ages
    )
    radii = (
        rows[:, 1, 2] * 0.5 / (1.0 - config.planner.sigma_multiplier * diameter_fraction)
        + clearance
        + config.planner.sigma_multiplier * range_noise
        + 0.5 * observer_allowance * ages**2
        + growth * ages
    )
    # Keep the same history disagreement term as ResilientGuidance._inflate,
    # with its time-dependent factor split into nonnegative coefficients.
    for timestamp in np.unique(rows[:, 2, 0]):
        historical = estimator.at(float(timestamp))
        if historical is None:
            continue
        mask = rows[:, 2, 0] == timestamp
        age = now - float(timestamp)
        assumed = raw_position - age * raw_velocity
        history_position = distance(historical.position_m, point(assumed, "backdated observer"))
        history_position += historical.position_uncertainty_m
        history_velocity = distance(historical.velocity_mps, point(raw_velocity, "raw velocity"))
        history_velocity += historical.velocity_uncertainty_mps
        radii[mask] += history_position + age * history_velocity
        growth[mask] += history_velocity
    own = distance(estimate.position_m, point(raw_position, "raw position"))
    own += estimate.position_uncertainty_m
    radii = np.concatenate((known_radii, radii)) + own
    growth = np.concatenate((np.zeros(3), growth)) + estimate.velocity_uncertainty_mps
    quadratic = np.full(len(radii), 0.5 * estimator.accel_allowance_mps2)
    return TemporalObstacles(
        np.concatenate((known_centers, centers)),
        np.concatenate((np.zeros((3, 3)), velocities)),
        radii,
        growth,
        quadratic,
    )


def _end(segment: MotionSegment):
    p = np.asarray(segment.position_m)
    v = np.asarray(segment.velocity_mps)
    a = np.asarray(segment.acceleration_mps2)
    t = segment.duration_s
    return p + v * t + 0.5 * a * t * t, v + a * t


def stopping_motion(position, velocity, acceleration, lease, braking, velocity_allowance):
    first = MotionSegment(lease, tuple(position), tuple(velocity), tuple(acceleration))
    stop_position, stop_velocity = _end(first)
    speed = math.hypot(*stop_velocity)
    segments = [first]
    if speed > 1e-12:
        duration = speed / braking
        segment = MotionSegment(
            duration, tuple(stop_position), tuple(stop_velocity), tuple(-stop_velocity / duration)
        )
        segments.append(segment)
        stop_position, stop_velocity = _end(segment)
    # Reserve the existing velocity allowance too, without changing the source
    # lease or pretending that the future braking command is already applied.
    if velocity_allowance > 0:
        segments.append(
            MotionSegment(
                velocity_allowance / braking,
                tuple(stop_position),
                tuple(stop_velocity),
                (0.0, 0.0, 0.0),
            )
        )
    return tuple(segments)


def _radial_supported(segments, axis, tolerance):
    """Retain the outward-only departure exception; no sideways launch shortcut."""
    # Reject a nonradial path before solving any longitudinal extrema. Most
    # turning candidates pass the short first piece's tolerance but fail their
    # complete brake; solving roots for that first piece cannot change rejection.
    for segment in segments:
        p = np.asarray(segment.position_m)
        v = np.asarray(segment.velocity_mps)
        a = np.asarray(segment.acceleration_mps2)
        t = segment.duration_s
        pp = p - axis * np.dot(p, axis)
        vp = v - axis * np.dot(v, axis)
        ap = a - axis * np.dot(a, axis)
        if math.hypot(*pp) + math.hypot(*vp) * t + 0.5 * math.hypot(*ap) * t * t > tolerance:
            return False
    for segment in segments:
        p = np.asarray(segment.position_m)
        v = np.asarray(segment.velocity_mps)
        a = np.asarray(segment.acceleration_mps2)
        t = segment.duration_s
        # p(t) dot v(t) is cubic. Its minimum on this interval is at an
        # endpoint or a real root of its quadratic derivative.
        quadratic = float(1.5 * np.dot(a, a))
        linear = float(3.0 * np.dot(v, a))
        constant = float(np.dot(p, a) + np.dot(v, v))
        roots = []
        if quadratic > 0.0:
            discriminant = linear**2 - 4.0 * quadratic * constant
            if discriminant >= 0.0:
                q = -0.5 * (linear + math.copysign(math.sqrt(discriminant), linear))
                roots = [q / quadratic]
                if q != 0.0:
                    roots.append(constant / q)
        elif linear != 0.0:
            roots = [-constant / linear]
        times = [
            0.0,
            t,
            *(root for root in roots if 0.0 < root < t),
        ]
        for elapsed in times:
            pos = p + v * elapsed + 0.5 * a * elapsed * elapsed
            vel = v + a * elapsed
            if np.dot(pos, vel) < -1e-8:
                return False
    return True


def _public_flow(observation):
    settings = observation.get("debris_flow")
    if not isinstance(settings, dict):
        experiment = observation.get("experiment_settings", {})
        settings = experiment.get("debris") if isinstance(experiment, dict) else None
    if not isinstance(settings, dict):
        return None
    axis, sign = settings.get("stream_axis"), settings.get("stream_sign")
    if axis not in ("x", "y", "z") or sign not in (-1, 1) or isinstance(sign, bool):
        return None
    speed = scalar(settings["speed_mps"], "speed_mps")
    spread = scalar(settings["speed_spread_mps"], "speed_spread_mps")
    velocity = np.zeros(3)
    velocity[("x", "y", "z").index(axis)] = sign * speed
    return velocity, (axis, sign, speed, spread)


def _anonymous_matches(
    centers,
    sizes,
    position_error,
    stamp,
    previous,
    inlet_velocity,
    spread,
    directions,
    radial,
    acceleration,
    config,
):
    """One bounded all-pair association; every constraint precedes uniqueness."""
    old_time, old_centers, old_sizes, old_error = previous[:4]
    duration = stamp - old_time
    if duration <= 0 or len(centers) * len(old_centers) > config.planner.max_nodes:
        return None, [], 0.0
    error = (position_error + old_error) / duration + 0.5 * acceleration * duration
    gate = (
        position_error
        + old_error
        + duration * (config.planner.unknown_tangential_speed_mps + spread)
    )
    gate += 0.5 * acceleration * duration**2
    delta = centers[:, None, :] - old_centers[None, :, :]
    # Inlet motion remains a valid proposal for new traffic. It cannot exclude
    # a row whose genuine measured radial motion changed after a contact.
    # Both proposals use the same original position/motion allowance; all
    # eligibility constraints still precede mutual uniqueness and corroboration.
    measured_radial = directions * radial[:, None]
    residual = np.minimum(
        np.linalg.norm(delta - inlet_velocity * duration, axis=2),
        np.linalg.norm(delta - measured_radial[:, None, :] * duration, axis=2),
    )
    sizes_overlap = (sizes[:, None, 0] <= old_sizes[None, :, 1]) & (
        old_sizes[None, :, 0] <= sizes[:, None, 1]
    )
    candidate_radial = np.einsum("ijk,ik->ij", delta, directions) / duration
    possible = (
        sizes_overlap & (residual <= gate) & (abs(candidate_radial - radial[:, None]) <= error)
    )
    unique = np.flatnonzero(np.sum(possible, axis=1) == 1)
    pairs = [(int(i), int(np.flatnonzero(possible[i])[0])) for i in unique]
    return delta, [(i, j) for i, j in pairs if np.sum(possible[:, j]) == 1], error


class RadarMotionHistory:
    """Anonymous, mutually unique displacement hypotheses, never object IDs.

    Two scans supply advice only. Updating a safety forecast requires a third
    distinct genuine scan, mutual position/size/range-rate associations, and
    agreement between independently timed displacement estimates. Ambiguity or
    disagreement retains the radial forecast. Every original radius/growth
    allowance is retained, and the history velocity error is added to it.
    Contacts, births, associations and Gaussian tails remain model limitations.
    """

    def __init__(self):
        self.frames = deque(maxlen=64)
        self.flow_key = None
        self.advisory_velocities_mps = None
        self.advisory_associations = None
        self.associated_rows = 0
        self.conditioned_obstacles = None
        self.corroborated_rows = 0
        self.trusted_motion_rows = None

    def refine(self, observation, estimator, config, obstacles):
        self.advisory_velocities_mps = None
        self.advisory_associations = None
        self.associated_rows = 0
        self.conditioned_obstacles = None
        self.corroborated_rows = 0
        self.trusted_motion_rows = None
        flow = _public_flow(observation)
        if flow is None:
            self.frames.clear()
            return obstacles, 0
        nominal, flow_key = flow
        self.advisory_velocities_mps = np.tile(nominal, (len(obstacles.radii_m) - 3, 1))
        self.advisory_associations = np.zeros(len(obstacles.radii_m) - 3, dtype=bool)
        if flow_key != self.flow_key:
            self.frames.clear()
            self.flow_key = flow_key
        rows = checked_radar(observation, config.radar_age_s)
        times = np.unique(rows[:, 2, 0])
        if len(times) != 1:
            return obstacles, 0
        stamp = float(times[0])
        pose = estimator.at(stamp)
        if pose is None:
            return obstacles, 0
        if self.frames and stamp < self.frames[-1][0]:
            self.frames.clear()
        while self.frames and stamp - self.frames[0][0] > config.history_window_s:
            self.frames.popleft()
        range_noise, _, diameter_noise = _noise_assumptions(observation, config.planner)
        position_error = config.planner.sigma_multiplier * range_noise * math.sqrt(3)
        position_error += pose.position_uncertainty_m
        centers = rows[:, 0, :] + np.asarray(pose.position_m)
        fraction = config.planner.sigma_multiplier * diameter_noise
        diameters = np.column_stack(
            (rows[:, 1, 2] / (1 + fraction), rows[:, 1, 2] / (1 - fraction))
        )
        eligible = [frame for frame in self.frames if 3.0 <= stamp - frame[0] <= 8.0]
        result, inferred = obstacles, 0
        resolved = np.zeros(len(rows), dtype=bool)
        velocity_error = 0.0
        if eligible:
            previous = min(eligible, key=lambda frame: abs(stamp - frame[0] - 5.0))
            old_time, old_centers, _, _ = previous[:4]
            duration = stamp - old_time
            # Exhaustion retains every raw obstacle and its original radial
            # forecast. It never drops rows to fit an association budget.
            if len(centers) * len(old_centers) <= config.planner.max_nodes:
                acceleration = max(
                    config.planner.observer_accel_allowance_mps2,
                    estimator.accel_allowance_mps2,
                )
                directions = rows[:, 0] / np.linalg.norm(rows[:, 0], axis=1)[:, None]
                current_radial = np.sum(obstacles.velocities_mps[3:] * directions, axis=1)
                # Apply every eligibility constraint before mutual uniqueness.
                # Otherwise an impossible range-rate association can make the
                # valid pair look ambiguous and replace measured sideways
                # motion with the inlet-flow hypothesis in heading advice.
                delta, pairs, error = _anonymous_matches(
                    centers,
                    diameters,
                    position_error,
                    stamp,
                    previous,
                    nominal,
                    flow_key[3],
                    directions,
                    current_radial,
                    acceleration,
                    config,
                )
                velocity_error = error
                if pairs:
                    velocities = np.array(obstacles.velocities_mps, copy=True)
                    shifted = np.array(obstacles.centers_m, copy=True)
                    radii = np.array(obstacles.radii_m, copy=True)
                    growth = np.array(obstacles.growth_mps, copy=True)
                    age = observation["time_s"] - stamp
                    for i, j in pairs:
                        velocity = delta[i, j] / duration
                        direction = rows[i, 0] / math.hypot(*rows[i, 0])
                        # Preserve the actual current range-rate component.
                        # Reject a history inconsistent with it rather than
                        # smoothing away a possible contact or changed motion.
                        radial = float(np.dot(obstacles.velocities_mps[i + 3], direction))
                        if abs(float(np.dot(velocity, direction)) - radial) > error:
                            continue
                        self.advisory_velocities_mps[i] = velocity
                        self.advisory_associations[i] = True
                        self.associated_rows += 1
                        tangent = velocity - direction * np.dot(velocity, direction)
                        if math.hypot(*tangent) <= error:
                            continue
                        resolved[i] = True
                        velocities[i + 3] += tangent
                        shifted[i + 3] += age * tangent
                        radii[i + 3] += age * error
                        growth[i + 3] += error
                        inferred += 1
                    result = TemporalObstacles(
                        shifted, velocities, radii, growth, obstacles.growth_mps2
                    )
                    # The immediately preceding scan can be separated by only
                    # 0.1 s, leaving displacement buried in the unchanged range
                    # allowance and losing otherwise unique associations. Use
                    # the eligible genuine source nearest the temporal midpoint.
                    # A frame with no displacement association cannot satisfy
                    # the corroboration gate for any row, even if well spaced.
                    # Retain every corroboration gate and error term below.
                    recent = min(
                        (
                            frame
                            for frame in self.frames
                            if old_time < frame[0] < stamp and np.any(frame[7])
                        ),
                        key=lambda frame: abs(frame[0] - 0.5 * (old_time + stamp)),
                        default=None,
                    )
                    if recent is not None:
                        _, recent_pairs, _ = _anonymous_matches(
                            centers,
                            diameters,
                            position_error,
                            stamp,
                            recent,
                            nominal,
                            flow_key[3],
                            directions,
                            current_radial,
                            acceleration,
                            config,
                        )
                        trusted = np.zeros(len(rows), dtype=bool)
                        retained_error = max(error, recent[6])
                        for i, j in recent_pairs:
                            if (
                                self.advisory_associations[i]
                                and recent[7][j]
                                and math.dist(
                                    self.advisory_velocities_mps[i],
                                    recent[4][j],
                                )
                                <= error + recent[6]
                            ):
                                trusted[i] = True
                        self.trusted_motion_rows = trusted
                        confirmed = trusted & resolved
                        if np.any(confirmed):
                            selection = np.concatenate((np.zeros(3, dtype=bool), confirmed))
                            extra_error = retained_error - error
                            self.conditioned_obstacles = TemporalObstacles(
                                np.where(selection[:, None], result.centers_m, obstacles.centers_m),
                                np.where(
                                    selection[:, None],
                                    result.velocities_mps,
                                    obstacles.velocities_mps,
                                ),
                                np.where(
                                    selection, result.radii_m + age * extra_error, obstacles.radii_m
                                ),
                                np.where(
                                    selection, result.growth_mps + extra_error, obstacles.growth_mps
                                ),
                                obstacles.growth_mps2,
                            )
                            self.corroborated_rows = int(np.count_nonzero(confirmed))
        if not self.frames or stamp > self.frames[-1][0]:
            self.frames.append(
                (
                    stamp,
                    centers.copy(),
                    diameters.copy(),
                    position_error,
                    self.advisory_velocities_mps.copy(),
                    resolved.copy(),
                    velocity_error,
                    self.advisory_associations.copy(),
                )
            )
        return result, inferred


def _traffic_encounters(
    observation,
    obstacles,
    position,
    velocity,
    lookahead,
    *,
    advisory_velocities=None,
    eligible_rows=None,
):
    """Advisory flow prediction only; it never supplies collision clearance.

    Public experiment settings describe the nominal inlet velocity, not each
    rock's measured motion after gravity or contact. Use it to begin turning
    before an encounter, while the separate all-row motion check still vetoes every
    command and its full stop with all original uncertainty allowances.
    """
    flow = _public_flow(observation)
    if flow is None:
        return None, 0.0, 0, None
    nominal_flow, _ = flow
    relative = obstacles.centers_m[3:] - position
    forecasts = (
        np.tile(nominal_flow, (len(relative), 1))
        if advisory_velocities is None
        else advisory_velocities
    )
    relative_velocity = forecasts - velocity
    squared = np.sum(relative_velocity**2, axis=1)
    encounter_times = np.divide(
        -np.sum(relative * relative_velocity, axis=1),
        squared,
        out=np.zeros_like(squared),
        where=squared > 1e-12,
    )
    times = np.clip(encounter_times, 1.0, lookahead)
    separation = relative + times[:, None] * relative_velocity
    # Steer perpendicular to the nominal stream rather than merely slowing
    # down in the path of incoming traffic.
    flow_length = math.hypot(*nominal_flow)
    if flow_length == 0:
        return None, 0.0, 0, None
    flow_axis = nominal_flow / flow_length
    lateral = separation - (separation @ flow_axis)[:, None] * flow_axis
    lengths = np.linalg.norm(lateral, axis=1)
    buffer = obstacles.radii_m[3:] + 4 * scalar(observation["shuttle"]["radius_m"], "radius")
    risk = np.maximum(0.0, buffer - lengths) / times
    risk[(encounter_times <= 0) | (encounter_times > lookahead)] = 0.0
    if eligible_rows is not None:
        risk[~eligible_rows] = 0.0
    index = int(np.argmax(risk))
    if risk[index] <= 0:
        return None, 0.0, 0, None
    away = -lateral[index]
    length = math.hypot(*away)
    if length < 1e-8:
        away = np.cross(flow_axis, (0.0, 1.0, 0.0))
        length = math.hypot(*away)
        if length < 1e-8:
            away = np.cross(flow_axis, (0.0, 0.0, 1.0))
            length = math.hypot(*away)
    away /= length
    return (
        away,
        float(risk[index]),
        int(np.count_nonzero(risk)),
        float(np.min(encounter_times[risk > 0])),
    )


def _traffic_steering(
    observation,
    obstacles,
    position,
    velocity,
    goal_velocity,
    lookahead,
    *,
    advisory_velocities=None,
):
    away, risk, count, _ = _traffic_encounters(
        observation,
        obstacles,
        position,
        velocity,
        lookahead,
        advisory_velocities=advisory_velocities,
    )
    if away is None:
        return goal_velocity, 0
    # Do not let forward cruise cancel the preferred escape velocity.
    return away * max(math.hypot(*goal_velocity), 2.0 * risk), count


def continuing_motion(
    position, velocity, first_accel, second_accel, lease, braking, velocity_error
):
    """Two reachable commanded pieces, then the same complete brake and reserve."""
    first = MotionSegment(lease, tuple(position), tuple(velocity), tuple(first_accel))
    next_position, next_velocity = _end(first)
    return (
        first,
        *stopping_motion(
            next_position,
            next_velocity,
            second_accel,
            lease,
            braking,
            velocity_error,
        ),
    )


def _motion_rejection(
    segments,
    obstacles,
    config,
    *,
    axis,
    radial_prefix,
    departure,
    departure_radius,
    tolerance,
    padding,
):
    """One acceptance path for immediate stops and optional continuations."""
    horizon = math.fsum(segment.duration_s for segment in segments)
    if horizon > config.max_stopping_horizon_s:
        return "stopping_horizon_budget", False, horizon
    supported = radial_prefix and _radial_supported(segments, axis, tolerance)
    if supported:
        stop_position, _ = _end(segments[-1])
        supported = math.hypot(*stop_position) <= departure_radius
    if departure and not supported:
        return "unsupported_radial_departure", False, horizon
    start = 1 if supported else 0
    inflated = TemporalObstacles(
        obstacles.centers_m[start:],
        obstacles.velocities_mps[start:],
        obstacles.radii_m[start:] + padding,
        obstacles.growth_mps[start:],
        obstacles.growth_mps2[start:],
    )
    stop_position, _ = _end(segments[-1])
    stop_centers = inflated.centers_m + inflated.velocities_mps * horizon
    stop_radii = inflated.radii_m + inflated.growth_mps * horizon
    stop_radii += inflated.growth_mps2 * horizon**2
    blocked = np.hypot.reduce(stop_position - stop_centers, axis=1) <= stop_radii
    if np.any(blocked):
        reason = "known_body" if np.any(blocked[: 3 - start]) else "radar_occupancy"
        return reason, bool(supported), horizon
    if motion_clear(segments, inflated):
        return None, bool(supported), horizon
    known = TemporalObstacles(
        inflated.centers_m[: 3 - start],
        inflated.velocities_mps[: 3 - start],
        inflated.radii_m[: 3 - start],
        inflated.growth_mps[: 3 - start],
        inflated.growth_mps2[: 3 - start],
    )
    return (
        "known_body" if not motion_clear(segments, known) else "radar_occupancy",
        bool(supported),
        horizon,
    )


def nominal_rollouts(position, velocity, directions, speed, maximum, gain, lease, horizon):
    """Calculated reachable burn/coast paths, never observations or authority."""
    reachable_speed = min(speed, math.hypot(*velocity) + maximum * gain * lease)
    delta_velocity = directions * reachable_speed - velocity
    lengths = np.hypot.reduce(delta_velocity, axis=1)
    accelerations = np.divide(
        delta_velocity * maximum * gain,
        lengths[:, None],
        out=np.zeros_like(delta_velocity),
        where=lengths[:, None] > 0,
    )
    times = np.linspace(0.0, horizon, 9)
    burns = np.minimum(horizon, lengths / (maximum * gain))
    elapsed_burn = np.minimum(times[None, :], burns[:, None])
    displacement = velocity * times[None, :, None]
    displacement = (
        displacement
        + accelerations[:, None, :]
        * (elapsed_burn * (times[None, :] - 0.5 * elapsed_burn))[:, :, None]
    )
    return times, position + displacement, accelerations, burns


def checkpoint_approach(position, target, radius, obstacles):
    """Nominal interior entry point, never the station body's occupied centre."""
    radius = scalar(radius, "checkpoint approach radius", positive=True)
    index = CollisionIndex(
        Capsule(center, center + velocity * 1.5, obstacle_radius)
        for center, velocity, obstacle_radius in zip(
            obstacles.centers_m,
            obstacles.velocities_mps,
            obstacles.radii_m,
            strict=True,
        )
    )
    return _goal_approach(
        point(position, "position"), point(target, "target"), radius, index
    ), index


def checkpoint_entry_speed(position, target, velocity, requested, limit, braking, body_radius):
    """Nominal measured-closing feedforward; no speed or stopping permission."""
    delta = np.asarray(target) - position
    length = math.hypot(*delta)
    if length == 0.0:
        return 0.0
    closing = max(0.0, float(np.dot(velocity, delta / length)))
    body_clearance = max(0.0, length - body_radius)
    return max(requested, min(limit, closing, math.sqrt(2 * braking * body_clearance)))


def checkpoint_entry_radius(arrival_radius, body_radius, inset, position_error):
    """An interior planning target; the native arrival region is unchanged."""
    outer = arrival_radius - max(inset, 2 * position_error + 0.01)
    if outer <= body_radius:
        raise ValueError("Checkpoint entry cannot fit the unchanged target clearance")
    return 0.5 * (outer + body_radius)


def checkpoint_stop_speed(
    entry_radius, body_radius, growth, quadratic, padding, lease, braking, error
):
    """Speed preference whose whole stop horizon fits the interior body margin."""
    gap = entry_radius - body_radius - padding
    if gap <= 0.0:
        return 0.0
    if quadratic > 0.0:
        # Rationalized positive root avoids cancellation when growth dominates.
        horizon = 2.0 * gap / (growth + math.sqrt(growth**2 + 4.0 * quadratic * gap))
    elif growth > 0.0:
        horizon = gap / growth
    else:
        return math.inf
    return max(0.0, braking * max(0.0, horizon - lease) - error)


def stop_endpoint_margins(
    position, velocity, commands, gain, lease, braking, error, obstacles, padding
):
    """Calculated endpoint margins only; continuous clearance remains separate."""
    accelerations = np.asarray(commands) * gain
    end_velocity = velocity + accelerations * lease
    speeds = np.hypot.reduce(end_velocity, axis=1)
    horizon = lease + (speeds + error) / braking
    stopped = position + velocity * lease + 0.5 * accelerations * lease**2
    stopped += end_velocity * (speeds / (2.0 * braking))[:, None]
    relative = stopped[:, None, :] - (
        obstacles.centers_m + horizon[:, None, None] * obstacles.velocities_mps
    )
    radii = obstacles.radii_m + obstacles.growth_mps * horizon[:, None]
    radii = radii + obstacles.growth_mps2 * horizon[:, None] ** 2
    return np.min(np.hypot.reduce(relative, axis=2) - radii - np.asarray(padding)[:, None], axis=1)


def blocked_radar_stop_endpoints(
    position, velocity, commands, gain, lease, braking, error, obstacles, padding, maximum_horizon
):
    """Batch only definite endpoint rejections; never grant motion clearance.

    Every radar row and its original radius/growth remain. Near-zero numerical
    margins and numeric failures defer to the unchanged complete per-motion gate.
    Batches contain at most 16,384 command/obstacle pairs.
    """
    if len(obstacles.radii_m) <= 3 or math.hypot(*velocity) > braking * maximum_horizon:
        return set()
    radar = TemporalObstacles(
        obstacles.centers_m[3:],
        obstacles.velocities_mps[3:],
        obstacles.radii_m[3:],
        obstacles.growth_mps[3:],
        obstacles.growth_mps2[3:],
    )
    rejected = set()
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            representatives = {}
            for command, allowance in zip(commands, padding, strict=True):
                representatives.setdefault(tuple(np.round(command, 14)), (command, allowance))
            selected = [
                (command, allowance)
                for command, allowance in representatives.values()
                if math.hypot(*(velocity + command * gain * lease)) > 1e-12
            ]
            if not selected:
                return set()
            commands = [item[0] for item in selected]
            padding = [item[1] for item in selected]
            scale = float(np.max(abs(position))) + float(np.max(abs(radar.centers_m)))
            scale += (
                math.hypot(*velocity) + float(np.max(abs(radar.velocities_mps)))
            ) * maximum_horizon
            scale += (
                float(np.max(radar.radii_m)) + float(np.max(radar.growth_mps)) * maximum_horizon
            )
            scale += (float(np.max(radar.growth_mps2)) + gain) * maximum_horizon**2
            rounding = 4e-8 + 1024 * float(np.finfo(float).eps) * scale
            size = max(1, 16_384 // len(radar.radii_m))
            for first in range(0, len(commands), size):
                last = min(len(commands), first + size)
                margins = stop_endpoint_margins(
                    position,
                    velocity,
                    np.asarray(commands[first:last]),
                    gain,
                    lease,
                    braking,
                    error,
                    radar,
                    np.asarray(padding[first:last]),
                )
                if not np.isfinite(margins).all() or not math.isfinite(rounding):
                    return set()
                rejected.update(
                    tuple(np.round(command, 14))
                    for command, margin in zip(commands[first:last], margins, strict=True)
                    if margin < -rounding
                )
    except (FloatingPointError, OverflowError, ValueError):
        return set()
    return rejected


class EscapeHeading:
    """Persistent nominal corridor advice, separate from stopping clearance.

    All rollouts start at measured position and velocity with bounded burns.
    Current radius allowances and nominal/associated velocities rank geometry;
    this advice deliberately does not certify future uncertainty or free space.
    The executable-motion checker still retains the complete time-growing raw
    envelopes. A noisy worst-row change must not undo an established maneuver.
    """

    def __init__(self):
        self.direction = None
        self.sample_time_s = None
        self.nominal_gap_m = None

    def choose(
        self,
        position,
        velocity,
        goal_velocity,
        obstacles,
        forecast_velocities,
        config,
        maximum,
        gain,
        _response,
        sample_time_s,
        *,
        lease=None,
    ):
        if self.direction is not None and sample_time_s == self.sample_time_s:
            return self.direction
        axis = position / math.hypot(*position)
        side = np.asarray((0.0, 1.0, 0.0)) - axis * axis[1]
        if math.hypot(*side) < 1e-8:
            side = np.asarray((0.0, 0.0, 1.0)) - axis * axis[2]
        side /= math.hypot(*side)
        second = np.cross(axis, side)
        choices = []
        if self.direction is not None:
            choices.append(self.direction)
        for angle in np.linspace(0.0, 2 * math.pi, 8, endpoint=False):
            tangent = side * math.cos(angle) + second * math.sin(angle)
            for outward in (0.25, 0.5, 1.0, 2.0):
                direction = tangent + outward * axis
                choices.append(direction / math.hypot(*direction))
        directions = np.asarray(choices)
        speed = math.hypot(*goal_velocity)
        lease = config.lease_sim_s if lease is None else lease
        # The old advice extrapolated an immediate 8 m/s escape even while the
        # real stopping gate allowed only about 1 m/s. That falsely made every
        # sideways heading appear to clear a later encounter. Model only the
        # speed reachable from the measured velocity during this actual lease.
        horizon = config.max_stopping_horizon_s
        # This longer nominal comparison grants no longer command or freshness.
        # Its continuous chord checks catch encounters between sample endpoints.
        times, positions, _, burns = nominal_rollouts(
            position,
            velocity,
            directions,
            speed,
            maximum,
            gain,
            lease,
            horizon,
        )
        centers = (
            obstacles.centers_m[None, :, :] + forecast_velocities[None, :, :] * times[:, None, None]
        )
        relative = positions[:, :, None, :] - centers[None, :, :, :]
        chord = np.diff(relative, axis=1)
        squared = np.sum(chord**2, axis=3)
        along = np.divide(
            -np.sum(relative[:, :-1] * chord, axis=3),
            squared,
            out=np.zeros_like(squared),
            where=squared > 0,
        )
        np.clip(along, 0.0, 1.0, out=along)
        closest = relative[:, :-1] + along[:, :, :, None] * chord
        gaps = np.linalg.norm(closest, axis=3) - obstacles.radii_m[None, None, :]
        curve = maximum * gain * np.diff(times) ** 2 / 8.0
        # Padding is needed only in intervals containing the bounded burn.
        curve = curve[None, :] * (times[:-1][None, :] < burns[:, None])
        gaps -= curve[:, :, None]
        radar_gap = np.min(gaps[:, :, 3:], axis=(1, 2))
        known_penalty = np.maximum(0.0, -np.min(gaps[:, :, :3], axis=(1, 2)))
        progress = (
            np.zeros(len(choices))
            if speed == 0.0
            else (positions[:, -1, :] - position) @ (goal_velocity / speed)
        )
        scores = radar_gap + 0.25 * progress - 1000.0 * known_penalty
        best = int(np.argmax(scores))
        # This threshold changes heading advice, never a collision allowance.
        # The original noise floor remains in every actual raw safety envelope.
        hysteresis = max(1.0, config.planner.sigma_multiplier * config.planner.range_noise_m)
        if self.direction is None or scores[best] > scores[0] + hysteresis:
            self.direction = directions[best].copy()
            self.nominal_gap_m = float(radar_gap[best])
        else:
            self.nominal_gap_m = float(radar_gap[0])
        self.sample_time_s = sample_time_s
        return self.direction


def initial_escape_side(position, anchor, preferred, radial_staging, obstacles, config):
    """Rank actual 3D departure rays, not projections of distant stream bodies.

    A start above the enclosing sphere is a calculated staging proposal only.
    The executable radial prefix still starts at the measured state and uses
    its original terrain, clearance, lease and complete stopping checks.
    """
    start = anchor * max(math.hypot(*position), float(obstacles.radii_m[0]) + 1.0)
    second = np.cross(anchor, preferred)
    angles = np.linspace(0.0, 2.0 * math.pi, 8, endpoint=False)
    sides = preferred * np.cos(angles)[:, None] + second * np.sin(angles)[:, None]
    goals = position + radial_staging * anchor + config.corridor_half_width_m * sides
    delta = goals - start
    relative = obstacles.centers_m - start
    along = (
        np.sum(relative[None, :, :] * delta[:, None, :], axis=2) / np.sum(delta**2, axis=1)[:, None]
    )
    np.clip(along, 0.0, 1.0, out=along)
    closest = relative[None, :, :] - along[:, :, None] * delta[:, None, :]
    gaps = np.hypot.reduce(closest, axis=2) - obstacles.radii_m
    radar_gap = np.min(gaps[:, 3:], axis=1)
    # Once every ray has ample local room, distant rocks must not choose a
    # different compass side. Geometry breaks meaningful near-field ties only.
    scores = np.minimum(radar_gap, min(200.0, config.corridor_half_width_m))
    scores[np.any(gaps[:, :3] <= 0.0, axis=1)] = -math.inf
    best = int(np.argmax(scores))
    return sides[best], float(radar_gap[best])


class LocalMotionPlanner:
    """Fresh checked motions can continue when a distant global route is blocked."""

    def __init__(self):
        self.anchor = None
        self.advisory = None
        self.advisory_surface_departure = False
        self.waypoint = 1
        self.departed = False
        self.departure_radius = None
        self.traffic_bypass = None
        self.bypass_waypoint = 0
        self.radar_history = RadarMotionHistory()
        self.previous_acceleration = np.zeros(3)
        self.nominal_route = NominalRouteAdviser()
        self.nominal_motion_plan = None
        self.nominal_motion_source_s = None
        self.arrival_approach = None
        self.escape_origin = None
        self.escape_side = None
        self.initial_exit_gap_m = None
        self.exit_staged = False

    @property
    def has_advisory(self):
        return self.advisory is not None

    def command(self, observation, estimator, config, lease, gains, *, degraded, route=None):
        now = scalar(field(observation, "time_s"), "time_s")
        estimate = estimator.estimate(now)
        position = np.asarray(estimate.position_m)
        velocity = np.asarray(estimate.velocity_mps)
        target = np.asarray(point(observation["target"]["position"], "target.position"))
        arrival_radius = scalar(
            observation["geometry"]["arrival_radius_m"], "arrival radius", positive=True
        )
        if observation.get("mission", {}).get("status") != "running":
            return LocalDecision((0.0, 0.0, 0.0), {"reason": "Native mission is not running"})
        if math.dist(position, target) == 0:
            return LocalDecision(
                (0.0, 0.0, 0.0),
                {
                    "reason": "Inside public waypoint region; native success is not inferred",
                },
            )
        gain = scalar(observation["ship_tuning"]["thrust_scale"], "thrust_scale", positive=True)
        maximum = min(
            0.5, config.max_accel_mps2, scalar(observation["max_accel_mps2"], "max_accel_mps2")
        )
        if degraded:
            maximum = min(maximum, config.degraded_max_accel_mps2)
        kp = scalar(gains.get("position_gain_s2", 0.0012), "position_gain_s2")
        kv = scalar(gains.get("velocity_gain_s", 0.07), "velocity_gain_s")
        if kp == 0 or kv == 0 or maximum == 0:
            return LocalDecision((0.0, 0.0, 0.0), {"reason": "Configured gains disable thrust"})
        # Local stopping uses a command the actuator can actually supply, with
        # authority left for model and discrete-step error. The
        # independent position/velocity uncertainty terms are still retained.
        braking = min(config.local_braking_accel_mps2, 0.98 * maximum * min(1.0, gain))
        if estimate.angular_velocity_rad_s is not None:
            # A body-frame burn rotates during its lease. Do not certify the
            # unspun braking model through a large attitude sweep. The separate
            # bounded angular controller can damp spin without linear thrust.
            sweep = math.hypot(*estimate.angular_velocity_rad_s) * (
                lease + estimate.time_s - estimate.source_time_s
            )
            if sweep > 0.15:
                return LocalDecision(
                    (0.0, 0.0, 0.0),
                    {
                        "reason": "Translation awaiting bounded angular-rate damping",
                        "attitude_sweep_rad": sweep,
                    },
                )
            braking *= math.cos(sweep)
        if braking <= 0:
            raise ValueError("No modeled braking authority")
        obstacles = temporal_obstacles(observation, estimator, config)
        _, inferred_rows = self.radar_history.refine(observation, estimator, config, obstacles)
        if self.radar_history.conditioned_obstacles is not None:
            obstacles = self.radar_history.conditioned_obstacles
        if self.anchor is None:
            radial = math.hypot(*position)
            if radial == 0:
                raise ValueError("No supported radial departure axis")
            self.anchor = position / radial
            self.departed = bool(radial > obstacles.radii_m[0])
            self.departure_radius = float(obstacles.radii_m[0] + config.planner.cell_size_m)
        radial = math.hypot(*position)
        if radial > self.departure_radius:
            self.departed = True
        departure = bool(not self.departed and radial <= obstacles.radii_m[0])
        surface = observation["geometry"].get("terrain_radius_at_shuttle_m")
        if departure and (
            surface is None
            or math.hypot(*position) < scalar(surface, "terrain radius", positive=True)
        ):
            raise ValueError("Surface departure lacks a current public terrain radius")
        radial_prefix = (
            not self.departed
            and surface is not None
            and radial >= scalar(surface, "terrain radius", positive=True)
        )
        if isinstance(route, RoutePlan) and route.status == "candidate":
            points = route.points_world_m
            if self.advisory is None or not np.array_equal(self.advisory[1:], points[1:]):
                self.advisory = np.array(points, copy=True)
                self.advisory_surface_departure = route.frame.has_surface_departure
                self.waypoint = 1
        unobserved = max(0, observation["geometry"]["debris_count"] - len(obstacles.radii_m) + 3)
        if self.traffic_bypass is None and unobserved:
            # A partial scan cannot establish that the direct lane is clear.
            # Choose a geometric detour at the configured corridor width, not
            # a fabricated map of the unobserved bodies. Every actual command
            # and stopping reserve is still checked against the current scan.
            staging = np.asarray(
                nominal_escape_waypoint(
                    position,
                    target,
                    config.planner,
                    radial_tolerance_m=max(
                        estimate.position_uncertainty_m,
                        min(4.0, 0.1 * config.planner.cell_size_m),
                    ),
                )
            )
            across = staging - position
            across -= self.anchor * np.dot(across, self.anchor)
            across /= math.hypot(*across)
            # Select the side using full current 3D geometry, then retain the
            # macro-goal while the local visibility graph supplies detours.
            staging_speed = config.degraded_max_speed_mps if degraded else config.max_speed_mps
            staging_horizon = lease + (staging_speed + estimate.velocity_uncertainty_mps) / braking
            staging_radius = obstacles.radii_m[0] + obstacles.growth_mps[0] * staging_horizon
            staging_radius += obstacles.growth_mps2[0] * staging_horizon**2
            radial_staging = max(config.planner.cell_size_m, staging_radius - radial)
            across, self.initial_exit_gap_m = initial_escape_side(
                position,
                self.anchor,
                across,
                radial_staging,
                obstacles,
                config.planner,
            )
            self.escape_origin = position.copy()
            self.escape_side = across.copy()
            offset = across * config.planner.corridor_half_width_m
            self.traffic_bypass = np.asarray(
                [
                    # Leave the traffic sideways first. A corridor-width
                    # radial leg spends the early encounter budget approaching
                    # oncoming traffic instead of leaving its stream line.
                    position + radial_staging * self.anchor + offset,
                    target + offset,
                    target,
                ]
            )
        goal = target
        intermediate = False
        if self.traffic_bypass is not None:
            if self.bypass_waypoint == 0:
                transverse = position - self.escape_origin
                transverse -= self.anchor * np.dot(transverse, self.anchor)
                transverse_distance = math.hypot(*transverse)
                if transverse_distance > config.planner.cell_size_m * 5:
                    side = transverse / transverse_distance
                    if np.dot(side, self.escape_side) < 0.5:
                        # A real avoidance maneuver can leave on another side.
                        # Do not steer back through the field just to touch an
                        # obsolete staging point on the original compass ray.
                        radial_offset = float(
                            np.dot(self.traffic_bypass[0] - self.escape_origin, self.anchor)
                        )
                        self.escape_side = side
                        offset = side * config.planner.corridor_half_width_m
                        self.traffic_bypass[0] = (
                            self.escape_origin + radial_offset * self.anchor + offset
                        )
                        self.traffic_bypass[1] = target + offset
                exit_distance = max(
                    0.9 * config.planner.corridor_half_width_m,
                    config.planner.corridor_half_width_m - 20.0,
                )
                if transverse_distance >= exit_distance:
                    self.escape_side = transverse / transverse_distance
                    self.traffic_bypass[1] = (
                        target + self.escape_side * config.planner.corridor_half_width_m
                    )
                    self.bypass_waypoint = 1
            while (
                self.bypass_waypoint < 2
                and math.dist(position, self.traffic_bypass[self.bypass_waypoint]) <= 20
            ):
                self.bypass_waypoint += 1
            goal = self.traffic_bypass[self.bypass_waypoint]
            if self.bypass_waypoint == 0 and not self.exit_staged:
                radial_offset = float(
                    np.dot(self.traffic_bypass[0] - self.escape_origin, self.anchor)
                )
                side_offset = min(200.0, config.planner.corridor_half_width_m)
                radial_progress = float(np.dot(position - self.escape_origin, self.anchor))
                self.exit_staged = radial_progress >= radial_offset - min(
                    20.0, 0.1 * radial_offset
                ) and transverse_distance >= side_offset - min(20.0, 0.1 * side_offset)
                if not self.exit_staged:
                    # Build radial stopping room while leaving the near field.
                    # An 800 m sideways goal otherwise spends that first turn
                    # nearly tangential to the growing enclosing-body envelope.
                    goal = (
                        self.escape_origin
                        + radial_offset * self.anchor
                        + side_offset * self.escape_side
                    )
            intermediate = self.bypass_waypoint < 2
        elif self.advisory is not None:
            # Once the enclosing bound is cleared, the old radial prefix need
            # not prevent a locally checked turn. Known-body clearance below
            # applies to every such turn and its modeled stopping trajectory.
            if (
                not departure
                and self.advisory_surface_departure
                and self.waypoint == 1
                and len(self.advisory) > 2
            ):
                self.waypoint = 2
            while (
                self.waypoint < len(self.advisory) - 1
                and math.dist(position, self.advisory[self.waypoint]) <= 20.0
            ):
                self.waypoint += 1
            goal = self.advisory[self.waypoint]
            intermediate = self.waypoint < len(self.advisory) - 1
        delta = goal - position
        length = math.hypot(*delta)
        if not departure and length <= estimate.position_uncertainty_m:
            direction = (
                (target - position) / math.dist(target, position)
                if math.dist(target, position) > 0
                else np.zeros(3)
            )
        else:
            direction = self.anchor if departure else delta / length
        remaining = max(
            0.0, math.dist(position, target) - (arrival_radius - config.arrival_inset_m)
        )
        interior_radius = None
        if self.traffic_bypass is not None and self.bypass_waypoint == 2:
            interior_radius = checkpoint_entry_radius(
                arrival_radius,
                obstacles.radii_m[2],
                config.arrival_inset_m,
                estimate.position_uncertainty_m,
            )
            remaining = max(0.0, math.dist(position, target) - interior_radius)
        speed_limit = config.degraded_max_speed_mps if degraded else config.max_speed_mps
        entry_speed_limit = None
        if interior_radius is not None:
            command_padding = maximum * gain * lease**2 / 32
            command_padding += (
                0.5 * (estimator.accel_allowance_mps2 + 2 * maximum * gain) * lease**2
            )
            entry_speed_limit = min(
                speed_limit,
                checkpoint_stop_speed(
                    interior_radius,
                    obstacles.radii_m[2],
                    obstacles.growth_mps[2],
                    obstacles.growth_mps2[2],
                    command_padding,
                    lease,
                    braking,
                    estimate.velocity_uncertainty_mps,
                ),
            )
            speed_limit = min(speed_limit, entry_speed_limit)
        speed = min(speed_limit, math.sqrt(2 * braking * remaining), kp * remaining / kv)
        if self.traffic_bypass is not None and self.bypass_waypoint == 2:
            # Entry to a region is not docking at its boundary. Retain measured
            # closing motion while a stop fits outside the station body; the
            # independent raw motion/stop certificate remains mandatory below.
            speed = checkpoint_entry_speed(
                position,
                target,
                velocity,
                speed,
                speed_limit,
                braking,
                obstacles.radii_m[2],
            )
        if intermediate and not departure:
            speed = min(
                speed,
                math.sqrt(
                    2
                    * braking
                    * max(
                        0.0,
                        length - max(0.0, 20.0 - config.arrival_inset_m),
                    )
                ),
            )
        goal_velocity = direction * speed
        steering_encounters = 0
        traffic_deadline = None
        if not departure:
            _, _, steering_encounters, traffic_deadline = _traffic_encounters(
                observation,
                obstacles,
                position,
                velocity,
                config.max_stopping_horizon_s,
                advisory_velocities=self.radar_history.advisory_velocities_mps,
            )
        cruise_horizon = lease + (speed_limit + estimate.velocity_uncertainty_mps) / braking
        radial_reserve = obstacles.radii_m[0] + obstacles.growth_mps[0] * cruise_horizon
        radial_reserve += obstacles.growth_mps2[0] * cruise_horizon**2
        radar_gaps = (
            np.hypot.reduce(obstacles.centers_m[3:] - position, axis=1) - obstacles.radii_m[3:]
        )
        radar_reserve = (
            obstacles.growth_mps[3:] * cruise_horizon
            + obstacles.growth_mps2[3:] * cruise_horizon**2
        )
        escape_active = not departure and (
            steering_encounters > 0 or radial < radial_reserve or np.any(radar_gaps < radar_reserve)
        )
        route_advice = None
        if not departure and self.traffic_bypass is not None:
            # Plan spatial progress through the measured near field instead of
            # reversing a velocity heading after every noisy far-flow forecast.
            # Added nominal padding encourages room for the existing full stop;
            # it is not a replacement for any raw time-growing safety envelope.
            reserve_time = 0.5 * config.planner.forecast_horizon_s
            available_gap = max(0.0, float(np.min(radar_gaps)))
            nominal_padding = np.minimum(
                obstacles.growth_mps[3:] * reserve_time
                + obstacles.growth_mps2[3:] * reserve_time**2,
                0.75 * available_gap,
            )
            nominal_radii = np.array(obstacles.radii_m, copy=True)
            # A spatial search starts at the measured position. Replacing its
            # current enclosing radius with a future stop radius can put that
            # clear start inside its own advisory obstacle and disable every
            # connector. Future growth belongs to the complete motion/stop
            # proof below, which still retains all original coefficients.
            nominal_radii[3:] += nominal_padding
            nominal_velocities = np.array(obstacles.velocities_mps, copy=True)
            if self.radar_history.advisory_velocities_mps is not None:
                # This channel already distinguishes public inlet hypotheses
                # from anonymous displacement advice. An unassociated radial
                # projection is not a nominal world trajectory toward the ship.
                nominal_velocities[3:] = self.radar_history.advisory_velocities_mps
            nominal_geometry = TemporalObstacles(
                obstacles.centers_m,
                nominal_velocities,
                nominal_radii,
                obstacles.growth_mps,
                obstacles.growth_mps2,
            )
            if self.bypass_waypoint == 2:
                approach_radius = interior_radius
                if approach_radius <= obstacles.radii_m[2]:
                    raise ValueError("Checkpoint entry cannot fit the unchanged target clearance")
                try:
                    candidate, approach_index = checkpoint_approach(
                        position,
                        target,
                        approach_radius,
                        nominal_geometry,
                    )
                    if self.arrival_approach is None or not approach_index.clear(
                        self.arrival_approach,
                        self.arrival_approach,
                    ):
                        self.arrival_approach = candidate
                except ValueError:
                    if self.arrival_approach is None:
                        self.arrival_approach = point(
                            target
                            + (position - target) / math.dist(position, target) * approach_radius,
                            "unverified checkpoint approach preference",
                        )
                goal = np.asarray(self.arrival_approach)
                approach_delta = goal - position
                approach_distance = math.hypot(*approach_delta)
                direction = (
                    approach_delta / approach_distance if approach_distance > 0 else np.zeros(3)
                )
                goal_velocity = direction * speed
            route_advice = self.nominal_route.choose(
                position,
                target,
                nominal_geometry,
                now,
                config.planner,
                primary_goal=goal,
                context_key=(
                    observation.get("seed"),
                    observation.get("trial_revision"),
                    observation.get("tuning_revision"),
                    observation.get("frame"),
                    self.bypass_waypoint,
                ),
            )
            # No geometric result is a clearance veto or a complete route. The
            # same full all-row stop checker below remains mandatory in both cases.
            if route_advice.target_world_m is not None:
                goal = np.asarray(route_advice.target_world_m)
                local_delta = goal - position
                local_distance = math.hypot(*local_delta)
                speed = min(
                    speed,
                    math.sqrt(
                        2
                        * braking
                        * max(
                            0.0,
                            local_distance
                            - min(config.arrival_inset_m, 0.5 * route_advice.waypoint_tolerance_m),
                        )
                    ),
                )
                goal_velocity = local_delta / local_distance * speed
                axis = position / radial
                outward = max(0.0, float(np.dot(velocity, axis)))
                if radial < radial_reserve and np.dot(goal_velocity, axis) < outward < speed:
                    lateral = goal_velocity - axis * np.dot(goal_velocity, axis)
                    lateral_length = math.hypot(*lateral)
                    if lateral_length > 0.0:
                        goal_velocity = axis * outward + lateral / lateral_length * math.sqrt(
                            speed**2 - outward**2
                        )
        goal_velocity = _bounded(goal_velocity, speed_limit)
        nominal = _bounded((goal_velocity - velocity) * min(kv, 1 / (gain * lease)), maximum)
        flow = _public_flow(observation)
        if departure:
            lateral = velocity - self.anchor * np.dot(velocity, self.anchor)
            nominal = _bounded(
                self.anchor * np.dot(nominal, self.anchor) - lateral / (gain * lease), maximum
            )
            commands = [scale * nominal for scale in (1.0, 0.5, 0.25, 0.125)]
            for magnitude in (maximum, maximum * 0.5, 0.0, -braking / gain):
                commands.append(
                    _bounded(self.anchor * magnitude - lateral / (gain * lease), maximum)
                )
        else:
            commands = [scale * nominal for scale in (1.0, 0.5, 0.25, 0.125)]
            commands.append(np.zeros(3))
            # A full-authority direction can fail its stopping reserve while a
            # smaller burn in that same direction remains admissible. Sampling
            # only full burns forces needless braking or direction reversals.
            directions = [
                maximum * scale * direction
                for scale in (1.0, 0.5, 0.25)
                for direction in _DIRECTIONS
            ]
            if radial_prefix:
                # Keep the original bounded radial departure prefix available
                # until its endpoint. Merely crossing the enclosing sphere is
                # not a reason to reject an otherwise supported outward motion.
                index = max(
                    range(len(_DIRECTIONS)), key=lambda i: np.dot(_DIRECTIONS[i], self.anchor)
                )
                lateral = velocity - self.anchor * np.dot(velocity, self.anchor)
                directions[index] = _bounded(
                    maximum * self.anchor - lateral / (gain * lease), maximum
                )
            commands.extend(directions)
            if escape_active:
                axis = position / radial
                side = goal_velocity - axis * np.dot(goal_velocity, axis)
                if math.hypot(*side) < 1e-8:
                    side = np.cross(axis, (0.0, 1.0, 0.0))
                if math.hypot(*side) < 1e-8:
                    side = np.cross(axis, (0.0, 0.0, 1.0))
                side /= math.hypot(*side)
                second = np.cross(axis, side)
                # Reachable outward/sideways combinations around the complete
                # circle. No candidate is assigned a hypothetical start speed.
                for angle in np.linspace(0.0, 2 * math.pi, 8, endpoint=False):
                    tangent = side * math.cos(angle) + second * math.sin(angle)
                    for outward in (0.5, 1.0, 2.0):
                        direction = tangent + outward * axis
                        direction /= math.hypot(*direction)
                        commands.extend(maximum * scale * direction for scale in (1.0, 0.5, 0.25))
            if math.hypot(*velocity) > 0:
                commands.append(_bounded(-velocity / (gain * lease), maximum))
        seed_command = None
        seed_age = None
        if self.nominal_motion_plan is not None and self.nominal_motion_source_s is not None:
            seed_age = now - self.nominal_motion_source_s
            query = seed_age + lease
            plan = self.nominal_motion_plan
            if seed_age >= 0.0 and len(plan.times_s) > 1 and query <= plan.times_s[-1]:
                desired = np.asarray(
                    [
                        np.interp(query, plan.times_s, plan.velocities_mps[:, axis])
                        for axis in range(3)
                    ]
                )
                seed_command = _bounded((desired - velocity) / (gain * lease), maximum)
                commands.append(seed_command)
        valid = []
        seen = set()
        rejected = Counter()
        flow_axis = (
            None if flow is None or math.hypot(*flow[0]) == 0.0 else flow[0] / math.hypot(*flow[0])
        )
        lateral_speed = (
            None
            if flow_axis is None
            else math.hypot(*(velocity - flow_axis * np.dot(velocity, flow_axis)))
        )
        measured_fast_deadline = None
        if flow is not None:
            fast_threshold = 0.5 * max(0.0, math.hypot(*flow[0]) - flow[1][3])
            rows = checked_radar(observation, config.radar_age_s)
            directly_closing = rows[:, 1, 1] < -fast_threshold
            _, _, _, measured_fast_deadline = _traffic_encounters(
                observation,
                obstacles,
                position,
                velocity,
                config.max_stopping_horizon_s,
                advisory_velocities=obstacles.velocities_mps[3:],
                eligible_rows=directly_closing,
            )
        commands = [_bounded(command, maximum) for command in commands]

        def rank(command):
            return (
                math.hypot(*(velocity + command * gain * lease - goal_velocity)),
                math.hypot(*(command * gain - self.previous_acceleration))
                if escape_active
                else 0.0,
                math.hypot(*command),
            )

        commands.sort(key=rank)
        endpoint_blocked = set()
        if not departure:
            padding = [math.hypot(*(command * gain)) * lease**2 / 32 for command in commands]
            padding = (
                np.asarray(padding)
                + 0.5 * (estimator.accel_allowance_mps2 + 2 * maximum * gain) * lease**2
            )
            endpoint_blocked = blocked_radar_stop_endpoints(
                position,
                velocity,
                commands,
                gain,
                lease,
                braking,
                estimate.velocity_uncertainty_mps,
                obstacles,
                padding,
                config.max_stopping_horizon_s,
            )
        context = {
            "axis": self.anchor,
            "radial_prefix": radial_prefix,
            "departure": departure,
            "departure_radius": self.departure_radius,
            "tolerance": estimate.position_uncertainty_m + 0.01,
        }
        proposals = []
        for command in commands:
            key = tuple(np.round(command, 14))
            if key in seen:
                continue
            seen.add(key)
            end_velocity = velocity + command * gain * lease
            if math.hypot(*end_velocity) > max(speed_limit, math.hypot(*velocity)) + 1e-12:
                rejected["speed_limit"] += 1
                continue
            if key in endpoint_blocked:
                rejected["radar_occupancy"] += 1
                continue
            proposals.append(command)

        certificates = {}

        def admissible(index):
            if index in certificates:
                return certificates[index] is not None
            command = proposals[index]
            segments = stopping_motion(
                position,
                velocity,
                command * gain,
                lease,
                braking,
                estimate.velocity_uncertainty_mps,
            )
            curve = math.hypot(*(command * gain)) * lease**2 / 32
            curve += 0.5 * (estimator.accel_allowance_mps2 + 2 * maximum * gain) * lease**2
            reason, supported_radial, horizon = _motion_rejection(
                segments,
                obstacles,
                config,
                padding=curve,
                **context,
            )
            if reason is not None:
                rejected[reason] += 1
                certificates[index] = None
                return False
            certificate = (command, horizon, supported_radial)
            certificates[index] = certificate
            valid.append(certificate)
            return True

        selected = None
        motion_advice = None
        motion_advice_used = False
        if (
            not departure
            and escape_active
            and len(proposals) > 1
            and config.max_stopping_horizon_s >= lease
        ):
            forecasts = np.array(obstacles.velocities_mps, copy=True)
            if self.radar_history.advisory_velocities_mps is not None:
                forecasts[3:] = self.radar_history.advisory_velocities_mps
            proposed_controls = np.asarray(proposals)
            advice_padding = float(np.max(np.hypot.reduce(proposed_controls * gain, axis=1)))
            advice_padding *= lease**2 / 32
            advice_padding += 0.5 * (estimator.accel_allowance_mps2 + 2 * maximum * gain) * lease**2
            # Every beam root must pass this source's complete stop proof. Check
            # them in preview order, rather than proving every lower-ranked
            # alternative before making the only command that can be sent.
            seed_index = None
            if seed_command is not None:
                seed_key = tuple(np.round(seed_command, 14))
                seed_index = next(
                    (
                        index
                        for index, command in enumerate(proposals)
                        if tuple(np.round(command, 14)) == seed_key
                    ),
                    None,
                )
            seed_args = (
                {}
                if seed_index is None
                else {
                    "seed_plan": self.nominal_motion_plan,
                    "seed_age_s": seed_age,
                    "seed_control_index": seed_index,
                }
            )
            motion_advice = search_nominal_motion(
                position,
                velocity,
                proposed_controls,
                obstacles,
                forecasts,
                goal,
                speed,
                maximum,
                gain,
                lease,
                horizon=min(40.0, config.max_stopping_horizon_s),
                goal_tolerance=min(4.0, 0.1 * config.planner.cell_size_m),
                braking_accel_mps2=braking,
                velocity_allowance_mps=estimate.velocity_uncertainty_mps,
                command_padding_m=advice_padding,
                root_is_admissible=admissible,
                **seed_args,
            )
            if (
                motion_advice.advice is not None
                and motion_advice.advice.minimum_gap_lower_bound_m > 0.0
                and admissible(motion_advice.advice.control_index)
            ):
                selected = certificates[motion_advice.advice.control_index]
                motion_advice_used = True
                self.nominal_motion_plan = motion_advice.advice
                self.nominal_motion_source_s = now
        if selected is None:
            for index in range(len(proposals)):
                if admissible(index):
                    selected = certificates[index]
                    break
        report = {
            "guidance_strategy": "time_indexed_local_motion",
            "source_time_s": now,
            "decision_valid_until_s": now + lease,
            "checked_radar_rows": len(observation["radar"]),
            "history_velocity_rows": inferred_rows,
            "history_velocity_model": "mutually_unique_anonymous_displacements_with_added_error",
            "history_associated_rows": self.radar_history.associated_rows,
            "history_corroborated_velocity_rows": self.radar_history.corroborated_rows,
            "radar_model": (
                "three_scan_corroborated_motion_with_all_original_allowances_and_added_error"
            )
            if self.radar_history.corroborated_rows
            else ("unchanged_raw_radial_safety_envelopes_with_all_additive_margins"),
            "unmodelled_obstacles_lower_bound": unobserved,
            "traffic_bypass_waypoint": self.bypass_waypoint
            if self.traffic_bypass is not None
            else None,
            "nominal_initial_exit_gap_m": self.initial_exit_gap_m,
            "nominal_exit_kind": "initial_all_row_current_3d_ray_advice_only",
            "nominal_exit_staging_reached": self.exit_staged,
            "unknown_tangential_speed_allowance_mps": config.planner.unknown_tangential_speed_mps,
            "surface_departure": departure,
            "terrain_clearance_verified": False,
            "local_candidates": len(seen),
            "candidate_selection": (
                "nominal_motion_among_current_full_stop_certificates"
                if motion_advice_used
                else "bounded_reachable_maneuvers_with_checked_full_stop"
            ),
            "nominal_motion_status": None if motion_advice is None else motion_advice.status,
            "nominal_motion_used": motion_advice_used,
            "nominal_motion_pair_intervals": 0
            if motion_advice is None
            else motion_advice.evaluated_pair_intervals,
            "nominal_motion_beam_width": None
            if motion_advice is None
            else motion_advice.beam_width,
            "nominal_motion_duration_wall_s": None
            if motion_advice is None
            else motion_advice.elapsed_s,
            "nominal_motion_safety_clearance": False,
            "nominal_motion_source_freshness": False,
            "selected_nominal_motion_cost": (
                motion_advice.advice.cost.tolist() if motion_advice_used else None
            ),
            "nominal_motion_proposal_cost": (
                None
                if motion_advice is None or motion_advice.advice is None
                else motion_advice.advice.cost.tolist()
            ),
            "nominal_motion_proposal_gap_m": (
                None
                if motion_advice is None or motion_advice.advice is None
                else motion_advice.advice.minimum_gap_lower_bound_m
            ),
            "local_rejections": dict(rejected),
            "admissible_local_candidates": len(valid),
            "desired_speed_mps": speed,
            "desired_velocity_world_mps": point(goal_velocity, "desired advisory velocity"),
            "waypoint_index": self.waypoint,
            "next_waypoint_world_m": point(goal, "local advisory waypoint"),
            "global_route_kind": "advisory_geometry_only_not_a_complete_current_route",
            "braking_model_kind": "assumed_active_braking_not_fault_coasting",
            "braking_model_accel_mps2": braking,
            "reference_accel_limit_mps2": maximum,
            "configured_speed_limit_mps": speed_limit,
            "advisory_flow_encounters": steering_encounters,
            "advisory_traffic_deadline_s": traffic_deadline,
            "measured_cross_stream_speed_mps": lateral_speed,
            "directly_closing_traffic_deadline_s": measured_fast_deadline,
            "nominal_checkpoint_entry_radius_m": interior_radius,
            "checkpoint_stop_speed_limit_mps": entry_speed_limit,
            "advisory_velocity_kind": (
                "raw_radial_only_no_public_motion_hypothesis"
                if self.radar_history.advisory_velocities_mps is None
                else (
                    "mixed_nominal_inlet_and_anonymous_displacement_advice_not_measured_velocity"
                    if self.radar_history.associated_rows
                    else "public_nominal_inlet_flow_not_measured_rock_velocity"
                )
            ),
            "escape_maneuver_search": bool(escape_active),
            "continuation_checks": 0,
            "continuation_checks_by_finalist": [0] * len(valid),
            "continuation_kind": "not_evaluated_no_future_command_permission",
            "nominal_route_status": None if route_advice is None else route_advice.status,
            "nominal_route_kind": None if route_advice is None else route_advice.kind,
            "nominal_route_reused": False if route_advice is None else route_advice.reused,
            "nominal_route_expansions": 0 if route_advice is None else route_advice.expanded_nodes,
            "nominal_route_visibility_checks": 0
            if route_advice is None
            else route_advice.collision_checks,
        }
        if selected is None:
            return LocalDecision(
                (0.0, 0.0, 0.0),
                {**report, "reason": "No admissible time-indexed motion and stopping reserve"},
            )
        command, horizon, supported_radial = selected
        self.previous_acceleration = command * gain
        endpoint_margin = None
        if not supported_radial:
            padding = math.hypot(*(command * gain)) * lease**2 / 32
            padding += 0.5 * (estimator.accel_allowance_mps2 + 2 * maximum * gain) * lease**2
            endpoint_margin = float(
                stop_endpoint_margins(
                    position,
                    velocity,
                    np.asarray((command,)),
                    gain,
                    lease,
                    braking,
                    estimate.velocity_uncertainty_mps,
                    obstacles,
                    np.asarray((padding,)),
                )[0]
            )
        return LocalDecision(
            point(command, "local command"),
            {
                **report,
                "reason": "Current time-indexed motion and modeled stop passed",
                "command_stopping_horizon_s": horizon,
                "calculated_stop_endpoint_margin_m": endpoint_margin,
                "surface_departure": bool(supported_radial),
                "continuation_clear": False,
                "continuation_stopping_horizon_s": None,
            },
        )
