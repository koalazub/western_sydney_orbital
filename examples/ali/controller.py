"""Ali's radar-only MVP for Escape from Phobos.

A single-file SDK entrypoint with a functional control core. Radar rows are
anonymous 3x3 blocks: row 0 is the rock centre relative to the shuttle, row 1
is (surface range, range rate, diameter), and row 2 is the measurement time.
Positions and velocities use the Phobos frame unless a name says otherwise.
Attitude points body +X at the commanded burn with bounded side-thruster
angular acceleration during the initial acceleration phase; attitude is held
after alignment so avoidance dodges do not keep the ship spinning.
"""

# Keep the SDK bootstrap imports in their original order.
# ruff: noqa: E402

#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import datetime
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
topCameraFrame = topCamera.GetBodyFixedFrame()
bottomCamera: st.Entity = this.GetParam(st.VarType.entityRef, "BottomCamera")
bottomCameraFrame = bottomCamera.GetBodyFixedFrame()


def GetNearestDebrisLocations(N: int) -> list[npt.NDArray]:
    allDebrisInfo: list[npt.NDArray] = shuttle.GetParamArray(
        st.VarType.doubleM3x3, ["Radar", "SortedDebrisData"]
    )
    nearest = allDebrisInfo[: min(N, len(allDebrisInfo))]
    return nearest


def PanTiltCamera(camera: st.Entity, yaw_deg: float, pitch_deg: float) -> None:
    if camera == topCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, -50.0, 0.0)
    elif camera == bottomCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, 0.0, 50.0)
    camera.setRotation_DCM(
        st.math.Euler321_to_DCM([np.deg2rad(yaw_deg), np.deg2rad(pitch_deg), 0.0]),
        shuttleFrame,
    )


def SetCommand(accelCommand: npt.NDArray, angAccelCommand: npt.NDArray) -> None:
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accelCommand)
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAngAccel"], angAccelCommand)


#############################################################################################
# DON'T CHANGE ANY OF THE ABOVE. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

# BEGIN ALI PURE CORE

CRUISE_SPEED_MPS, GOAL_RADIUS_M = 17.0, 50.0
DEFAULT_MAX_ACCEL_MPS2 = 0.5
CONTROL_DEADBAND_MPS = 0.15
TRACK_GATE_M, TRACK_MAX_AGE_S = 12.0, 5.0
TRACK_LOOKAHEAD_S, PLAN_ACTIVATION_S = 15.0, 10.0
PLAN_MARGIN_M, UNKNOWN_VELOCITY_MARGIN_M = 2.0, 3.0
EMERGENCY_RANGE_M, EMERGENCY_LOOKAHEAD_S, EMERGENCY_MARGIN_M = 150.0, 6.0, 1.0
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
ATTITUDE_RESPONSE_S = 0.25
ATTITUDE_DEADBAND_RAD = np.radians(2.0)
ATTITUDE_SETTLE_RATE_RAD_S = 0.02


def unit(vector):
    length = np.linalg.norm(vector)
    return vector / length if length > 1e-9 else np.zeros(3)


