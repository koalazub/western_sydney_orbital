"""SDK entrypoint for Ali's MPPI controller.

This is a single-file STP submission. The marked core below is the only copy;
the local adapter imports the same marked region for tests and practice runs.
"""

# ruff: noqa: E402, E501, E731

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
# fmt: off
ARRIVAL_RADIUS_M = 50.0
MPPI_SAMPLES, MPPI_STEPS = 48, 40
CRUISE_SPEED_MPS, MIN_CRUISE_SPEED_MPS = 7.0, 1.0
TERMINAL_COAST_RANGE_M, TERMINAL_COAST_MIN_SPEED_MPS = 200.0, 4.0
ATTITUDE_HOLD_RAD, ATTITUDE_HOLD_RATE_RAD_S = np.radians(1.0), 0.01
DEFAULT_SCORE = dict(nominal_time_s=120.0, nominal_fuel_kg=75.0, impact_threshold_j=209200.0, points_scale=100.0, damage_multiplier=2.0, score_offset=0.5)
DEFAULT_POLICY = dict(cruise_fraction=0.5, cruise_speed_mps=None, main_engine_gate_rad=0.0, coast_range_m=200.0, coast_speed_mps=4.0, emergency_range_m=180.0, emergency_lookahead_s=7.0, emergency_margin_m=6.0, emergency_escape_mps=3.5, plan_steering=False, plan_risk_threshold=0.01, risk_weight=200.0, switch_weight=0.5, attitude_brake_scale=0.65)

def unit(value):
    length = np.linalg.norm(value, axis=-1, keepdims=True)
    return np.divide(value, length, out=np.zeros_like(value), where=length > 1e-9)


def update_tracks(state, rows, position, now):
    rocks = state.setdefault("rocks", [])
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        measured = position + row[0]
        radius, at = 0.5 * float(row[1, 2]), float(row[2, 0])
        gaps = [np.linalg.norm(rock["p"] + rock["v"] * (at - rock["t"]) - measured) for rock in rocks]
        index = int(np.argmin(gaps)) if gaps else -1
        if index < 0 or gaps[index] >= max(15.0, 2.0 * rocks[index]["s"]):
            rocks.append({"p": measured.copy(), "v": np.zeros(3), "r": radius, "t": at, "s": 0.5, "q": [(at, measured.copy())]})
        elif at > rocks[index]["t"]:
            rock = rocks[index]
            rock.update(p=measured.copy(), r=max(rock["r"], radius), t=at)
            rock["q"] = (rock["q"] + [(at, measured.copy())])[-10:]
            if len(rock["q"]) >= 4 and rock["q"][-1][0] - rock["q"][0][0] >= 1.0:
                times = np.array([sample[0] for sample in rock["q"]])
                points = np.array([sample[1] for sample in rock["q"]])
                design = np.column_stack((np.ones(len(times)), times - times[0]))
                coef, *_ = np.linalg.lstsq(design, points, rcond=None)
                rock["p"], rock["v"] = coef
                rock["s"] = float(np.sqrt(np.sum((points - design @ coef) ** 2) / max(1, len(times) - 2)))
    state["rocks"] = sorted((rock for rock in rocks if now - rock["t"] <= 5.0), key=lambda rock: np.linalg.norm(rock["p"] - position))[:160]


def cruise_velocity(position, target_position, cruise_speed_mps=CRUISE_SPEED_MPS, *_):
    delta = target_position - position
    distance = np.linalg.norm(delta)
    return np.zeros(3) if distance < 1e-9 else max(0.0, float(cruise_speed_mps)) * delta / distance


def govern(desired_accel, nominal_velocity, velocity, burning, max_accel):
    error = nominal_velocity - velocity
    forward = np.linalg.norm(desired_accel) > 0.5 * max_accel and desired_accel @ unit(nominal_velocity) > 0.0
    burning = (burning and (np.linalg.norm(error) >= 0.3 or forward)) or np.linalg.norm(error) > 0.5 or forward
    return (max_accel * unit(desired_accel if forward else error), True) if burning else (np.zeros(3), False)


