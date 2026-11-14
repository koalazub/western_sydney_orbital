"""SDK entrypoint for Ali's MPPI controller.

This is a single-file STP submission. The marked core is duplicated in
`mppi_core.py` for analysis and numeric tests; keep the two copies in sync.
"""

# ruff: noqa: E402

#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import json
import sys
import time
import traceback

import spaceteams as st


def custom_exception_handler(exctype, value, tb):
    error_message = "".join(traceback.format_exception(exctype, value, tb))
    st.logger_fatal(error_message)


sys.excepthook = custom_exception_handler
st.connect_to_sim(sys.argv)

import numpy as np
import numpy.typing as npt

this = st.GetThisSystem()
doSpawning = st.SimGlobals.GetSimEntity().GetParam(st.VarType.bool, "DoSpawning")
if doSpawning:
    while True:
        time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))

phobos: st.Entity = this.GetParam(st.VarType.entityRef, "Phobos")
phobosFrame = phobos.GetBodyFixedFrame()
shuttle: st.Entity = this.GetParam(st.VarType.entityRef, "Shuttle")
shuttleFrame = shuttle.GetBodyFixedFrame()
target: st.Entity = this.GetParam(st.VarType.entityRef, "Target")
topCamera: st.Entity = this.GetParam(st.VarType.entityRef, "TopCamera")
bottomCamera: st.Entity = this.GetParam(st.VarType.entityRef, "BottomCamera")


def GetNearestDebrisLocations(N: int) -> list[npt.NDArray]:
    allDebrisInfo: list[npt.NDArray] = shuttle.GetParamArray(
        st.VarType.doubleM3x3, ["Radar", "SortedDebrisData"]
    )
    return allDebrisInfo[: min(N, len(allDebrisInfo))]


def SetCommand(accelCommand: npt.NDArray, angAccelCommand: npt.NDArray) -> None:
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accelCommand)
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAngAccel"], angAccelCommand)


#############################################################################################
# DON'T CHANGE ANY OF THE ABOVE. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

# BEGIN ALI MPPI CORE

ARRIVAL_RADIUS_M = 50.0
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
MPPI_SAMPLES, MPPI_STEPS = 48, 40
PLAN_DT_S = 0.25
CRUISE_SPEED_MPS = 7.0
BRAKE_ACCEL_MPS2 = 0.5
BURN_START_MPS, BURN_STOP_MPS = 0.50, 0.30
ATTITUDE_RATE_GAIN = 4.0
ATTITUDE_BRAKE_SCALE = 0.65
ATTITUDE_TERMINAL_RAD = np.radians(0.5)
ATTITUDE_HOLD_RAD = np.radians(1.0)
ATTITUDE_RELEASE_RAD = np.radians(2.0)
ATTITUDE_HOLD_RATE_RAD_S = 0.01
ATTITUDE_RELEASE_RATE_RAD_S = 0.02
CONTROL_SWITCH_WEIGHT = 0.50
SAFETY_MARGIN_M = 2.0
REACTION_TIME_S = 0.5
RISK_WEIGHT = 200.0
EMERGENCY_RANGE_M = 120.0
EMERGENCY_LOOKAHEAD_S = 5.0
EMERGENCY_MARGIN_M = 4.0
EMERGENCY_ESCAPE_MPS = 2.5
MIN_TIME_TO_GO_SPEED_MPS = 1.0
MIN_CRUISE_SPEED_MPS = 1.0
MAX_PLAN_DT_S = 0.5
TERMINAL_COAST_RANGE_M = 200.0
TERMINAL_COAST_MIN_SPEED_MPS = 4.0
DEFAULT_EXHAUST_VELOCITY_MPS = 80.0 * 9.8
DEFAULT_TIME_BUDGET_MULTIPLE = 2.5
CRUISE_TIME_FRACTION = 0.6
DEFAULT_SCORE = {
    "nominal_time_s": 120.0,
    "nominal_fuel_kg": 75.0,
    "impact_threshold_j": 209200.0,
    "arrival_radius_m": ARRIVAL_RADIUS_M,
    "points_scale": 100.0,
    "damage_multiplier": 2.0,
    "score_offset": 0.5,
}


