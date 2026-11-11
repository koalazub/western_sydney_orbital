"""Pure functional core for Ali's MPPI controller.

The SDK entrypoint imports this file. The local adapter loads it directly.
Radar rows are anonymous 3x3 blocks: row 0 is the rock centre relative to the
shuttle, row 1 is (surface range, range rate, diameter), and row 2 is the time.
"""

import numpy as np

ARRIVAL_RADIUS_M = 50.0
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
MPPI_SAMPLES, MPPI_STEPS = 48, 30
CRUISE_SPEED_MPS = 7.0
BURN_START_MPS, BURN_STOP_MPS = 0.50, 0.30
ATTITUDE_DEADBAND_RAD = np.radians(1.5)
RATE_DEADBAND_RAD_S = 0.02
CONTROL_SWITCH_WEIGHT = 0.10


def unit(value):
    length = np.linalg.norm(value, axis=-1, keepdims=True)
    return np.divide(value, length, out=np.zeros_like(value), where=length > 1e-9)


def capsule_distance(points, centers, axis, half_length, radius):
    offset = centers[None, :, :] - points[:, None, :]
    along = np.clip(np.einsum("knd,d->kn", offset, axis), -half_length, half_length)
    closest = points[:, None, :] + along[:, :, None] * axis
    return np.linalg.norm(centers[None, :, :] - closest, axis=2) - radius


def cruise_velocity(position, target_position):
    delta = target_position - position
    distance = np.linalg.norm(delta)
    return np.zeros(3) if distance < 1e-9 else CRUISE_SPEED_MPS * delta / distance


def controlled_acceleration(wanted_velocity, current_velocity, max_accel):
    error = wanted_velocity - current_velocity
    magnitude = np.linalg.norm(error)
    if magnitude <= BURN_STOP_MPS:
        return np.zeros(3)
    return min(1.0, max_accel / magnitude) * error


def govern(desired_accel, nominal_velocity, velocity, burning, max_accel):
    error = np.linalg.norm(nominal_velocity - velocity)
    desired_norm = np.linalg.norm(desired_accel)
    if burning:
        if error < BURN_STOP_MPS and desired_norm < BURN_STOP_MPS:
            burning = False
    elif error > BURN_START_MPS or desired_norm > BURN_START_MPS:
        burning = True
    if not burning:
        return np.zeros(3), False
    direction = desired_accel if desired_norm > 1e-9 else nominal_velocity - velocity
    return max_accel * unit(direction), True


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
    fuel_rate,
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
    nominal_velocity = cruise_velocity(position, target_position)
    nominal = controlled_acceleration(nominal_velocity, velocity, max_accel)
    noise = np.random.default_rng().normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3))
    directions = unit(nominal + noise * max_accel)
    magnitudes = np.random.default_rng().random((MPPI_SAMPLES, MPPI_STEPS, 1)) * max_accel
    controls = directions * magnitudes
    positions = np.broadcast_to(position, (MPPI_SAMPLES, 3)).copy()
    ship_velocities = np.broadcast_to(velocity, (MPPI_SAMPLES, 3)).copy()
    risk = np.zeros(MPPI_SAMPLES)
    for step in range(MPPI_STEPS):
        positions += ship_velocities * dt + 0.5 * controls[:, step] * dt * dt
        ship_velocities += controls[:, step] * dt
        if len(rocks):
            future = centers + (step + 1) * dt * rock_velocities
            distance = capsule_distance(positions, future, body_x, radius, radius)
            risk += np.sum(np.maximum(0.0, 3.0 * sigmas - (distance - radii)), axis=1)
    time_to_go = np.maximum(
        0.0, np.linalg.norm(positions - target_position, axis=1) - ARRIVAL_RADIUS_M
    )
    time_to_go = time_to_go / np.maximum(np.linalg.norm(ship_velocities, axis=1), 1e-3)
    fuel_used = fuel + fuel_rate * np.sum(np.linalg.norm(controls, axis=2), axis=1) * dt
    score = 100.0 * (
        120.0 / np.maximum(now + time_to_go, 1e-3)
        - fuel_used / 75.0
        - 2.0 * damage / 209200.0
        + 0.5
    )
    terminal_speed_error = np.linalg.norm(ship_velocities - nominal_velocity, axis=1)
    switch = np.linalg.norm(controls[:, 0] - state["last_control"], axis=1)
    cost = -score + 1000.0 * risk + 0.5 * terminal_speed_error**2 + CONTROL_SWITCH_WEIGHT * switch
    best = int(np.argmin(cost))
    state["last_control"] = controls[best, 0].copy()
    return controls[best, 0]


def attitude(body_x, angular_velocity, desired, max_angular_accel, dt):
    desired = unit(desired)
    if np.linalg.norm(desired) < 1e-9:
        return np.zeros(3)
    cross = np.cross(body_x, desired)
    sine = np.linalg.norm(cross)
    angle = np.arctan2(sine, float(np.clip(body_x @ desired, -1.0, 1.0)))
    speed = np.linalg.norm(angular_velocity)
    if angle < ATTITUDE_DEADBAND_RAD and speed < RATE_DEADBAND_RAD_S:
        return np.zeros(3)
    if angle < ATTITUDE_DEADBAND_RAD:
        result = -unit(angular_velocity) * max_angular_accel
    else:
        axis = cross / sine if sine > 1e-9 else unit(np.cross(body_x, np.array([0.0, 0.0, 1.0])))
        natural_rate = np.sqrt(max_angular_accel / max(angle, 0.02))
        result = axis * (natural_rate**2 * angle) - angular_velocity * (2.0 * natural_rate)
    length = np.linalg.norm(result)
    return result / length * max_angular_accel if length > max_angular_accel else result
