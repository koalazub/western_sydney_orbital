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
    points = np.asarray(points, dtype=float)
    axis = np.asarray(axis, dtype=float)
    if axis.ndim == 1:
        axis = np.broadcast_to(axis, (points.shape[0], 3))
    offset = centers[None, :, :] - points[:, None, :]
    along = np.clip(np.einsum("knd,kd->kn", offset, axis), -half_length, half_length)
    closest = points[:, None, :] + along[:, :, None] * axis[:, None, :]
    return np.linalg.norm(centers[None, :, :] - closest, axis=2) - radius


def rotation_from_body_x(body_x):
    x = unit(np.asarray(body_x, dtype=float))
    helper = np.array([0.0, 0.0, 1.0]) if abs(float(x[2])) < 0.9 else np.array([0.0, 1.0, 0.0])
    y = unit(np.cross(helper, x))
    z = np.cross(x, y)
    return np.column_stack((x, y, z))


def skew(value):
    value = np.asarray(value, dtype=float)
    result = np.zeros(value.shape[:-1] + (3, 3))
    result[..., 0, 1] = -value[..., 2]
    result[..., 0, 2] = value[..., 1]
    result[..., 1, 0] = value[..., 2]
    result[..., 1, 2] = -value[..., 0]
    result[..., 2, 0] = -value[..., 1]
    result[..., 2, 1] = value[..., 0]
    return result


def advanced_rotation(rotation, angular_velocity, dt):
    updated = rotation + np.einsum("nij,njk->nik", skew(angular_velocity * dt), rotation)
    x = updated[..., 0]
    x = x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)
    y = updated[..., 1]
    y = y - np.sum(y * x, axis=-1, keepdims=True) * x
    y = y / np.maximum(np.linalg.norm(y, axis=-1, keepdims=True), 1e-12)
    z = np.cross(x, y)
    return np.stack((x, y, z), axis=-1)


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
    speed = min(
        max(0.0, float(cruise_speed_mps)),
        np.sqrt(
            2.0
            * max(1e-6, float(brake_accel_mps2))
            * max(distance - max(0.0, float(arrival_radius_m)), 0.0)
        ),
    )
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