def score_parameters(score=None, arrival_radius=None):
    """Resolve the published score coefficients once, not at every sample."""
    values = dict(DEFAULT_SCORE)
    if isinstance(score, dict):
        for key in values:
            value = score.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not np.isfinite(value) or value < 0.0:
                continue
            if key != "score_offset" and value <= 0.0:
                continue
            values[key] = float(value)
    if arrival_radius is not None:
        try:
            radius = float(arrival_radius)
        except (TypeError, ValueError):
            radius = 0.0
        if np.isfinite(radius) and radius > 0.0:
            values["arrival_radius_m"] = radius
    return values


def terminal_time(remaining_m, speed_mps, cruise_mps, accel_mps2):
    """Braking-profile time estimate, vectorized over samples."""
    remaining = np.maximum(np.asarray(remaining_m, dtype=float), 0.0)
    speed = np.maximum(np.asarray(speed_mps, dtype=float), 0.0)
    cruise = np.maximum(np.asarray(cruise_mps, dtype=float), MIN_CRUISE_SPEED_MPS)
    cruise = np.maximum(cruise, speed)
    accel = np.maximum(np.asarray(accel_mps2, dtype=float), 1e-6)
    accel_distance = np.maximum(0.0, (cruise * cruise - speed * speed) / (2.0 * accel))
    brake_distance = cruise * cruise / (2.0 * accel)
    cruise_time = (
        (cruise - speed) / accel
        + (remaining - accel_distance - brake_distance) / cruise
        + cruise / accel
    )
    peak = np.sqrt(np.maximum(0.0, accel * remaining + 0.5 * speed * speed))
    triangular_time = (peak - speed) / accel + peak / accel
    arrival_time = remaining / np.maximum(speed, 1e-6)
    result = np.where(remaining >= accel_distance + brake_distance, cruise_time, triangular_time)
    result = np.where(remaining <= speed * speed / (2.0 * accel), arrival_time, result)
    return np.maximum(result, 0.0)


def optimal_cruise_speed(
    remaining_m,
    speed_mps,
    max_accel_mps2,
    score,
    fuel_kg,
    damage_j,
    fuel_rate_kg_s,
    now_s,
    time_budget_s=None,
):
    """Choose the cruise speed that maximizes the score under the job budget.

    Retained for a disturbance-aware rollout. The stable reference policy does
    not call it: without the local environment acceleration, a lower target can
    sit in a gravity-balanced regime and burn without accelerating.
    """
    remaining = max(0.0, float(remaining_m))
    if remaining <= 0.0:
        return 0.0
    accel = max(1e-6, float(max_accel_mps2))
    speed = max(0.0, float(speed_mps))
    values = score_parameters(score)
    budget = (
        DEFAULT_TIME_BUDGET_MULTIPLE * values["nominal_time_s"]
        if time_budget_s is None
        else float(time_budget_s)
    )
    margin = max(5.0, 0.05 * max(budget, 1.0))
    maximum = max(MIN_CRUISE_SPEED_MPS, np.sqrt(2.0 * accel * remaining))
    candidates = np.linspace(MIN_CRUISE_SPEED_MPS, maximum, 33)
    best_speed, best_score = None, -np.inf
    fuel_rate = max(0.0, float(fuel_rate_kg_s))
    for candidate in candidates:
        elapsed = float(terminal_time(remaining, speed, candidate, accel))
        if now_s + elapsed > budget - margin:
            continue
        burn_time = (abs(candidate - speed) + candidate) / accel
        fuel_used = max(0.0, float(fuel_kg)) + fuel_rate * burn_time
        value = values["points_scale"] * (
            values["nominal_time_s"] / max(now_s + elapsed, 1e-3)
            - fuel_used / values["nominal_fuel_kg"]
            - values["damage_multiplier"] * max(0.0, float(damage_j)) / values["impact_threshold_j"]
            + values["score_offset"]
        )
        if value > best_score:
            best_speed, best_score = float(candidate), float(value)
    return maximum if best_speed is None else best_speed


def unit(value):
    length = np.linalg.norm(value, axis=-1, keepdims=True)
    return np.divide(value, length, out=np.zeros_like(value), where=length > 1e-9)