def emergency_guard(velocity, rows, now, max_accel, policy=None):
    policy = {**DEFAULT_POLICY, **(policy or {})}
    velocity = np.asarray(velocity, float)
    items = []
    for row in rows:
        if np.all(np.isfinite(row)) and row[1, 2] > 0.0:
            los = unit(row[0])
            rock_velocity = los * (float(row[1, 1]) + los @ velocity)
            position = row[0] + (now - float(row[2, 0])) * (rock_velocity - velocity)
            if np.linalg.norm(position) <= policy["emergency_range_m"]:
                items.append((position, rock_velocity, 0.55 * float(row[1, 2]) + policy["emergency_margin_m"]))
    if not items:
        return np.zeros(3), False
    relative, rock_velocity, keep_out = (np.array(values) for values in zip(*items, strict=True))

    def miss(option):
        relative_velocity = rock_velocity - option
        time = np.clip(-np.einsum("ij,ij->i", relative, relative_velocity) / (np.einsum("ij,ij->i", relative_velocity, relative_velocity) + 1e-9), 0.0, policy["emergency_lookahead_s"])
        return np.linalg.norm(relative + time[:, None] * relative_velocity, axis=1) - keep_out

    current = miss(velocity)
    if current.min() >= 0.0:
        return np.zeros(3), False
    threat = int(np.argmin(current))
    heading = unit(velocity) if np.linalg.norm(velocity) > 1e-6 else np.array([1.0, 0.0, 0.0])
    lateral = relative[threat] - (relative[threat] @ heading) * heading
    side = unit(lateral) if np.linalg.norm(lateral) > 1e-6 else unit(np.cross(heading, [0.0, 0.0, 1.0] if abs(heading[2]) < 0.9 else [1.0, 0.0, 0.0]))
    escape = policy["emergency_escape_mps"]
    options = [velocity + escape * unit(-relative[threat]), velocity + escape * side, velocity - escape * side, velocity]
    scores = [float(miss(option).min()) for option in options]
    best = int(np.argmax(scores))
    if scores[best] <= float(current.min()):
        return np.zeros(3), False
    return max_accel * unit(options[best] - velocity), True


