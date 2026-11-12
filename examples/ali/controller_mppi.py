"""SDK entrypoint for Ali's MPPI controller.

This is a single-file STP submission. The marked core is duplicated in
`mppi_core.py` for analysis and numeric tests; keep the two copies in sync.
"""

# ruff: noqa: E402

#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import os
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
BURN_START_MPS, BURN_STOP_MPS = 0.50, 0.30
ATTITUDE_NATURAL_RATE_RAD_S = 0.60
ATTITUDE_DEADBAND_RAD = np.radians(2.5)
RATE_DEADBAND_RAD_S = 0.03
CONTROL_SWITCH_WEIGHT = 0.50
SAFETY_MARGIN_M = 2.0
REACTION_TIME_S = 0.5
RISK_WEIGHT = 200.0
MIN_TIME_TO_GO_SPEED_MPS = 1.0


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


def target_direction(position, target_position):
    return unit(target_position - position)


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
    nominal = max_accel * target_direction(position, target_position)
    rng = state.setdefault("rng", np.random.default_rng(12345))
    noise = rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3))
    directions = unit(nominal + noise * max_accel)
    magnitudes = rng.random((MPPI_SAMPLES, MPPI_STEPS, 1)) * max_accel
    controls = directions * magnitudes
    plan_dt = max(dt, PLAN_DT_S)
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
    time_to_go = np.maximum(
        0.0, np.linalg.norm(positions - target_position, axis=1) - ARRIVAL_RADIUS_M
    )
    time_to_go = time_to_go / np.maximum(
        np.linalg.norm(ship_velocities, axis=1), MIN_TIME_TO_GO_SPEED_MPS
    )
    plan_horizon = MPPI_STEPS * plan_dt
    delta_v = np.sum(np.linalg.norm(controls, axis=2), axis=1) * plan_dt
    fuel_used = fuel + ship_mass_kg * delta_v / (80.0 * 9.8)
    score = 100.0 * (
        120.0 / np.maximum(now + plan_horizon + time_to_go, 1e-3)
        - fuel_used / 75.0
        - 2.0 * damage / 209200.0
        + 0.5
    )
    switch = np.linalg.norm(controls[:, 0] - state["last_control"], axis=1)
    cost = -score + RISK_WEIGHT * risk + CONTROL_SWITCH_WEIGHT * switch
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
        result = -angular_velocity * (2.0 * ATTITUDE_NATURAL_RATE_RAD_S)
    else:
        axis = cross / sine if sine > 1e-9 else unit(np.cross(body_x, np.array([0.0, 0.0, 1.0])))
        result = axis * (ATTITUDE_NATURAL_RATE_RAD_S**2 * angle) - angular_velocity * (
            2.0 * ATTITUDE_NATURAL_RATE_RAD_S
        )
    length = np.linalg.norm(result)
    return result / length * max_angular_accel if length > max_angular_accel else result


# END ALI MPPI CORE

t0 = (
    st.SimGlobals.GetSimEntity()
    .GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"])
    .as_datetime()
)
state = {
    "rocks": [],
    "burning": False,
    "last_control": np.zeros(3),
    "rng": np.random.default_rng(12345),
}
last_time, last_status = None, -1.0
log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_mppi.csv")
log_file = open(log_path, "w", buffering=1)  # noqa: SIM115
log_file.write(
    "time_s,x,y,z,vx,vy,vz,speed,target_range,fuel,damage,"
    "accel_norm,accel_x,accel_y,accel_z,ang_norm,ang_x,ang_y,ang_z,"
    "mode,velocity_error,alignment,rocks\n"
)

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
    nominal_velocity = cruise_velocity(position, target_position)
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
    desired = unit(nominal_velocity)
    alignment = (
        np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0)))
        if np.linalg.norm(desired) > 1e-9
        else 0.0
    )
    angular_world = (
        attitude(body_x, angular_velocity, desired, max_angular, dt)
        if state["burning"]
        or np.linalg.norm(angular_velocity) > RATE_DEADBAND_RAD_S
        or alignment > ATTITUDE_DEADBAND_RAD
        else np.zeros(3)
    )
    body_accel = st.frames.FramedVector(accel_world, phobosFrame).ExprIn(shuttleFrame)
    body_angular = st.frames.FramedVector(angular_world, phobosFrame).ExprIn(shuttleFrame)
    SetCommand(body_accel, body_angular)

    speed = np.linalg.norm(velocity)
    target_range = np.linalg.norm(target_position - position)
    velocity_error = np.linalg.norm(nominal_velocity - velocity)
    mode = "burn" if state["burning"] else "coast"
    accel_norm = np.linalg.norm(accel_world)
    angular_norm = np.linalg.norm(angular_world)
    log_file.write(
        f"{now:.3f},"
        + ",".join(
            f"{value:.5f}" for value in (*position, *velocity, speed, target_range, fuel, damage)
        )
        + f",{accel_norm:.5f},"
        + ",".join(f"{value:.5f}" for value in (*accel_world, angular_norm, *angular_world))
        + f",{mode},{velocity_error:.4f},{alignment:.4f},{len(state['rocks'])}\n"
    )
    if now - last_status >= 1.0:
        last_status = now
        message = (
            f"{mode} t={now:5.1f}s v={speed:5.2f} err={velocity_error:4.2f} "
            f"fuel={fuel:5.1f} dmg={damage:6.0f} range={target_range:6.0f} "
            f"a={accel_norm:4.2f}"
        )
        st.logger_info(message)
        st.OnScreenLogMessage(message, "MPPI", st.Severity.Info)
        st.OnScreenAlert(message, "MPPI", st.Severity.Info)