def capsule_distance(points, centers, axis, half_length, radius):
    offset = centers[None, :, :] - points[:, None, :]
    along = np.clip(np.einsum("knd,d->kn", offset, axis), -half_length, half_length)
    closest = points[:, None, :] + along[:, :, None] * axis
    return np.linalg.norm(centers[None, :, :] - closest, axis=2) - radius


def cruise_velocity(
    position,
    target_position,
    cruise_speed_mps=CRUISE_SPEED_MPS,
    arrival_radius_m=ARRIVAL_RADIUS_M,
    brake_accel_mps2=BRAKE_ACCEL_MPS2,
):
    delta = target_position - position
    distance = np.linalg.norm(delta)
    if distance < 1e-9:
        return np.zeros(3)
    speed = max(0.0, float(cruise_speed_mps))
    return speed * delta / distance


def target_direction(position, target_position):
    return unit(target_position - position)


def govern(desired_accel, nominal_velocity, velocity, burning, max_accel):
    error_vec = nominal_velocity - velocity
    error = np.linalg.norm(error_vec)
    desired_norm = np.linalg.norm(desired_accel)
    target_unit = unit(nominal_velocity)
    forward_avoid = (
        desired_norm > BURN_START_MPS
        and float(desired_accel @ target_unit) > 0.0
        and error > BURN_STOP_MPS
    )
    if burning:
        if error < BURN_STOP_MPS and not forward_avoid:
            burning = False
    elif error > BURN_START_MPS or forward_avoid:
        burning = True
    if not burning:
        return np.zeros(3), False
    direction = desired_accel if forward_avoid else error_vec
    return max_accel * unit(direction), True


def closest_margin(velocity, relative, rock_velocities, keep_out, horizon):
    relative_velocity = rock_velocities - velocity
    denominator = np.einsum("ij,ij->i", relative_velocity, relative_velocity) + 1e-9
    time = np.clip(
        -np.einsum("ij,ij->i", relative, relative_velocity) / denominator,
        0.0,
        horizon,
    )
    miss = np.linalg.norm(relative + time[:, None] * relative_velocity, axis=1)
    return miss - keep_out