def plan(state, position, velocity, target_position, body_x, radius, max_accel, dt, fuel, damage, ship_mass_kg, now, rows):
    policy = {**DEFAULT_POLICY, **state.setdefault("policy", {})}
    radius_target = float(policy.get("arrival_radius_m", ARRIVAL_RADIUS_M))
    remaining = max(0.0, float(np.linalg.norm(target_position - position)) - radius_target)
    budget = policy.get("time_budget_s")
    budget = 2.5 * DEFAULT_SCORE["nominal_time_s"] if budget is None else float(budget)
    fixed_speed = policy.get("cruise_speed_mps")
    reference = state.setdefault("cruise_reference_mps", float(fixed_speed) if fixed_speed else remaining / max(1.0, policy["cruise_fraction"] * budget))
    cruise_speed = min(max(MIN_CRUISE_SPEED_MPS, float(reference)), float(np.sqrt(2.0 * max(1e-6, max_accel) * remaining)))
    state["cruise_speed_mps"], state["fuel_rate_kg_s"] = cruise_speed, max(1e-6, float(ship_mass_kg) * float(max_accel) / (80.0 * 9.8))
    nominal = max_accel * unit(cruise_velocity(position, target_position, cruise_speed) - velocity)
    rocks = state.get("rocks", [])[:30]
    rng = state.setdefault("rng", np.random.default_rng(12345))
    controls = unit(nominal + rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3)) * max_accel) * (rng.random((MPPI_SAMPLES, MPPI_STEPS, 1)) * max_accel)
    plan_dt = float(np.clip(float(np.clip(0.1 * (now + remaining / max(cruise_speed, 1.0)), 8.0, 30.0)) / MPPI_STEPS, max(dt, 1e-3), 0.5))
    positions, ship_velocities = (np.broadcast_to(value, (MPPI_SAMPLES, 3)).copy() for value in (position, velocity))
    risk = np.zeros(MPPI_SAMPLES)
    if rocks:
        centers, rock_velocities, radii, sigmas = (np.array([rock[name] for rock in rocks]) for name in ("p", "v", "r", "s"))
    for step in range(MPPI_STEPS):
        positions += ship_velocities * plan_dt + 0.5 * controls[:, step] * plan_dt**2
        ship_velocities += controls[:, step] * plan_dt
        if rocks:
            future = centers + (step + 1) * plan_dt * rock_velocities
            offset = future[None, :, :] - positions[:, None, :]
            along = np.clip(np.einsum("knd,d->kn", offset, body_x), -radius, radius)
            distance = np.linalg.norm(future[None, :, :] - (positions[:, None, :] + along[:, :, None] * body_x), axis=2) - radius
            risk += np.maximum(0.0, 3.0 + 0.5 * np.linalg.norm(ship_velocities, axis=1)[:, None] - (distance - radii)).sum(axis=1)
    time_to_go = np.maximum(0.0, np.linalg.norm(positions - target_position, axis=1) - radius_target) / np.maximum(np.linalg.norm(ship_velocities, axis=1), 1.0)
    burn_time = np.linalg.norm(controls, axis=2).sum(axis=1) / max(1e-6, max_accel) * plan_dt
    points = DEFAULT_SCORE["points_scale"] * (DEFAULT_SCORE["nominal_time_s"] / np.maximum(now + MPPI_STEPS * plan_dt + time_to_go, 1e-3) - (fuel + state["fuel_rate_kg_s"] * burn_time) / DEFAULT_SCORE["nominal_fuel_kg"] - DEFAULT_SCORE["damage_multiplier"] * damage / DEFAULT_SCORE["impact_threshold_j"] + DEFAULT_SCORE["score_offset"])
    cost = -points + policy["risk_weight"] * risk + policy["switch_weight"] * np.linalg.norm(controls[:, 0] - state["last_control"], axis=1)
    best = int(np.argmin(cost))
    state["last_control"], state["risk"] = controls[best, 0].copy(), float(risk[best])
    return controls[best, 0]


def attitude(body_x, angular_velocity, desired, max_angular_accel, dt, state, diagnostics=None):
    policy = {**DEFAULT_POLICY, **state.get("policy", {})}
    trace = diagnostics if diagnostics is not None else {}
    trace.update(angle_rad=0.0, angle_deg=0.0, rate_rad_s=0.0, desired_rate_rad_s=0.0, rate_error_rad_s=0.0, command_rad_s2=[0.0] * 3, command_norm_rad_s2=0.0, saturated=False, hold=False, released=False)
    desired = unit(desired)
    cross = np.cross(body_x, desired)
    sine = np.linalg.norm(cross)
    angle = np.arctan2(sine, float(np.clip(body_x @ desired, -1.0, 1.0)))
    speed = np.linalg.norm(angular_velocity)
    held = state.get("attitude_hold", False)
    hold = angle <= (np.radians(2.0) if held else ATTITUDE_HOLD_RAD) and speed <= (0.02 if held else ATTITUDE_HOLD_RATE_RAD_S)
    trace.update(angle_rad=float(angle), angle_deg=float(np.degrees(angle)), rate_rad_s=float(speed), desired_rate_rad_s=0.0, rate_error_rad_s=0.0, command_rad_s2=[0.0] * 3, command_norm_rad_s2=0.0, saturated=False, hold=bool(hold), released=bool(held and not hold))
    state["attitude_hold"] = bool(hold)
    if hold or np.linalg.norm(desired) < 1e-9:
        return np.zeros(3)
    axis = cross / sine if sine > 1e-9 else unit(np.cross(body_x, [0.0, 0.0, 1.0]))
    safe_rate = np.sqrt(2.0 * max_angular_accel * policy["attitude_brake_scale"] * max(angle - np.radians(0.5), 0.0))
    rate_error = axis * safe_rate - angular_velocity
    requested = 4.0 * rate_error
    length = np.linalg.norm(requested)
    command = requested / length * max_angular_accel if length > max_angular_accel else requested
    trace.update(desired_rate_rad_s=float(safe_rate), rate_error_rad_s=float(safe_rate - float(angular_velocity @ axis)), command_rad_s2=[float(value) for value in command], command_norm_rad_s2=float(np.linalg.norm(command)), saturated=bool(length > max_angular_accel))
    return command