def lateral_axes(direction):
    helper = np.array([0.0, 0.0, 1.0]) if abs(direction[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    side = unit(np.cross(direction, helper))
    return side, unit(np.cross(direction, side))


def closest_approach(my_velocity, relative_positions, rock_velocities, horizon):
    relative = rock_velocities - my_velocity
    speed_squared = np.einsum("ij,ij->i", relative, relative)
    time = -np.einsum("ij,ij->i", relative_positions, relative) / (speed_squared + 1e-9)
    time = np.clip(time, 0.0, horizon)
    return time, np.linalg.norm(relative_positions + time[:, None] * relative, axis=1)


def goal_velocity(position, velocity, target_position):
    to_target = target_position - position
    distance = np.linalg.norm(to_target)
    if distance < 1e-6:
        return np.zeros(3)
    speed = np.linalg.norm(velocity)
    if speed > CRUISE_SPEED_MPS * 0.9:
        heading = velocity / speed
        along = to_target @ heading
        miss = np.linalg.norm(to_target - along * heading) if along > 0.0 else np.inf
        if miss < GOAL_RADIUS_M * 0.8:
            return velocity
    return CRUISE_SPEED_MPS * to_target / distance


def sighted(rock, position, diameter, measured_at):
    if rock is not None and measured_at <= rock["last_seen"]:
        return rock
    samples = ([] if rock is None else rock["samples"])[-29:] + [
        (float(measured_at), np.asarray(position, float))
    ]
    times = np.array([sample[0] for sample in samples])
    velocity = None
    if len(samples) >= 4 and times[-1] - times[0] >= 1.0:
        positions = np.array([sample[1] for sample in samples])
        velocity = np.polyfit(times - times[0], positions, 1)[0]
    return {
        "position": np.asarray(position, float),
        "velocity": velocity,
        "diameter": float(diameter),
        "max_diameter": max(float(diameter), 0.0 if rock is None else rock["max_diameter"]),
        "last_seen": float(measured_at),
        "samples": samples,
    }


def match_index(rocks, position, diameter, measured_at):
    best_index, best_distance = None, np.inf
    for index, rock in enumerate(rocks):
        if abs(rock["diameter"] - diameter) > max(2.0, 0.4 * rock["diameter"]):
            continue
        predicted = rock["position"]
        if rock["velocity"] is not None:
            predicted = predicted + rock["velocity"] * (measured_at - rock["last_seen"])
        distance = np.linalg.norm(predicted - position)
        if distance < TRACK_GATE_M + 0.5 * rock["diameter"] and distance < best_distance:
            best_index, best_distance = index, distance
    return best_index


def update_tracker(rocks, rows, ship_position, now):
    updated = list(rocks)
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        position = ship_position + row[0]
        diameter, measured_at = float(row[1, 2]), float(row[2, 0])
        index = match_index(updated, position, diameter, measured_at)
        if index is None:
            updated.append(sighted(None, position, diameter, measured_at))
        else:
            updated[index] = sighted(updated[index], position, diameter, measured_at)
    return [rock for rock in updated if now - rock["last_seen"] <= TRACK_MAX_AGE_S]


def predicted_position(rock, time):
    if rock["velocity"] is None:
        return rock["position"]
    return rock["position"] + rock["velocity"] * (time - rock["last_seen"])


def choose_planner(wanted_velocity, rocks, position, now, held_velocity):
    if not rocks:
        return wanted_velocity, None
    relative = np.array([predicted_position(rock, now) - position for rock in rocks])
    velocities = np.array(
        [rock["velocity"] if rock["velocity"] is not None else np.zeros(3) for rock in rocks]
    )
    keep_out = np.array(
        [
            0.55 * rock["max_diameter"]
            + PLAN_MARGIN_M
            + (0.0 if rock["velocity"] is not None else UNKNOWN_VELOCITY_MARGIN_M)
            for rock in rocks
        ]
    )
    time, miss = closest_approach(wanted_velocity, relative, velocities, TRACK_LOOKAHEAD_S)
    margin = miss - keep_out
    if np.min(margin) >= 0.0 or time[np.argmin(margin)] > PLAN_ACTIVATION_S:
        return wanted_velocity, None
    if held_velocity is not None:
        held_margin = closest_approach(held_velocity, relative, velocities, TRACK_LOOKAHEAD_S)[1]
        if np.min(held_margin - keep_out) >= 0.0:
            return held_velocity, held_velocity
    speed = np.linalg.norm(wanted_velocity)
    if speed < 1e-6:
        return wanted_velocity, None
    forward = wanted_velocity / speed
    threat = int(np.argmin(margin))
    lateral = relative[threat] - (relative[threat] @ forward) * forward
    away = -unit(lateral) if np.linalg.norm(lateral) > 1e-6 else lateral_axes(forward)[0]
    for direction in (away, -away):
        for angle in np.radians((2.0, 5.0, 10.0, 20.0)):
            candidate = speed * unit(np.cos(angle) * forward + np.sin(angle) * direction)
            candidate_margin = closest_approach(candidate, relative, velocities, TRACK_LOOKAHEAD_S)[
                1
            ]
            if np.min(candidate_margin - keep_out) >= 0.0:
                return candidate, candidate
    return wanted_velocity, None


def choose_emergency(velocity, rows, now, heading):
    heading = unit(heading)
    relative, velocities, keep_out = [], [], []
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        position = np.asarray(row[0], float)
        line_of_sight = unit(position)
        rock_velocity = line_of_sight * (float(row[1, 1]) + line_of_sight @ velocity)
        position = position + (now - float(row[2, 0])) * (rock_velocity - velocity)
        if np.linalg.norm(position) > EMERGENCY_RANGE_M or position @ heading <= 0.0:
            continue
        relative.append(position)
        velocities.append(rock_velocity)
        keep_out.append(0.55 * float(row[1, 2]) + EMERGENCY_MARGIN_M)
    if not relative:
        return velocity, False
    relative, velocities, keep_out = map(np.array, (relative, velocities, keep_out))
    margin = closest_approach(velocity, relative, velocities, EMERGENCY_LOOKAHEAD_S)[1] - keep_out
    if np.min(margin) >= 0.0:
        return velocity, False
    threat = int(np.argmin(margin))
    lateral = relative[threat] - (relative[threat] @ heading) * heading
    away = -unit(lateral) if np.linalg.norm(lateral) > 1e-6 else lateral_axes(heading)[0]
    escape = max(2.0, min(4.0, 0.25 * np.linalg.norm(velocity)))
    options = (velocity + escape * away, velocity - escape * away)
    scores = [
        np.min(closest_approach(option, relative, velocities, EMERGENCY_LOOKAHEAD_S)[1] - keep_out)
        for option in options
    ]
    best = int(np.argmax(scores))
    return (options[best], True) if scores[best] > np.min(margin) else (velocity, False)


def bounded_vector(vector, maximum):
    length = np.linalg.norm(vector)
    if length <= maximum:
        return vector
    return vector / length * (maximum - 4.0 * np.spacing(maximum))


def controlled_acceleration(wanted_velocity, current_velocity, max_accel=DEFAULT_MAX_ACCEL_MPS2):
    error = wanted_velocity - current_velocity
    magnitude = np.linalg.norm(error)
    if magnitude <= CONTROL_DEADBAND_MPS:
        return np.zeros(3)
    return min(1.0, max_accel / magnitude) * error


def attitude_acceleration(
    body_x_world,
    angular_velocity_world,
    desired_direction_world,
    max_angular_accel=MAX_ANGULAR_ACCEL_RAD_S2,
):
    body_x = unit(body_x_world)
    desired = unit(desired_direction_world)
    if np.linalg.norm(desired) < 1e-9:
        return np.zeros(3), 0.0
    cross = np.cross(body_x, desired)
    sine = np.linalg.norm(cross)
    cosine = float(np.clip(body_x @ desired, -1.0, 1.0))
    angle = float(np.arctan2(sine, cosine))
    if angle <= ATTITUDE_DEADBAND_RAD:
        desired_rate = np.zeros(3)
    else:
        rate = min(angle / ATTITUDE_RESPONSE_S, np.sqrt(2.0 * max_angular_accel * angle))
        axis = cross / sine if sine > 1e-9 else lateral_axes(body_x)[0]
        desired_rate = axis * rate
    rate_error = desired_rate - np.asarray(angular_velocity_world, float)
    return bounded_vector(rate_error / ATTITUDE_RESPONSE_S, max_angular_accel), angle


def initial_state():
    return {"rocks": [], "held_velocity": None, "pivot_done": False}


def decide(state, position, velocity, target_position, rows, now):
    wanted = goal_velocity(position, velocity, target_position)
    planned, held_velocity = choose_planner(
        wanted, state["rocks"], position, now, state["held_velocity"]
    )
    safe_velocity, emergency_active = choose_emergency(planned, rows, now, unit(planned))
    return (
        {
            "rocks": state["rocks"],
            "held_velocity": held_velocity,
            "pivot_done": state["pivot_done"],
        },
        safe_velocity,
        emergency_active,
    )


def step(state, position, velocity, target_position, rows, now):
    rocks = update_tracker(state["rocks"], rows, position, now)
    return decide({**state, "rocks": rocks}, position, velocity, target_position, rows, now)


# END ALI PURE CORE

MAX_ACCEL_MPS2 = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAccel_m_s2"]))
MAX_ANGULAR_ACCEL = float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAngAccel_rad_s2"]))


def log_telemetry(
    handle, last_status, time_s, position, velocity, fuel, damage, nearest, rocks, emergency
):
    speed = np.linalg.norm(velocity)
    values = (time_s, *position, *velocity, speed, fuel, damage, nearest, rocks, int(emergency))
    handle.write(",".join(f"{value:.6g}" for value in values) + "\n")
    if time_s - last_status[0] >= 1.0:
        last_status[0] = time_s
        message = (
            f"t={time_s:6.1f}s speed={speed:5.2f}m/s fuel={fuel:7.2f}kg "
            f"damage={damage:8.1f}J nearest={nearest:6.1f}m rocks={rocks} emergency={emergency}"
        )
        st.OnScreenLogMessage(message, "Status", st.Severity.Info)


t0_sim: st.timestamp = st.SimGlobals.GetSimEntity().GetParam(
    st.VarType.timestamp, ["Time", "StartDateTAI"]
)
t0: datetime.datetime = t0_sim.as_datetime()

state = initial_state()
telemetry = open(  # noqa: SIM115
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "controller_telemetry.csv"),
    "w",
    buffering=1,
)
telemetry.write(
    "time_s,x_m,y_m,z_m,vx_mps,vy_mps,vz_mps,speed_mps,fuel_kg,damage_j,nearest_m,rocks,emergency\n"
)
last_status = [-np.inf]

while True:
    time.sleep(1.0 / this.GetParam(st.VarType.double, "LoopFreqHz"))
    t_from_start = (st.SimGlobals.SimClock.GetTimeNow().as_datetime() - t0).total_seconds()

    state_now = shuttle.getLocVelAcc()
    position = np.asarray(state_now.loc_WRT_ExprIn(phobosFrame), float)
    velocity = np.asarray(state_now.vel_WRT_ExprIn(phobosFrame), float)
    target_position = np.asarray(target.getLocVelAcc().loc_WRT_ExprIn(phobosFrame), float)
    fuel = float(shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"]))
    damage = float(shuttle.GetParam(st.VarType.double, ["Stats", "Damage"]))
    radar_items = GetNearestDebrisLocations(50)

    state, safe_velocity, emergency_active = step(
        state, position, velocity, target_position, radar_items, t_from_start
    )
    acceleration = controlled_acceleration(safe_velocity, velocity, MAX_ACCEL_MPS2)
    body_x_world = np.asarray(
        st.frames.FramedVector(np.array([1.0, 0.0, 0.0]), shuttleFrame).ExprIn(phobosFrame),
        dtype=float,
    )
    angular_velocity_world = np.asarray(
        shuttle.getAngVelocity().WRT_ExprIn(phobosFrame), dtype=float
    )
    pivot_active = False
    if not state["pivot_done"] and np.linalg.norm(acceleration) > 0.0:
        desired_direction = unit(acceleration)
        angular_accel_world, alignment_angle = attitude_acceleration(
            body_x_world, angular_velocity_world, desired_direction, MAX_ANGULAR_ACCEL
        )
        still_rotating = np.linalg.norm(angular_velocity_world) > ATTITUDE_SETTLE_RATE_RAD_S
        pivot_active = alignment_angle > ATTITUDE_DEADBAND_RAD or still_rotating
        if not pivot_active:
            state["pivot_done"] = True
            angular_accel_world = np.zeros(3)
    else:
        angular_accel_world, alignment_angle = np.zeros(3), 0.0
    body_acceleration = st.frames.FramedVector(acceleration, phobosFrame).ExprIn(shuttleFrame)
    body_angular_accel = st.frames.FramedVector(angular_accel_world, phobosFrame).ExprIn(
        shuttleFrame
    )
    SetCommand(body_acceleration, body_angular_accel)

    nearest = min(
        (float(item[1, 0]) for item in radar_items if np.all(np.isfinite(item))),
        default=float("nan"),
    )
    log_telemetry(
        telemetry,
        last_status,
        t_from_start,
        position,
        velocity,
        fuel,
        damage,
        nearest,
        len(state["rocks"]),
        emergency_active,
    )