def emergency_guard(velocity, rows, now, max_accel):
    velocity = np.asarray(velocity, dtype=float)
    relative, rock_velocities, keep_out = [], [], []
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        position = np.asarray(row[0], dtype=float)
        line_of_sight = unit(position)
        rock_velocity = line_of_sight * (float(row[1, 1]) + line_of_sight @ velocity)
        position = position + (now - float(row[2, 0])) * (rock_velocity - velocity)
        if np.linalg.norm(position) > EMERGENCY_RANGE_M:
            continue
        relative.append(position)
        rock_velocities.append(rock_velocity)
        keep_out.append(0.55 * float(row[1, 2]) + EMERGENCY_MARGIN_M)
    if not relative:
        return np.zeros(3), False
    relative = np.array(relative)
    rock_velocities = np.array(rock_velocities)
    keep_out = np.array(keep_out)
    current = closest_margin(velocity, relative, rock_velocities, keep_out, EMERGENCY_LOOKAHEAD_S)
    if np.min(current) >= 0.0:
        return np.zeros(3), False
    threat = int(np.argmin(current))
    heading = unit(velocity) if np.linalg.norm(velocity) > 1e-6 else np.array([1.0, 0.0, 0.0])
    lateral = relative[threat] - (relative[threat] @ heading) * heading
    if np.linalg.norm(lateral) > 1e-6:
        side = unit(lateral)
    else:
        helper = np.array([0.0, 0.0, 1.0]) if abs(heading[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
        side = unit(np.cross(heading, helper))
    away = unit(-relative[threat])
    options = (
        velocity + EMERGENCY_ESCAPE_MPS * away,
        velocity + EMERGENCY_ESCAPE_MPS * side,
        velocity - EMERGENCY_ESCAPE_MPS * side,
        velocity,
    )
    scores = [
        np.min(closest_margin(option, relative, rock_velocities, keep_out, EMERGENCY_LOOKAHEAD_S))
        for option in options
    ]
    best = int(np.argmax(scores))
    if scores[best] <= np.min(current):
        return np.zeros(3), False
    return max_accel * unit(options[best] - velocity), True


def fit_rock(rock):
    if len(rock["samples"]) < 4:
        return
    times = np.array([s[0] for s in rock["samples"]])
    points = np.array([s[1] for s in rock["samples"]])
    if times[-1] - times[0] < 1.0:
        return
    design = np.column_stack((np.ones(len(times)), times - times[0]))
    coef, *_ = np.linalg.lstsq(design, points, rcond=None)
    residual = points - design @ coef
    rock["position"], rock["velocity"] = coef[0], coef[1]
    rock["sigma"] = float(np.sqrt(np.sum(residual**2) / max(1, len(times) - 2)))


def update_belief(state, rows, ship_position, now):
    rocks = state["rocks"]
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        measured = ship_position + row[0]
        diameter, measured_at = float(row[1, 2]), float(row[2, 0])
        best, distance = None, np.inf
        for rock in rocks:
            predicted = rock["position"] + rock["velocity"] * (measured_at - rock["last_seen"])
            gap = np.linalg.norm(predicted - measured)
            if gap < max(15.0, 2.0 * rock["sigma"]) and gap < distance:
                best, distance = rock, gap
        if best is None:
            best = {
                "position": measured.copy(),
                "velocity": np.zeros(3),
                "diameter": diameter,
                "max_diameter": diameter,
                "last_seen": measured_at,
                "samples": [(measured_at, measured.copy())],
                "sigma": 0.5,
            }
            rocks.append(best)
        elif measured_at > best["last_seen"]:
            best["last_seen"] = measured_at
            best["diameter"] = diameter
            best["max_diameter"] = max(best["max_diameter"], diameter)
            best["samples"] = (best["samples"] + [(measured_at, measured.copy())])[-30:]
            fit_rock(best)
    state["rocks"] = [rock for rock in rocks if now - rock["last_seen"] <= 5.0]
    state["rocks"].sort(key=lambda rock: np.linalg.norm(rock["position"] - ship_position))
    state["rocks"] = state["rocks"][:160]
    return state


def plan(
    state,
    position,
    velocity,
    target_position,
    body_x,
    radius,
    max_accel,
    dt,
    fuel,
    damage,
    ship_mass_kg,
    now,
):
    rocks = state["rocks"]
    centers = np.array([rock["position"] for rock in rocks]) if rocks else np.empty((0, 3))
    rock_velocities = np.array([rock["velocity"] for rock in rocks]) if rocks else np.empty((0, 3))
    radii = np.array([0.5 * rock["max_diameter"] for rock in rocks]) if rocks else np.empty(0)
    sigmas = np.array([rock["sigma"] for rock in rocks]) if rocks else np.empty(0)
    if len(rocks):
        nearest = np.argsort(np.linalg.norm(centers - position, axis=1))[:30]
        centers, rock_velocities = centers[nearest], rock_velocities[nearest]
        radii, sigmas = radii[nearest], sigmas[nearest]
    policy = state.setdefault("policy", {})
    values = score_parameters(policy.get("score"), policy.get("arrival_radius_m"))
    budget = policy.get("time_budget_s")
    last_fuel = state.get("fuel_sample")
    last_time = state.get("fuel_time")
    fuel_rate = state.get("fuel_rate_kg_s")
    if last_fuel is not None and last_time is not None:
        elapsed = now - last_time
        burned = fuel - last_fuel
        if elapsed > 1e-6 and burned > 0.0 and state.get("burning"):
            observed = burned / elapsed
            if np.isfinite(observed) and observed > 0.0:
                fuel_rate = observed if fuel_rate is None else 0.8 * fuel_rate + 0.2 * observed
    state["fuel_sample"], state["fuel_time"] = float(fuel), float(now)
    if fuel_rate is None or not np.isfinite(fuel_rate) or fuel_rate <= 0.0:
        fuel_rate = max(
            1e-6,
            float(ship_mass_kg) * float(max_accel) / DEFAULT_EXHAUST_VELOCITY_MPS,
        )
    state["fuel_rate_kg_s"] = float(fuel_rate)
    remaining = max(
        0.0,
        float(np.linalg.norm(target_position - position)) - values["arrival_radius_m"],
    )
    speed = float(np.linalg.norm(velocity))
    budget_value = (
        DEFAULT_TIME_BUDGET_MULTIPLE * values["nominal_time_s"] if budget is None else float(budget)
    )
    reference = state.get("cruise_reference_mps")
    if reference is None:
        reference = remaining / max(1.0, CRUISE_TIME_FRACTION * budget_value)
        state["cruise_reference_mps"] = float(reference)
    cruise_speed = min(
        max(MIN_CRUISE_SPEED_MPS, float(reference)),
        float(np.sqrt(2.0 * max(1e-6, max_accel) * remaining)),
    )
    state["cruise_speed_mps"] = cruise_speed
    nominal = max_accel * unit(
        cruise_velocity(
            position,
            target_position,
            cruise_speed,
            values["arrival_radius_m"],
            max_accel,
        )
        - velocity
    )
    rng = state.setdefault("rng", np.random.default_rng(12345))
    noise = rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3))
    directions = unit(nominal + noise * max_accel)
    magnitudes = rng.random((MPPI_SAMPLES, MPPI_STEPS, 1)) * max_accel
    controls = directions * magnitudes
    remaining_time = now + float(terminal_time(remaining, speed, cruise_speed, max_accel))
    horizon = float(np.clip(0.1 * remaining_time, 8.0, 30.0))
    plan_dt = float(np.clip(horizon / MPPI_STEPS, max(dt, 1e-3), MAX_PLAN_DT_S))
    positions = np.broadcast_to(position, (MPPI_SAMPLES, 3)).copy()
    ship_velocities = np.broadcast_to(velocity, (MPPI_SAMPLES, 3)).copy()
    risk = np.zeros(MPPI_SAMPLES)
    for step in range(MPPI_STEPS):
        positions += ship_velocities * plan_dt + 0.5 * controls[:, step] * plan_dt * plan_dt
        ship_velocities += controls[:, step] * plan_dt
        if len(rocks):
            future = centers + (step + 1) * plan_dt * rock_velocities
            distance = capsule_distance(positions, future, body_x, radius, radius)
            margin = (
                SAFETY_MARGIN_M
                + REACTION_TIME_S * np.linalg.norm(ship_velocities, axis=1)[:, None]
                + 2.0 * sigmas
            )
            risk += np.sum(np.maximum(0.0, margin - (distance - radii)), axis=1)
    sample_remaining = np.maximum(
        0.0,
        np.linalg.norm(positions - target_position, axis=1) - values["arrival_radius_m"],
    )
    time_to_go = terminal_time(
        sample_remaining,
        np.linalg.norm(ship_velocities, axis=1),
        cruise_speed,
        max_accel,
    )
    plan_horizon = MPPI_STEPS * plan_dt
    burn_time = np.sum(np.linalg.norm(controls, axis=2), axis=1) / max(1e-6, max_accel) * plan_dt
    fuel_used = fuel + float(fuel_rate) * burn_time
    score = values["points_scale"] * (
        values["nominal_time_s"] / np.maximum(now + plan_horizon + time_to_go, 1e-3)
        - fuel_used / values["nominal_fuel_kg"]
        - values["damage_multiplier"] * damage / values["impact_threshold_j"]
        + values["score_offset"]
    )
    switch = np.linalg.norm(controls[:, 0] - state["last_control"], axis=1)
    risk_weight = values["points_scale"] * values["damage_multiplier"]
    cost = -score + risk_weight * risk + CONTROL_SWITCH_WEIGHT * switch
    best = int(np.argmin(cost))
    state["last_control"] = controls[best, 0].copy()
    return controls[best, 0]