finite = lambda value: float(value) if value is not None and np.isfinite(float(value)) else None

def telemetry(now, mode, gate, body_x, target_direction, accel_world, accel_body, diagnostics, speed, target_range, fuel, damage, track_count, emergency):
    def angle(first, second):
        first, second = unit(first), unit(second)
        return None if np.linalg.norm(first) < 1e-9 or np.linalg.norm(second) < 1e-9 else float(np.arccos(np.clip(first @ second, -1.0, 1.0)))

    record = {"schema": "wsorb.mppi_attitude", "time_s": finite(now), "mode": mode, "gate": gate, "emergency": bool(emergency), "target_align_rad": finite(angle(body_x, target_direction)), "burn_align_rad": finite(angle(body_x, accel_world)), "accel_body_mps2": [finite(value) for value in accel_body], "speed_mps": finite(speed), "target_range_m": finite(target_range), "fuel_kg": finite(fuel), "damage_j": finite(damage), "track_count": int(track_count), **diagnostics}
    record["warning"] = "coast_misaligned_with_residual_rate" if mode == "coast" and (record.get("target_align_rad") or 0.0) > np.radians(5.0) and (record.get("rate_rad_s") or 0.0) > 0.02 else None
    record["line"] = f"MPPI t={record['time_s'] or 0.0:.1f}s mode={mode} gate={gate} align={np.degrees(record.get('target_align_rad') or 0.0):.1f}deg rate={record.get('rate_rad_s') or 0.0:.3f} want={record.get('desired_rate_rad_s') or 0.0:.3f} cmd={record.get('command_norm_rad_s2') or 0.0:.3f} sat={int(record.get('saturated', False))} hold={int(record.get('hold', False))} emerg={int(emergency)} v={record.get('speed_mps') or 0.0:.2f} range={record.get('target_range_m') or 0.0:.0f}m fuel={record.get('fuel_kg') or 0.0:.1f} dmg={record.get('damage_j') or 0.0:.0f} rocks={track_count}"
    return record


def step(state, obs, rows, now, dt, to_body=None):
    position, velocity, target, body_x = (np.asarray(obs[key], float) for key in ("position", "velocity", "target", "body_x"))
    max_accel, max_angular = float(obs["max_accel"]), float(obs["max_angular"])
    policy = {**DEFAULT_POLICY, **state.setdefault("policy", {})}
    radius = float(policy.get("arrival_radius_m", ARRIVAL_RADIUS_M))
    if policy["plan_steering"]:
        update_tracks(state, rows, position, now)
    desired_accel = plan(state, position, velocity, target, body_x, float(obs["radius"]), max_accel, dt, float(obs["fuel"]), float(obs["damage"]), float(obs["mass"]), now, rows)
    if not policy["plan_steering"] or state.get("risk", 0.0) <= policy["plan_risk_threshold"]:
        desired_accel = np.zeros(3)
    nominal = cruise_velocity(position, target, state.get("cruise_speed_mps", CRUISE_SPEED_MPS), radius)
    acceleration, burning = govern(desired_accel, nominal, velocity, state.get("burning", False), max_accel)
    emergency_accel, emergency_active = emergency_guard(velocity, rows, now, max_accel, policy)
    if emergency_active:
        acceleration, burning = emergency_accel, True
    elif np.linalg.norm(target - position) - radius <= policy["coast_range_m"] and np.linalg.norm(velocity) >= policy["coast_speed_mps"]:
        acceleration, burning = np.zeros(3), False
    state["burning"] = burning
    desired = unit(nominal)
    alignment = np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0))) if np.linalg.norm(desired) > 1e-9 else 0.0
    if policy["main_engine_gate_rad"] > 0.0 and burning and not emergency_active and alignment > policy["main_engine_gate_rad"]:
        acceleration = np.zeros(3)
    angular_velocity = np.asarray(obs["angular_velocity"], float)
    gate = "alignment" if burning and not np.any(acceleration) else "burn" if burning else "hold" if state.get("attitude_hold") else "rate" if np.linalg.norm(angular_velocity) > ATTITUDE_HOLD_RATE_RAD_S else "alignment" if alignment > ATTITUDE_HOLD_RAD else "off"
    trace = {}
    angular = attitude(body_x, angular_velocity, desired, max_angular, dt, state, trace)
    if gate == "off":
        angular = np.zeros(3)
        trace.update(command_rad_s2=[0.0] * 3, command_norm_rad_s2=0.0, saturated=False)
    to_body = to_body or (lambda value: value)
    body_accel = np.asarray(to_body(acceleration), float)
    body_angular = np.asarray(to_body(angular), float)
    record = telemetry(now, "burn" if burning else "coast", gate, body_x, desired, acceleration, body_accel, trace, float(np.linalg.norm(velocity)), float(np.linalg.norm(target - position)), float(obs["fuel"]), float(obs["damage"]), len(rows), emergency_active)
    record["nearest_raw_range_m"] = min((float(np.linalg.norm(np.asarray(row[0], float))) for row in rows if np.all(np.isfinite(row))), default=None)
    return body_accel, body_angular, record