def plan_coupled(
    state,
    position,
    velocity,
    target_position,
    body_x,
    rotation,
    angular_velocity,
    radius,
    max_accel,
    max_angular_accel,
    dt,
    fuel,
    damage,
    ship_mass_kg,
    now,
    disturbance_world=None,
):
    """Score translation and attitude together on the actual body-frame jets.

    The body-frame command costs the L1 nozzle force, not just its vector
    magnitude. The rollout therefore sees why a main-axis burn is cheaper than
    a lateral one, and can trade slew time against that fuel saving.
    """
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
    rotation = np.asarray(rotation, dtype=float)
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        rotation = rotation_from_body_x(body_x)
    body_x = rotation[:, 0]
    angular_velocity = np.asarray(angular_velocity, dtype=float)
    if angular_velocity.shape != (3,) or not np.all(np.isfinite(angular_velocity)):
        angular_velocity = np.zeros(3)
    last_body = np.asarray(state.get("last_accel_body", np.zeros(3)), dtype=float)
    last_alpha = np.asarray(state.get("last_alpha_body", np.zeros(3)), dtype=float)
    last_fuel = state.get("last_fuel_sample")
    last_time = state.get("last_fuel_time")
    exhaust = float(state.get("exhaust_velocity_mps", DEFAULT_EXHAUST_VELOCITY_MPS))
    if last_fuel is not None and last_time is not None:
        elapsed = now - last_time
        burned = fuel - last_fuel
        l1 = float(np.sum(np.abs(last_body)))
        if elapsed > 1e-6 and burned > 1e-9 and l1 > 1e-9 and np.isfinite(burned):
            observed = float(ship_mass_kg) * l1 * elapsed / burned
            if np.isfinite(observed) and 100.0 < observed < 100000.0:
                exhaust = 0.8 * exhaust + 0.2 * observed
    if not np.isfinite(exhaust) or exhaust <= 0.0:
        exhaust = DEFAULT_EXHAUST_VELOCITY_MPS
    state["exhaust_velocity_mps"] = float(exhaust)
    state["last_fuel_sample"], state["last_fuel_time"] = float(fuel), float(now)
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
    nominal_velocity = cruise_velocity(
        position,
        target_position,
        cruise_speed,
        values["arrival_radius_m"],
        max_accel,
    )
    desired = unit(nominal_velocity)
    attitude_trace = {}
    baseline_world = attitude(
        body_x,
        angular_velocity,
        desired,
        max(1e-6, float(max_angular_accel)),
        max(1e-3, float(dt)),
        state,
        attitude_trace,
    )
    baseline_body = rotation.T @ baseline_world
    state["attitude_trace"] = attitude_trace
    state["attitude_desired_world"] = desired
    if disturbance_world is None:
        disturbance = state.get("disturbance_world")
    else:
        disturbance = disturbance_world
    disturbance = np.asarray(disturbance, dtype=float) if disturbance is not None else np.zeros(3)
    if disturbance.shape != (3,) or not np.all(np.isfinite(disturbance)):
        disturbance = np.zeros(3)
    nominal_world = max_accel * unit(nominal_velocity - velocity)
    nominal_body = rotation.T @ nominal_world
    rng = state.setdefault("rng", np.random.default_rng(12345))
    translation_noise = rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3)) * max_accel
    translation = nominal_body[None, None, :] + translation_noise
    translation_norm = np.linalg.norm(translation, axis=-1, keepdims=True)
    translation = translation / np.maximum(1.0, translation_norm / max(1e-6, max_accel))
    angular_noise = rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3)) * max(1e-6, max_angular_accel)
    angular = baseline_body[None, None, :] + angular_noise
    angular_norm = np.linalg.norm(angular, axis=-1, keepdims=True)
    angular = angular / np.maximum(1.0, angular_norm / max(1e-6, max_angular_accel))
    remaining_time = now + float(terminal_time(remaining, speed, cruise_speed, max_accel))
    horizon = float(np.clip(0.1 * remaining_time, 8.0, 30.0))
    plan_dt = float(np.clip(horizon / MPPI_STEPS, max(dt, 1e-3), MAX_PLAN_DT_S))
    rotations = np.broadcast_to(rotation, (MPPI_SAMPLES, 3, 3)).copy()
    omegas = np.broadcast_to(angular_velocity, (MPPI_SAMPLES, 3)).copy()
    positions = np.broadcast_to(position, (MPPI_SAMPLES, 3)).copy()
    ship_velocities = np.broadcast_to(velocity, (MPPI_SAMPLES, 3)).copy()
    risk = np.zeros(MPPI_SAMPLES)
    fuel_l1 = np.zeros(MPPI_SAMPLES)
    for step in range(MPPI_STEPS):
        accel_world = np.einsum("nij,nj->ni", rotations, translation[:, step])
        acceleration = accel_world + disturbance
        positions += ship_velocities * plan_dt + 0.5 * acceleration * plan_dt * plan_dt
        ship_velocities += acceleration * plan_dt
        alpha_world = np.einsum("nij,nj->ni", rotations, angular[:, step])
        omegas += alpha_world * plan_dt
        rotations = advanced_rotation(rotations, omegas, plan_dt)
        if len(rocks):
            future = centers + (step + 1) * plan_dt * rock_velocities
            axis = rotations[:, :, 0]
            distance = capsule_distance(positions, future, axis, radius, radius)
            margin = (
                SAFETY_MARGIN_M
                + REACTION_TIME_S * np.linalg.norm(ship_velocities, axis=1)[:, None]
                + 2.0 * sigmas
            )
            risk += np.sum(np.maximum(0.0, margin - (distance - radii)), axis=1)
        fuel_l1 += np.sum(np.abs(translation[:, step]), axis=1) * plan_dt
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
    fuel_used = fuel + float(ship_mass_kg) * fuel_l1 / exhaust
    score = values["points_scale"] * (
        values["nominal_time_s"] / np.maximum(now + plan_horizon + time_to_go, 1e-3)
        - fuel_used / values["nominal_fuel_kg"]
        - values["damage_multiplier"] * damage / values["impact_threshold_j"]
        + values["score_offset"]
    )
    switch = np.linalg.norm(translation[:, 0] - last_body, axis=1) + 0.5 * np.linalg.norm(
        angular[:, 0] - last_alpha, axis=1
    )
    risk_weight = values["points_scale"] * values["damage_multiplier"]
    cost = -score + risk_weight * risk + CONTROL_SWITCH_WEIGHT * switch
    best = int(np.argmin(cost))
    accel_body = translation[best, 0].copy()
    angular_body = angular[best, 0].copy()
    state["last_accel_body"] = accel_body
    state["last_alpha_body"] = angular_body
    state["last_control"] = rotation @ accel_body
    state["plan_diagnostics"] = {
        "cruise_speed_mps": float(cruise_speed),
        "plan_horizon_s": float(plan_horizon),
        "plan_dt_s": float(plan_dt),
        "exhaust_velocity_mps": float(exhaust),
        "disturbance_world": [float(value) for value in disturbance],
        "attitude_hold": bool(attitude_trace.get("hold", False)),
        "accel_body": [float(value) for value in accel_body],
        "angular_body": [float(value) for value in angular_body],
    }
    return accel_body, angular_body


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
    """Compatibility wrapper for translation-only callers and numeric checks."""
    rotation = rotation_from_body_x(body_x)
    angular_velocity = np.asarray(state.get("angular_velocity_world", np.zeros(3)), dtype=float)
    accel_body, _ = plan_coupled(
        state,
        position,
        velocity,
        target_position,
        body_x,
        rotation,
        angular_velocity,
        radius,
        max_accel,
        MAX_ANGULAR_ACCEL_RAD_S2,
        dt,
        fuel,
        damage,
        ship_mass_kg,
        now,
    )
    world = rotation @ accel_body
    state["last_control"] = world
    return world


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
    "last_velocity": None,
    "last_accel_world": None,
    "last_accel_time": None,
    "angular_velocity_world": np.zeros(3),
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
    body_y = np.asarray(
        st.frames.FramedVector(np.array([0.0, 1.0, 0.0]), shuttleFrame).ExprIn(phobosFrame),
        dtype=float,
    )
    body_z = np.asarray(
        st.frames.FramedVector(np.array([0.0, 0.0, 1.0]), shuttleFrame).ExprIn(phobosFrame),
        dtype=float,
    )
    rotation = np.column_stack((body_x, body_y, body_z))
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
    if state.get("last_velocity") is not None and state.get("last_accel_time") is not None:
        elapsed = now - state["last_accel_time"]
        applied = state.get("last_accel_world")
        if elapsed > 1e-6 and applied is not None:
            observed = (velocity - state["last_velocity"]) / elapsed
            estimate = observed - np.asarray(applied, dtype=float)
            if np.all(np.isfinite(estimate)):
                estimate = np.clip(estimate, -max_accel, max_accel)
                if np.linalg.norm(estimate) < 0.02:
                    estimate = np.zeros(3)
                prior = state.get("disturbance_world")
                state["disturbance_world"] = (
                    estimate
                    if prior is None
                    else 0.95 * np.asarray(prior, dtype=float) + 0.05 * estimate
                )
    state["last_velocity"] = velocity.copy()
    state["last_accel_time"] = now
    state["angular_velocity_world"] = angular_velocity
    accel_body, angular_body = plan_coupled(
        state,
        position,
        velocity,
        target_position,
        body_x,
        rotation,
        angular_velocity,
        radius,
        max_accel,
        max_angular,
        dt,
        fuel,
        damage,
        mass,
        now,
        state.get("disturbance_world"),
    )
    nominal_velocity = cruise_velocity(
        position,
        target_position,
        state.get("cruise_speed_mps", CRUISE_SPEED_MPS),
        ARRIVAL_RADIUS_M,
        max_accel,
    )
    governed_world, burning = govern(
        rotation @ accel_body,
        nominal_velocity,
        velocity,
        state["burning"],
        max_accel,
    )
    accel_body = rotation.T @ governed_world
    state["burning"] = burning
    emergency_accel, emergency_active = emergency_guard(velocity, rows, now, max_accel)
    if emergency_active:
        accel_body = rotation.T @ emergency_accel
        state["burning"] = True
    accel_world = rotation @ accel_body
    state["last_accel_world"] = accel_world.copy()
    state["last_accel_body"] = accel_body.copy()
    desired = state.get("attitude_desired_world", unit(nominal_velocity))
    attitude_trace = dict(state.get("attitude_trace", {}))
    attitude_trace["applied_angular_body_rad_s2"] = [float(value) for value in angular_body]
    gate = "burn" if state["burning"] else "hold" if attitude_trace.get("hold", False) else "off"
    SetCommand(accel_body, angular_body)

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
        accel_body,
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