def _finite(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def angle_between(first, second):
    first = unit(first)
    second = unit(second)
    if np.linalg.norm(first) < 1e-9 or np.linalg.norm(second) < 1e-9:
        return None
    return float(np.arccos(np.clip(float(first @ second), -1.0, 1.0)))


def attitude(body_x, angular_velocity, desired, max_angular_accel, dt, state, diagnostics=None):
    trace = diagnostics if diagnostics is not None else {}
    trace.update(
        {
            "angle_rad": 0.0,
            "angle_deg": 0.0,
            "rate_rad_s": 0.0,
            "desired_rate_rad_s": 0.0,
            "rate_error_rad_s": 0.0,
            "along_gain_rad_s2": 0.0,
            "off_axis_rad_s2": 0.0,
            "axis_world": [0.0, 0.0, 0.0],
            "requested_rad_s2": [0.0, 0.0, 0.0],
            "requested_norm_rad_s2": 0.0,
            "command_rad_s2": [0.0, 0.0, 0.0],
            "command_norm_rad_s2": 0.0,
            "saturated": False,
            "hold": False,
            "released": False,
        }
    )
    desired = unit(desired)
    if np.linalg.norm(desired) < 1e-9:
        state["attitude_hold"] = True
        trace["hold"] = True
        return np.zeros(3)
    cross = np.cross(body_x, desired)
    sine = np.linalg.norm(cross)
    angle = np.arctan2(sine, float(np.clip(body_x @ desired, -1.0, 1.0)))
    speed = np.linalg.norm(angular_velocity)
    axis = cross / sine if sine > 1e-9 else unit(np.cross(body_x, np.array([0.0, 0.0, 1.0])))
    rate_along = float(angular_velocity @ axis)
    trace.update(
        {
            "angle_rad": float(angle),
            "angle_deg": float(np.degrees(angle)),
            "rate_rad_s": float(speed),
            "axis_world": [float(value) for value in axis],
        }
    )
    if state.get("attitude_hold", False):
        if angle <= ATTITUDE_RELEASE_RAD and speed <= ATTITUDE_RELEASE_RATE_RAD_S:
            trace["hold"] = True
            return np.zeros(3)
        state["attitude_hold"] = False
        trace["released"] = True
    if angle <= ATTITUDE_HOLD_RAD and speed <= ATTITUDE_HOLD_RATE_RAD_S:
        state["attitude_hold"] = True
        trace["hold"] = True
        return np.zeros(3)
    safe_rate = np.sqrt(
        2.0 * max_angular_accel * ATTITUDE_BRAKE_SCALE * max(angle - ATTITUDE_TERMINAL_RAD, 0.0)
    )
    forced_rate = axis * safe_rate
    rate_error = forced_rate - angular_velocity
    requested = ATTITUDE_RATE_GAIN * rate_error
    length = np.linalg.norm(requested)
    command = requested / length * max_angular_accel if length > max_angular_accel else requested
    along_error = float(rate_error @ axis)
    trace.update(
        {
            "desired_rate_rad_s": float(safe_rate),
            "rate_error_rad_s": float(safe_rate - rate_along),
            "along_gain_rad_s2": float(ATTITUDE_RATE_GAIN * (safe_rate - rate_along)),
            "off_axis_rad_s2": float(
                ATTITUDE_RATE_GAIN * np.linalg.norm(rate_error - axis * along_error)
            ),
            "requested_rad_s2": [float(value) for value in requested],
            "requested_norm_rad_s2": float(length),
            "saturated": bool(length > max_angular_accel),
            "command_rad_s2": [float(value) for value in command],
            "command_norm_rad_s2": float(np.linalg.norm(command)),
        }
    )
    return command


def telemetry_record(
    now,
    dt,
    mode,
    gate,
    body_x_world,
    target_direction_world,
    accel_world,
    accel_body,
    angular_velocity_world,
    diagnostics,
    velocity_error,
    speed_mps,
    target_range,
    track_count,
    fuel,
    damage,
):
    """Build one frame-labelled record for STP logger_info and local progress."""
    record = {
        "schema": "wsorb.mppi_attitude",
        "schema_version": 1,
        "time_s": _finite(now),
        "dt_s": _finite(dt),
        "mode": mode if mode in ("burn", "coast") else "unknown",
        "gate": gate if gate in ("burn", "hold", "rate", "alignment", "off") else "unknown",
        "attitude_frame": "world",
        "accel_frame": "body",
        "target_align_rad": _finite(angle_between(body_x_world, target_direction_world)),
        "burn_align_rad": _finite(angle_between(body_x_world, accel_world)),
        "translation_align_rad": _finite(angle_between(np.array([1.0, 0.0, 0.0]), accel_body)),
        "accel_body_mps2": [_finite(value) for value in accel_body],
        "accel_norm_mps2": _finite(np.linalg.norm(accel_body)),
        "angular_velocity_world_rad_s": [_finite(value) for value in angular_velocity_world],
        "velocity_error_mps": _finite(velocity_error),
        "speed_mps": _finite(speed_mps),
        "target_range_m": _finite(target_range),
        "track_count": int(track_count),
        "fuel_kg": _finite(fuel),
        "damage_j": _finite(damage),
    }
    for key, value in diagnostics.items():
        if key in record or key == "line":
            continue
        if isinstance(value, (list, tuple)):
            record[key] = [_finite(item) for item in value]
        elif isinstance(value, (bool, np.bool_)):
            record[key] = bool(value)
        elif isinstance(value, (int, np.integer)):
            record[key] = int(value)
        elif isinstance(value, (float, np.floating)):
            record[key] = _finite(value)
        else:
            record[key] = value
    record["warning"] = attitude_warning(record)
    record["line"] = telemetry_line(record)
    return record


def telemetry_line(record):
    def number(key, digits=1):
        value = record.get(key)
        return "?" if value is None else f"{float(value):.{digits}f}"

    def degrees(key):
        value = record.get(key)
        return "?" if value is None else f"{np.degrees(float(value)):.1f}"

    return (
        f"MPPI t={number('time_s', 2)}s mode={record.get('mode', '?')} "
        f"gate={record.get('gate', '?')} align={degrees('target_align_rad')}deg "
        f"burn={degrees('burn_align_rad')}deg trans={degrees('translation_align_rad')}deg "
        f"rate={number('rate_rad_s', 3)} want={number('desired_rate_rad_s', 3)} "
        f"err={number('rate_error_rad_s', 3)} off={number('off_axis_rad_s2', 3)} "
        f"cmd={number('command_norm_rad_s2', 3)}/0.050 "
        f"sat={int(bool(record.get('saturated')))} hold={int(bool(record.get('hold')))} "
        f"emerg={int(bool(record.get('emergency')))} "
        f"v={number('speed_mps', 2)} err={number('velocity_error_mps', 2)} "
        f"range={number('target_range_m', 0)}m "
        f"fuel={number('fuel_kg', 1)} dmg={number('damage_j', 0)} "
        f"rocks={record.get('track_count', 0)}"
    )


def attitude_warning(record):
    if record.get("mode") != "coast":
        return None
    align = record.get("target_align_rad")
    rate = record.get("rate_rad_s")
    if align is not None and rate is not None and align > np.radians(5.0) and rate > 0.02:
        return "coast_misaligned_with_residual_rate"
    return None


# END ALI MPPI CORE

t0 = (
    st.SimGlobals.GetSimEntity()
    .GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"])
    .as_datetime()
)
state = {
    "rocks": [],
    "burning": False,
    "attitude_hold": False,
    "last_control": np.zeros(3),
    "rng": np.random.default_rng(12345),
    "policy": {
        "time_budget_s": DEFAULT_TIME_BUDGET_MULTIPLE * DEFAULT_SCORE["nominal_time_s"],
        "arrival_radius_m": ARRIVAL_RADIUS_M,
    },
}
last_time, last_status, last_display, last_alert = None, -np.inf, -np.inf, -np.inf

while True:
    time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))
    now = (st.SimGlobals.SimClock.GetTimeNow().as_datetime() - t0).total_seconds()
    dt = (
        1.0 / this.GetParam(st.VarType.double, "LoopFreqHz")
        if last_time is None
        else max(1e-3, now - last_time)
    )
    last_time = now
    stopped = shuttle.getLocVelAcc()
    position = np.asarray(stopped.loc_WRT_ExprIn(phobosFrame), dtype=float)
    velocity = np.asarray(stopped.vel_WRT_ExprIn(phobosFrame), dtype=float)
    target_position = np.asarray(target.getLocVelAcc().loc_WRT_ExprIn(phobosFrame), dtype=float)
    body_x = np.asarray(
        st.frames.FramedVector(np.array([1.0, 0.0, 0.0]), shuttleFrame).ExprIn(phobosFrame),
        dtype=float,
    )
    angular_velocity = np.asarray(shuttle.getAngVelocity().WRT_ExprIn(phobosFrame), dtype=float)
    fuel = float(shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"]))
    damage = float(shuttle.GetParam(st.VarType.double, ["Stats", "Damage"]))
    radius = float(shuttle.GetParam(st.VarType.double, ["Graphics", "VisualRadius_m"]))
    max_accel = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAccel_m_s2"]))
    max_angular = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAngAccel_rad_s2"]))
    mass = float(shuttle.getMass())
    if not np.isfinite(mass) or mass <= 0.0:
        mass = 3388.7
    rows = GetNearestDebrisLocations(80)
    state = update_belief(state, rows, position, now)
    nominal_velocity = cruise_velocity(
        position,
        target_position,
        state.get("cruise_speed_mps", CRUISE_SPEED_MPS),
        ARRIVAL_RADIUS_M,
        max_accel,
    )
    desired_accel = plan(
        state,
        position,
        velocity,
        target_position,
        body_x,
        radius,
        max_accel,
        dt,
        fuel,
        damage,
        mass,
        now,
    )
    accel_world, state["burning"] = govern(
        desired_accel, nominal_velocity, velocity, state["burning"], max_accel
    )
    emergency_accel, emergency_active = emergency_guard(velocity, rows, now, max_accel)
    if emergency_active:
        accel_world = emergency_accel
        state["burning"] = True
    terminal_range = np.linalg.norm(target_position - position)
    if (
        not emergency_active
        and terminal_range - ARRIVAL_RADIUS_M <= TERMINAL_COAST_RANGE_M
        and np.linalg.norm(velocity) >= TERMINAL_COAST_MIN_SPEED_MPS
    ):
        # The score checks the 50 m region without an arrival-speed gate.
        # Coast on the clear terminal path instead of paying for a stop.
        accel_world = np.zeros(3)
        state["burning"] = False
    desired = unit(nominal_velocity)
    alignment = (
        np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0)))
        if np.linalg.norm(desired) > 1e-9
        else 0.0
    )
    gate = "off"
    if state["burning"]:
        gate = "burn"
    elif state.get("attitude_hold", False):
        gate = "hold"
    elif np.linalg.norm(angular_velocity) > ATTITUDE_HOLD_RATE_RAD_S:
        gate = "rate"
    elif alignment > ATTITUDE_HOLD_RAD:
        gate = "alignment"
    attitude_trace = {}
    angular_world = attitude(
        body_x, angular_velocity, desired, max_angular, dt, state, attitude_trace
    )
    if gate == "off":
        angular_world = np.zeros(3)
        attitude_trace["command_rad_s2"] = [0.0, 0.0, 0.0]
        attitude_trace["command_norm_rad_s2"] = 0.0
        attitude_trace["saturated"] = False
    body_accel = st.frames.FramedVector(accel_world, phobosFrame).ExprIn(shuttleFrame)
    body_angular = st.frames.FramedVector(angular_world, phobosFrame).ExprIn(shuttleFrame)
    SetCommand(body_accel, body_angular)

    speed = np.linalg.norm(velocity)
    target_range = np.linalg.norm(target_position - position)
    velocity_error = np.linalg.norm(nominal_velocity - velocity)
    mode = "burn" if state["burning"] else "coast"
    record = telemetry_record(
        now,
        dt,
        mode,
        gate,
        body_x,
        desired,
        accel_world,
        body_accel,
        angular_velocity,
        attitude_trace,
        velocity_error,
        speed,
        target_range,
        len(state["rocks"]),
        fuel,
        damage,
    )
    record["emergency"] = bool(emergency_active)
    raw_ranges = [
        float(np.linalg.norm(np.asarray(row[0], dtype=float)))
        for row in rows
        if np.all(np.isfinite(row))
    ]
    track_ranges = [
        float(np.linalg.norm(np.asarray(rock["position"], dtype=float) - position))
        for rock in state["rocks"]
    ]
    record["nearest_raw_range_m"] = min(raw_ranges) if raw_ranges else None
    record["nearest_track_range_m"] = min(track_ranges) if track_ranges else None
    record["line"] = telemetry_line(record)
    if now - last_status >= 0.5:
        last_status = now
        st.logger_info("MPPI " + json.dumps(record, separators=(",", ":"), allow_nan=False))
    if now - last_display >= 1.0:
        last_display = now
        st.OnScreenLogMessage(record["line"], "MPPI", st.Severity.Info)
    if record["warning"] is not None and now - last_alert >= 5.0:
        last_alert = now
        st.OnScreenAlert(
            f"MPPI warning {record['warning']} " + record["line"],
            "MPPI",
            st.Severity.Info,
        )