# END ALI MPPI CORE

t0 = st.SimGlobals.GetSimEntity().GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"]).as_datetime()
loop_hz = this.GetParam(st.VarType.double, "LoopFreqHz")
state = {"burning": False, "attitude_hold": False, "last_control": np.zeros(3), "rng": np.random.default_rng(12345), "policy": {"time_budget_s": 2.5 * DEFAULT_SCORE["nominal_time_s"], "arrival_radius_m": ARRIVAL_RADIUS_M}}
last_time, last_status, last_display, last_alert = None, -np.inf, -np.inf, -np.inf
while True:
    time.sleep(1.0 / loop_hz)
    now = (st.SimGlobals.SimClock.GetTimeNow().as_datetime() - t0).total_seconds()
    dt = 1.0 / loop_hz if last_time is None else max(1e-3, now - last_time)
    last_time = now
    stopped = shuttle.getLocVelAcc()
    position = np.asarray(stopped.loc_WRT_ExprIn(phobosFrame), float)
    velocity = np.asarray(stopped.vel_WRT_ExprIn(phobosFrame), float)
    target_position = np.asarray(target.getLocVelAcc().loc_WRT_ExprIn(phobosFrame), float)
    body_x = np.asarray(st.frames.FramedVector(np.array([1.0, 0.0, 0.0]), shuttleFrame).ExprIn(phobosFrame), float)
    mass = float(shuttle.getMass())
    mass = mass if np.isfinite(mass) and mass > 0.0 else 3388.7
    obs = {"position": position, "velocity": velocity, "target": target_position, "body_x": body_x, "angular_velocity": np.asarray(shuttle.getAngVelocity().WRT_ExprIn(phobosFrame), float), "fuel": float(shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"])), "damage": float(shuttle.GetParam(st.VarType.double, ["Stats", "Damage"])), "radius": float(shuttle.GetParam(st.VarType.double, ["Graphics", "VisualRadius_m"])), "max_accel": float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAccel_m_s2"])), "max_angular": float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAngAccel_rad_s2"])), "mass": mass}
    accel_body, angular_body, record = step(state, obs, GetNearestDebrisLocations(80), now, dt, lambda value: np.asarray(st.frames.FramedVector(value, phobosFrame).ExprIn(shuttleFrame), float))
    SetCommand(accel_body, angular_body)
    if now - last_status >= 0.5:
        last_status = now
        st.logger_info("MPPI " + json.dumps(record, separators=(",", ":"), allow_nan=False))
    if now - last_display >= 1.0:
        last_display = now
        st.OnScreenLogMessage(record["line"], "MPPI", st.Severity.Info)
    if record["warning"] is not None and now - last_alert >= 5.0:
        last_alert = now
        st.OnScreenAlert(f"MPPI warning {record['warning']} " + record["line"], "MPPI", st.Severity.Info)
