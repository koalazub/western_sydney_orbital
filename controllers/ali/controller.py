"""SDK entrypoint for Ali's MPPI controller.

This is a single-file STP submission. The marked core below is the only copy;
the local adapter imports the same marked region for tests and practice runs.
"""

# ruff: noqa: E402, E501, E401, F401, I001

# fmt: off
#############################################################################################
# DON'T CHANGE ANY OF THE BELOW. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################

import os, sys, time, datetime, traceback
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
    allDebrisInfo: list[npt.NDArray] = shuttle.GetParamArray(st.VarType.doubleM3x3, 
                                                            ["Radar", "SortedDebrisData"])
    nearest = allDebrisInfo[:min(N, len(allDebrisInfo))]
    return nearest


def PanTiltCamera(camera: st.Entity, yaw_deg: float, pitch_deg: float) -> None:
    if camera == topCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, -50.0, 0.0)
    elif camera == bottomCamera:
        yaw_deg = np.clip(yaw_deg, -89.0, 89.0)
        pitch_deg = np.clip(pitch_deg, 0.0, 50.0)
    camera.setRotation_DCM(st.math.Euler321_to_DCM([np.deg2rad(yaw_deg), np.deg2rad(pitch_deg), 0.0]), shuttleFrame)


def SetCommand(accelCommand: npt.NDArray, angAccelCommand: npt.NDArray) -> None:
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accelCommand)
    shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAngAccel"], angAccelCommand)


#############################################################################################
# DON'T CHANGE ANY OF THE ABOVE. NECESSARY FOR CORRECTLY JOINING THE COMPETITION SIMULATION #
#############################################################################################
# fmt: on

import json


# BEGIN ALI MPPI CORE
from enum import Enum

ARRIVAL_RADIUS_M = 50.0
MPPI_SAMPLES, MPPI_STEPS = 48, 40
CRUISE_SPEED_MPS, MIN_CRUISE_SPEED_MPS = 7.0, 1.0
ATTITUDE_HOLD_RAD, ATTITUDE_HOLD_RATE_RAD_S = np.radians(1.0), 0.01
DEFAULT_SCORE = {
    "nominal_time_s": 120.0,
    "nominal_fuel_kg": 75.0,
    "impact_threshold_j": 209200.0,
    "points_scale": 100.0,
    "damage_multiplier": 2.0,
    "score_offset": 0.5,
}
DEFAULT_POLICY = {
    "cruise_fraction": 0.5,
    "cruise_speed_mps": 8.0,
    "main_engine_gate_rad": 0.0,
    "coast_range_m": 200.0,
    "coast_speed_mps": 4.0,
    "emergency_range_m": 180.0,
    "emergency_lookahead_s": 7.0,
    "emergency_margin_m": 6.0,
    "emergency_escape_mps": 3.5,
    "plan_steering": True,
    "plan_risk_threshold": 0.01,
    "risk_weight": 200.0,
    "switch_weight": 0.5,
    "attitude_brake_scale": 0.65,
    "tracker": "line",
    "guard_source": "tracks",
    "risk_inflation": False,
    "risk_k_sigma": 1.5,
    "risk_max_inflate": 10.0,
    "mppi_fixed_samples": False,
    "avoidance": "mppi",
    "governor": "full",
    "governor_gain": 2.0,
    "governor_start_err": 0.3,
    "governor_stop_err": 0.1,
    "attitude_control": True,
    "avoider_margin": 6.0,
    "avoider_horizon": 15.0,
    "avoider_k_sigma": 1.5,
    "avoider_max_inflate": 10.0,
    "avoider_lag_s": 0.5,
    "avoider_w_aim": 1.5,
    "avoider_switch_margin": 0.3,
    "collision_damage_j": 1.0,
    "collision_jump_mps": 0.75,
    "collision_escape_s": 0.6,
    "collision_detour_s": 120.0,
    "collision_offset_m": 25.0,
    "collision_side_margin_m": 8.0,
    "collision_side_horizon_s": 20.0,
    "collision_side_switch_m": 2.0,
    "collision_safe_damage_j": 104600.0,
    "collision_slow_fraction": 0.8,
}


class Phase(Enum):
    """Why the controller issued the current acceleration."""

    COLLISION = "collision"
    EMERGENCY = "emergency"
    TERMINAL = "terminal"
    PLAN = "plan"
    AVOIDER = "avoider"
    CRUISE = "cruise"


class GuardMode(Enum):
    """Derived emergency-guard state for the current tick."""

    CLEAR = "clear"
    ESCAPE = "escape"


class CollisionMode(Enum):
    """Explicit post-contact lifecycle."""

    CLEAR = "clear"
    ESCAPE = "escape"
    DETOUR = "detour"


def unit(value):
    length = np.linalg.norm(value, axis=-1, keepdims=True)
    return np.divide(value, length, out=np.zeros_like(value), where=length > 1e-9)


class KalmanTracker:
    """Tom's constant-velocity radar tracker as one self-contained submission class."""

    def __init__(self):
        self.pos_sigma, self.rr_sigma, self.accel_sigma = 2.0, 0.02, 0.02
        self.v0_sigma, self.gate_chi2, self.diameter_tol = 10.0, 16.0, 0.3
        self.drop_after_s, self.t_last = 5.0, -np.inf
        self.tracks = []

    def update(self, position, velocity, rows):
        valid = [row for row in rows if np.all(np.isfinite(row)) and row[1, 2] > 0.0]
        if not valid:
            return
        now = max(float(row[2, 0]) for row in valid)
        if now <= self.t_last + 1e-9:
            return
        self.t_last = now
        measurements = []
        for row in valid:
            relative = np.asarray(row[0], float)
            measurements.append(
                {
                    "p": np.asarray(position, float) + relative,
                    "rel": relative,
                    "vr": float(row[1, 1]),
                    "diam": float(row[1, 2]),
                }
            )
        for track in self.tracks:
            self._predict(track, now)
        pairs = []
        for index, track in enumerate(self.tracks):
            inverse = np.linalg.inv(track["P"][:3, :3] + self.pos_sigma**2 * np.eye(3))
            for measurement_index, measurement in enumerate(measurements):
                if abs(measurement["diam"] - track["diam"]) > self.diameter_tol * max(
                    track["diam"], 0.5
                ):
                    continue
                residual = measurement["p"] - track["x"][:3]
                distance = float(residual @ inverse @ residual)
                if distance < self.gate_chi2:
                    pairs.append((distance, index, measurement_index))
        pairs.sort()
        used_tracks, used_measurements = set(), set()
        for _distance, track_index, measurement_index in pairs:
            if track_index in used_tracks or measurement_index in used_measurements:
                continue
            used_tracks.add(track_index)
            used_measurements.add(measurement_index)
            self._correct(
                self.tracks[track_index], measurements[measurement_index], position, velocity, now
            )
        for measurement_index, measurement in enumerate(measurements):
            if measurement_index not in used_measurements:
                self.tracks.append(self._new(measurement, velocity, now))
        self.tracks = [track for track in self.tracks if now - track["last"] <= self.drop_after_s]

    def _new(self, measurement, velocity, now):
        distance = max(float(np.linalg.norm(measurement["rel"])), 1e-6)
        line = measurement["rel"] / distance
        radial = measurement["vr"] + np.asarray(velocity, float) @ line
        covariance = np.zeros((6, 6))
        covariance[:3, :3] = self.pos_sigma**2 * np.eye(3)
        covariance[3:, 3:] = self.v0_sigma**2 * (
            np.eye(3) - np.outer(line, line)
        ) + self.rr_sigma**2 * np.outer(line, line)
        return {
            "x": np.concatenate([measurement["p"], radial * line]),
            "P": covariance,
            "diam": measurement["diam"],
            "t": now,
            "last": now,
            "hits": 1,
        }

    def _predict(self, track, now):
        dt = now - track["t"]
        if dt <= 0.0:
            return
        transition = np.eye(6)
        transition[:3, 3:] = dt * np.eye(3)
        noise = self.accel_sigma**2
        process = np.zeros((6, 6))
        process[:3, :3] = noise * dt**3 / 3.0 * np.eye(3)
        process[:3, 3:] = process[3:, :3] = noise * dt**2 / 2.0 * np.eye(3)
        process[3:, 3:] = noise * dt * np.eye(3)
        track["x"] = transition @ track["x"]
        track["P"] = transition @ track["P"] @ transition.T + process
        track["t"] = now

    def _correct(self, track, measurement, position, velocity, now):
        identity = np.eye(6)
        observation = np.hstack([np.eye(3), np.zeros((3, 3))])
        covariance = observation @ track["P"] @ observation.T + self.pos_sigma**2 * np.eye(3)
        gain = track["P"] @ observation.T @ np.linalg.inv(covariance)
        track["x"] = track["x"] + gain @ (measurement["p"] - observation @ track["x"])
        residual = identity - gain @ observation
        track["P"] = (
            residual @ track["P"] @ residual.T + gain @ (self.pos_sigma**2 * np.eye(3)) @ gain.T
        )
        relative = track["x"][:3] - np.asarray(position, float)
        distance = max(float(np.linalg.norm(relative)), 1e-6)
        line = relative / distance
        predicted_range_rate = float(velocity @ line) + measurement["vr"]
        relative_speed = float(np.linalg.norm(track["x"][3:] - velocity))
        variance = self.rr_sigma**2 + (self.pos_sigma / distance * relative_speed) ** 2
        row = np.concatenate([np.zeros(3), line]).reshape(1, 6)
        innovation = predicted_range_rate - float((row @ track["x"]).item())
        scalar = float((row @ track["P"] @ row.T).item()) + variance
        gain = track["P"] @ row.T / scalar
        track["x"] = track["x"] + (gain * innovation).ravel()
        residual = identity - gain @ row
        track["P"] = residual @ track["P"] @ residual.T + (gain * variance) @ gain.T
        track["hits"] += 1
        track["last"] = now
        alpha = max(1.0 / track["hits"], 0.1)
        track["diam"] = (1.0 - alpha) * track["diam"] + alpha * measurement["diam"]

    def snapshot(self, now):
        tracks = []
        for track in self.tracks:
            dt = float(np.clip(now - track["t"], 0.0, 1.0))
            tracks.append(
                {
                    "p": track["x"][:3] + track["x"][3:] * dt,
                    "v": track["x"][3:].copy(),
                    "r": track["diam"] / 2.0,
                    "t": float(now),
                    "sp": float(np.sqrt(np.max(np.diag(track["P"])[:3]))),
                    "sv": float(np.sqrt(np.max(np.diag(track["P"])[3:]))),
                }
            )
        return tracks


SHUTTLE_HISTORY_S = 10.0
SHUTTLE_HISTORY_LIMIT = 200
SHUTTLE_HISTORY_EDGE_S = 0.5


def record_shuttle_state(state, now, position):
    """Keep recent shuttle positions so aged radar rows can be reconstructed."""
    history = state.setdefault("shuttle_history", [])
    position = np.asarray(position, float)
    if history and now < history[-1][0]:
        history.clear()
    history.append((float(now), position.copy()))
    cutoff = float(now) - SHUTTLE_HISTORY_S
    state["shuttle_history"] = [entry for entry in history if entry[0] >= cutoff][
        -SHUTTLE_HISTORY_LIMIT:
    ]
    return state["shuttle_history"]


def shuttle_position_at(history, epoch, fallback):
    """Interpolate the shuttle position at a radar measurement epoch."""
    if not history:
        return fallback
    if epoch <= history[0][0]:
        return history[0][1] if history[0][0] - epoch <= SHUTTLE_HISTORY_EDGE_S else fallback
    if epoch >= history[-1][0]:
        return history[-1][1] if epoch - history[-1][0] <= SHUTTLE_HISTORY_EDGE_S else fallback
    for (time0, position0), (time1, position1) in zip(history, history[1:], strict=False):
        if time0 <= epoch <= time1:
            span = float(time1 - time0)
            return (
                position1.copy()
                if span <= 1e-9
                else position0 + (position1 - position0) * ((epoch - time0) / span)
            )
    return fallback


def reconstruct_measurements(rows, history, position):
    """Rebase each radar row from its measurement epoch to the current position."""
    position = np.asarray(position, float)
    adjusted = []
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            adjusted.append(row)
            continue
        measured_at = shuttle_position_at(history, float(row[2, 0]), position)
        rebased = row.copy()
        rebased[0] = row[0] + measured_at - position
        adjusted.append(rebased)
    return adjusted


def update_estimates(state, rows, position, velocity, now, policy):
    if policy["tracker"] == "kalman":
        tracker = state.setdefault("kalman", KalmanTracker())
        tracker.update(position, velocity, rows)
        state["tracks"] = sorted(
            tracker.snapshot(now), key=lambda track: np.linalg.norm(track["p"] - position)
        )[:160]
    else:
        update_tracks(state, rows, position, velocity, now)
        tracks = []
        for rock in state.get("rocks", []):
            dt = max(0.0, float(now) - float(rock["t"]))
            tracks.append(
                {
                    "p": np.asarray(rock["p"], float) + np.asarray(rock["v"], float) * dt,
                    "v": np.asarray(rock["v"], float).copy(),
                    "r": float(rock["r"]),
                    "t": float(now),
                    "sp": float(rock["s"]),
                    "sv": 0.0,
                }
            )
        state["tracks"] = tracks
    return state["tracks"]


def collision_check(state, velocity, damage, dt, policy):
    energy = max(0.0, float(damage) - float(state.setdefault("last_damage", damage)))
    previous_velocity = state.get("last_velocity", velocity)
    previous_accel = state.get("last_accel", np.zeros(3))
    change = np.asarray(velocity, float) - previous_velocity - previous_accel * dt
    state["last_damage"], state["last_velocity"] = float(damage), np.asarray(velocity, float).copy()
    jump = np.linalg.norm(change) if dt <= 1.0 else 0.0
    hit = energy > policy["collision_damage_j"] or jump > policy["collision_jump_mps"]
    return hit, energy, change


def route_lateral(route, body_x):
    """A stable unit vector perpendicular to the route."""
    axis = body_x if abs(float(route @ body_x)) < 0.9 else np.array([0.0, 0.0, 1.0])
    lateral = np.cross(route, axis)
    if np.linalg.norm(lateral) < 1e-6:
        lateral = np.cross(route, [0.0, 0.0, 1.0])
    if np.linalg.norm(lateral) < 1e-6:
        lateral = np.array([0.0, 1.0, 0.0])
    return unit(lateral)


def detour_clearance(position, target, side, offset, tracks, speed, horizon, margin):
    """Minimum predicted clearance along a straight detour to target + side*offset."""
    detour = np.asarray(target, float) + np.asarray(side, float) * float(offset)
    delta = detour - position
    distance = float(np.linalg.norm(delta))
    if distance < 1e-9 or not tracks:
        return None
    direction = delta / distance
    duration = min(float(horizon), distance / max(float(speed), 1.0))
    if duration <= 0.0:
        return None
    times = np.linspace(0.0, duration, 12)
    ship = np.asarray(position, float) + direction[None, :] * (float(speed) * times)[:, None]
    best = np.inf
    for track in tracks:
        future = (
            np.asarray(track["p"], float)[None, :]
            + np.asarray(track["v"], float)[None, :] * times[:, None]
        )
        clearance = np.linalg.norm(future - ship, axis=1) - (float(track["r"]) + float(margin))
        best = min(best, float(clearance.min()))
    return best


def choose_collision_side(
    route, position, target, body_x, tracks, velocity, offset, previous_side, policy
):
    """Score both detour sides by predicted clearance; keep continuity when close."""
    if not tracks:
        return None, None, None, "fallback"
    lateral = route_lateral(route, body_x)
    candidates = (lateral, -lateral)
    speed = max(float(np.linalg.norm(velocity)), 1.0)
    scores = [
        detour_clearance(
            position,
            target,
            side,
            offset,
            tracks[:30],
            speed,
            policy["collision_side_horizon_s"],
            policy["collision_side_margin_m"],
        )
        for side in candidates
    ]
    if any(score is None for score in scores):
        return None, None, None, "fallback"
    best = int(np.argmax(scores))
    reason = "prediction"
    if previous_side is not None:
        previous = np.asarray(previous_side, float)
        previous_index = 0 if float(previous @ lateral) >= 0.0 else 1
        if scores[previous_index] >= scores[best] - policy["collision_side_switch_m"]:
            best = previous_index
            reason = "continuity"
    return candidates[best], float(scores[best]), float(scores[1 - best]), reason


def fallback_collision_side(change, closest, velocity, route, body_x, previous_side):
    """Geometry-only side choice used before any fitted track exists."""
    if np.linalg.norm(change) > 0.05:
        escape = unit(change)
    elif closest is not None and np.linalg.norm(closest) > 1e-6:
        escape = unit(-closest)
    else:
        escape = -unit(velocity)
    if np.linalg.norm(escape) < 1e-9:
        escape = -route if np.linalg.norm(route) > 1e-9 else unit(body_x)
    lateral = escape - float(escape @ route) * route
    if np.linalg.norm(lateral) < 1e-6:
        lateral = np.cross(route, body_x)
    if np.linalg.norm(lateral) < 1e-6:
        lateral = np.cross(route, [0.0, 0.0, 1.0])
    if np.linalg.norm(lateral) < 1e-6:
        lateral = np.array([0.0, 1.0, 0.0])
    side = unit(lateral)
    if closest is not None and abs(float(closest @ side)) > 1e-6:
        side = -np.sign(float(closest @ side)) * side
    if previous_side is not None and (closest is None or float(closest @ previous_side) <= 0.0):
        side = np.asarray(previous_side, float).copy()
    return side


def collision_route(
    state, position, velocity, target, body_x, rows, tracks, now, energy, change, max_accel, policy
):
    hits = int(state.get("collision_hits", 0)) + 1
    state["collision_hits"] = hits
    route = unit(np.asarray(target, float) - position)
    if np.linalg.norm(route) < 1e-9:
        route = unit(body_x)
    if np.linalg.norm(route) < 1e-9:
        route = np.array([1.0, 0.0, 0.0])
    valid = [np.asarray(row[0], float) for row in rows if np.all(np.isfinite(row))]
    closest = min(valid, key=np.linalg.norm, default=None)
    impulse_s = float(policy["collision_escape_s"]) * (1.0 + 0.5 * (hits - 1))
    speed = max(float(np.linalg.norm(velocity)), 1.0)
    time_to_go = float(np.linalg.norm(np.asarray(target, float) - position)) / speed
    desired_offset = float(max_accel) * impulse_s * time_to_go
    offset = min(float(policy["collision_offset_m"]), desired_offset)
    if desired_offset > offset and time_to_go > 1e-6:
        impulse_s = offset / (float(max_accel) * time_to_go)
    previous_side = state.get("collision_side")
    side, score, other, reason = choose_collision_side(
        route,
        position,
        target,
        body_x,
        tracks,
        velocity,
        offset,
        previous_side,
        policy,
    )
    if side is None:
        side = fallback_collision_side(change, closest, velocity, route, body_x, previous_side)
        reason = "fallback"
    state["collision_side"] = side.copy()
    state["collision"] = {
        "side": side,
        "offset": float(offset),
        "impulse_s": impulse_s,
        "until": float(now) + float(policy["collision_detour_s"]),
        "hits": hits,
        "side_score": score,
        "side_other": other,
        "side_reason": reason,
    }
    state["collision_escape_until"] = float(now) + impulse_s
    state["last_impact_j"], state["last_impact_mps"] = float(energy), float(np.linalg.norm(change))
    return state["collision"]


def active_collision(state, now):
    collision = state.get("collision")
    if collision is None:
        return None
    if now >= collision["until"]:
        state.pop("collision", None)
        return None
    return collision


def detour_target(target, collision):
    return np.asarray(target, float) + collision["side"] * float(collision["offset"])


def safe_cruise(state, speed, damage, policy):
    if (
        float(damage) > float(policy["collision_safe_damage_j"])
        or int(state.get("collision_hits", 0)) >= 3
    ):
        return speed * float(np.clip(policy["collision_slow_fraction"], 0.5, 1.0))
    return speed


def update_tracks(state, rows, position, velocity, now):
    rocks = state.setdefault("rocks", [])
    velocity = np.asarray(velocity, float)
    for row in rows:
        if not np.all(np.isfinite(row)) or row[1, 2] <= 0.0:
            continue
        measured = position + row[0]
        radius, at = 0.5 * float(row[1, 2]), float(row[2, 0])
        gaps = [
            np.linalg.norm(rock["p"] + rock["v"] * (at - rock["t"]) - measured) for rock in rocks
        ]
        index = int(np.argmin(gaps)) if gaps else -1
        if index < 0 or gaps[index] >= max(15.0, 2.0 * rocks[index]["s"]):
            los = unit(row[0])
            radial = float(row[1, 1]) + los @ velocity
            rocks.append(
                {
                    "p": measured.copy(),
                    "v": radial * los,
                    "r": radius,
                    "t": at,
                    "s": 0.5,
                    "q": [(at, measured.copy())],
                }
            )
        elif at > rocks[index]["t"]:
            rock = rocks[index]
            rock.update(p=measured.copy(), r=max(rock["r"], radius), t=at)
            rock["q"] = (rock["q"] + [(at, measured.copy())])[-10:]
            if len(rock["q"]) >= 4 and rock["q"][-1][0] - rock["q"][0][0] >= 1.0:
                times = np.array([sample[0] for sample in rock["q"]])
                points = np.array([sample[1] for sample in rock["q"]])
                design = np.column_stack((np.ones(len(times)), times - times[-1]))
                coef, *_ = np.linalg.lstsq(design, points, rcond=None)
                rock["p"], rock["v"] = coef[0], coef[1]
                rock["s"] = float(
                    np.sqrt(np.sum((points - design @ coef) ** 2) / max(1, len(times) - 2))
                )
    state["rocks"] = sorted(
        (rock for rock in rocks if now - rock["t"] <= 5.0),
        key=lambda rock: np.linalg.norm(rock["p"] - position),
    )[:160]


def cruise_velocity(position, target_position, cruise_speed_mps=CRUISE_SPEED_MPS):
    delta = target_position - position
    distance = np.linalg.norm(delta)
    return np.zeros(3) if distance < 1e-9 else max(0.0, float(cruise_speed_mps)) * delta / distance


def govern(desired_accel, nominal_velocity, velocity, burning, max_accel):
    error = nominal_velocity - velocity
    forward = (
        np.linalg.norm(desired_accel) > 0.5 * max_accel
        and desired_accel @ unit(nominal_velocity) > 0.0
    )
    burning = (
        (burning and (np.linalg.norm(error) >= 0.3 or forward))
        or np.linalg.norm(error) > 0.5
        or forward
    )
    return (
        (max_accel * unit(desired_accel if forward else error), True)
        if burning
        else (np.zeros(3), False)
    )


def proportional_govern(nominal_velocity, velocity, state, max_accel, policy):
    error = np.asarray(nominal_velocity, float) - velocity
    magnitude = float(np.linalg.norm(error))
    firing = bool(state.get("governor_firing", False))
    firing = (
        magnitude >= policy["governor_stop_err"]
        if firing
        else magnitude > policy["governor_start_err"]
    )
    state["governor_firing"] = firing
    if not firing:
        return np.zeros(3), False
    command = float(policy["governor_gain"]) * error
    length = float(np.linalg.norm(command))
    if length > max_accel:
        command = command * (max_accel / length)
    return command, True


def emergency_guard(velocity, rows, now, max_accel, policy=None, tracks=None, position=None):
    policy = {**DEFAULT_POLICY, **(policy or {})}
    velocity = np.asarray(velocity, float)
    items = []
    if tracks is not None and position is not None:
        for track in tracks:
            relative = np.asarray(track["p"], float) - position
            if np.linalg.norm(relative) <= policy["emergency_range_m"]:
                items.append(
                    (
                        relative,
                        np.asarray(track["v"], float),
                        float(track["r"]) + policy["emergency_margin_m"],
                    )
                )
    else:
        for row in rows:
            if np.all(np.isfinite(row)) and row[1, 2] > 0.0:
                los = unit(row[0])
                rock_velocity = los * (float(row[1, 1]) + los @ velocity)
                relative = row[0] + (now - float(row[2, 0])) * (rock_velocity - velocity)
                if np.linalg.norm(relative) <= policy["emergency_range_m"]:
                    items.append(
                        (
                            relative,
                            rock_velocity,
                            0.55 * float(row[1, 2]) + policy["emergency_margin_m"],
                        )
                    )
    if not items:
        return np.zeros(3), False
    relative, rock_velocity, keep_out = (np.array(values) for values in zip(*items, strict=True))

    def miss(option):
        relative_velocity = rock_velocity - option
        time = np.clip(
            -np.einsum("ij,ij->i", relative, relative_velocity)
            / (np.einsum("ij,ij->i", relative_velocity, relative_velocity) + 1e-9),
            0.0,
            policy["emergency_lookahead_s"],
        )
        return np.linalg.norm(relative + time[:, None] * relative_velocity, axis=1) - keep_out

    current = miss(velocity)
    if current.min() >= 0.0:
        return np.zeros(3), False
    threat = int(np.argmin(current))
    heading = unit(velocity) if np.linalg.norm(velocity) > 1e-6 else np.array([1.0, 0.0, 0.0])
    lateral = relative[threat] - (relative[threat] @ heading) * heading
    side = (
        unit(lateral)
        if np.linalg.norm(lateral) > 1e-6
        else unit(np.cross(heading, [0.0, 0.0, 1.0] if abs(heading[2]) < 0.9 else [1.0, 0.0, 0.0]))
    )
    escape = policy["emergency_escape_mps"]
    options = [
        velocity + escape * unit(-relative[threat]),
        velocity + escape * side,
        velocity - escape * side,
        velocity,
    ]
    scores = [float(miss(option).min()) for option in options]
    # Prefer a sideways dodge over reversing the launch. Retrograde only wins
    # when neither lateral option produces a positive predicted clearance.
    lateral = max((1, 2), key=lambda index: scores[index])
    best = lateral if scores[lateral] >= 0.0 else int(np.argmax(scores))
    if scores[best] <= float(current.min()):
        return np.zeros(3), False
    return max_accel * unit(options[best] - velocity), True


def avoid_velocity(v_des, tracks, position, velocity, state, policy):
    v_des = np.asarray(v_des, float)
    speed = float(np.linalg.norm(v_des))
    if speed < 1e-3 or not tracks:
        state["avoid_v_prev"] = v_des
        state["avoid_info"] = {
            "blocked": False,
            "any_safe": True,
            "clearance": None,
            "dv": 0.0,
            "n_tracks": len(tracks),
        }
        return v_des
    p_rel = np.array([np.asarray(track["p"], float) - position for track in tracks])
    v_abs = np.array([np.asarray(track["v"], float) for track in tracks])
    radii = np.array([float(track["r"]) for track in tracks])
    sigma_p = np.array([float(track["sp"]) for track in tracks])
    sigma_v = np.array([float(track["sv"]) for track in tracks])
    previous = state.get("avoid_v_prev", v_des)
    heading = unit(v_des)
    axis = np.array([1.0, 0.0, 0.0]) if abs(float(heading[0])) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = unit(np.cross(heading, axis))
    e2 = unit(np.cross(heading, e1))
    phis = np.linspace(0.0, 2.0 * np.pi, 16, endpoint=False)
    ring = np.cos(phis)[:, None] * e1 + np.sin(phis)[:, None] * e2
    fractions = np.array([0.05, 0.10, 0.18, 0.28, 0.40, 0.60, 0.90])
    candidates = [v_des, velocity, previous, np.zeros(3)]
    for scale in (1.0, 0.7, 0.4):
        candidates.extend(
            (scale * v_des + fractions[:, None, None] * speed * ring[None]).reshape(-1, 3)
        )
    options = np.array(candidates)
    lag = float(policy["avoider_lag_s"])
    predicted = p_rel + lag * (v_abs - velocity)
    relative_velocity = v_abs[:, None, :] - options[None, :, :]
    squared = np.einsum("mnk,mnk->mn", relative_velocity, relative_velocity)
    projected = np.einsum("mnk,mk->mn", relative_velocity, predicted)
    time = np.clip(-projected / (squared + 1e-9), 0.0, float(policy["avoider_horizon"]))
    distance = np.linalg.norm(predicted[:, None, :] + time[..., None] * relative_velocity, axis=2)
    inflation = float(policy["avoider_k_sigma"]) * (
        sigma_p[:, None] + sigma_v[:, None] * (time + lag)
    )
    keep_out = (radii + float(policy["avoider_margin"]))[:, None] + np.minimum(
        inflation, float(policy["avoider_max_inflate"])
    )
    clearance = (distance - keep_out).min(axis=0)
    safe = clearance > 0.0
    cost = np.linalg.norm(options - velocity, axis=1) + float(
        policy["avoider_w_aim"]
    ) * np.linalg.norm(options - v_des, axis=1)
    if safe.any():
        selected = int(np.argmin(np.where(safe, cost, np.inf)))
        if safe[2] and cost[2] <= cost[selected] + float(policy["avoider_switch_margin"]):
            selected = 2
    else:
        selected = int(np.argmax(clearance))
    state["avoid_v_prev"] = options[selected].copy()
    state["avoid_info"] = {
        "blocked": not bool(safe[0]),
        "any_safe": bool(safe.any()),
        "clearance": float(clearance[selected]),
        "dv": float(np.linalg.norm(options[selected] - velocity)),
        "n_tracks": len(tracks),
    }
    return options[selected]


def prepare_cruise(state, remaining, max_accel, damage, ship_mass_kg, policy):
    budget = policy.get("time_budget_s")
    budget = 2.5 * DEFAULT_SCORE["nominal_time_s"] if budget is None else float(budget)
    fixed_speed = policy.get("cruise_speed_mps")
    reference = state.setdefault(
        "cruise_reference_mps",
        float(fixed_speed)
        if fixed_speed
        else remaining / max(1.0, policy["cruise_fraction"] * budget),
    )
    cruise_speed = min(
        max(MIN_CRUISE_SPEED_MPS, float(reference)),
        float(np.sqrt(2.0 * max(1e-6, max_accel) * remaining)),
    )
    cruise_speed = safe_cruise(state, cruise_speed, damage, policy)
    state["cruise_speed_mps"], state["fuel_rate_kg_s"] = (
        cruise_speed,
        max(1e-6, float(ship_mass_kg) * float(max_accel) / (80.0 * 9.8)),
    )
    return cruise_speed


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
    policy = {**DEFAULT_POLICY, **state.setdefault("policy", {})}
    radius_target = float(policy.get("arrival_radius_m", ARRIVAL_RADIUS_M))
    remaining = max(0.0, float(np.linalg.norm(target_position - position)) - radius_target)
    cruise_speed = prepare_cruise(state, remaining, max_accel, damage, ship_mass_kg, policy)
    goal = np.asarray(state.get("steer_target", target_position), float)
    nominal = max_accel * unit(cruise_velocity(position, goal, cruise_speed) - velocity)
    tracks = state.get("tracks", [])[:30]
    rng = state.setdefault("rng", np.random.default_rng(12345))
    controls = unit(nominal + rng.normal(size=(MPPI_SAMPLES, MPPI_STEPS, 3)) * max_accel) * (
        rng.random((MPPI_SAMPLES, MPPI_STEPS, 1)) * max_accel
    )
    # Sample zero is the do-nothing coast path. Its risk decides whether the
    # planner should steer; the selected sample's risk can be zero even when
    # the current trajectory is unsafe.
    controls[0] = 0.0
    if policy["mppi_fixed_samples"]:
        previous = state.get("last_plan")
        controls[1] = (
            previous
            if np.shape(previous) == (MPPI_STEPS, 3)
            else state.get("last_control", np.zeros(3))
        )
    plan_dt = float(
        np.clip(
            float(np.clip(0.1 * (now + remaining / max(cruise_speed, 1.0)), 8.0, 30.0))
            / MPPI_STEPS,
            max(dt, 1e-3),
            0.5,
        )
    )
    positions, ship_velocities = (
        np.broadcast_to(value, (MPPI_SAMPLES, 3)).copy() for value in (position, velocity)
    )
    risk = np.zeros(MPPI_SAMPLES)
    if tracks:
        centers = np.array([track["p"] for track in tracks])
        rock_velocities = np.array([track["v"] for track in tracks])
        radii = np.array([track["r"] for track in tracks])
        sigma_p = np.array([track["sp"] for track in tracks])
        sigma_v = np.array([track["sv"] for track in tracks])
    for step in range(MPPI_STEPS):
        positions += ship_velocities * plan_dt + 0.5 * controls[:, step] * plan_dt**2
        ship_velocities += controls[:, step] * plan_dt
        if tracks:
            future = centers + (step + 1) * plan_dt * rock_velocities
            offset = future[None, :, :] - positions[:, None, :]
            along = np.clip(np.einsum("knd,d->kn", offset, body_x), -radius, radius)
            distance = (
                np.linalg.norm(
                    future[None, :, :] - (positions[:, None, :] + along[:, :, None] * body_x),
                    axis=2,
                )
                - radius
            )
            inflation = (
                np.minimum(
                    float(policy["risk_k_sigma"])
                    * (sigma_p[None, :] + sigma_v[None, :] * (step + 1) * plan_dt),
                    float(policy["risk_max_inflate"]),
                )
                if policy["risk_inflation"]
                else 0.0
            )
            risk += np.maximum(
                0.0,
                3.0
                + 0.5 * np.linalg.norm(ship_velocities, axis=1)[:, None]
                - (distance - radii - inflation),
            ).sum(axis=1)
    time_to_go = np.maximum(
        0.0, np.linalg.norm(positions - target_position, axis=1) - radius_target
    ) / np.maximum(np.linalg.norm(ship_velocities, axis=1), 1.0)
    burn_time = np.linalg.norm(controls, axis=2).sum(axis=1) / max(1e-6, max_accel) * plan_dt
    points = DEFAULT_SCORE["points_scale"] * (
        DEFAULT_SCORE["nominal_time_s"] / np.maximum(now + MPPI_STEPS * plan_dt + time_to_go, 1e-3)
        - (fuel + state["fuel_rate_kg_s"] * burn_time) / DEFAULT_SCORE["nominal_fuel_kg"]
        - DEFAULT_SCORE["damage_multiplier"] * damage / DEFAULT_SCORE["impact_threshold_j"]
        + DEFAULT_SCORE["score_offset"]
    )
    cost = (
        -points
        + policy["risk_weight"] * risk
        + policy["switch_weight"] * np.linalg.norm(controls[:, 0] - state["last_control"], axis=1)
    )
    best = int(np.argmin(cost))
    state["last_control"], state["last_plan"] = controls[best, 0].copy(), controls[best].copy()
    state["coast_risk"] = float(risk[0])
    return controls[best, 0]


def attitude(body_x, angular_velocity, desired, max_angular_accel, dt, state, diagnostics=None):
    policy = {**DEFAULT_POLICY, **state.get("policy", {})}
    trace = diagnostics if diagnostics is not None else {}
    desired = unit(desired)
    cross = np.cross(body_x, desired)
    sine = np.linalg.norm(cross)
    angle = np.arctan2(sine, float(np.clip(body_x @ desired, -1.0, 1.0)))
    speed = np.linalg.norm(angular_velocity)
    held = state.get("attitude_hold", False)
    hold = angle <= (np.radians(2.0) if held else ATTITUDE_HOLD_RAD) and speed <= (
        0.02 if held else ATTITUDE_HOLD_RATE_RAD_S
    )
    trace.update(
        angle_rad=float(angle),
        angle_deg=float(np.degrees(angle)),
        rate_rad_s=float(speed),
        desired_rate_rad_s=0.0,
        rate_error_rad_s=0.0,
        command_rad_s2=[0.0] * 3,
        command_norm_rad_s2=0.0,
        saturated=False,
        hold=bool(hold),
        released=bool(held and not hold),
    )
    state["attitude_hold"] = bool(hold)
    if hold or np.linalg.norm(desired) < 1e-9:
        return np.zeros(3)
    axis = cross / sine if sine > 1e-9 else unit(np.cross(body_x, [0.0, 0.0, 1.0]))
    safe_rate = np.sqrt(
        2.0 * max_angular_accel * policy["attitude_brake_scale"] * max(angle - np.radians(0.5), 0.0)
    )
    rate_error = axis * safe_rate - angular_velocity
    requested = 4.0 * rate_error
    length = np.linalg.norm(requested)
    command = requested / length * max_angular_accel if length > max_angular_accel else requested
    trace.update(
        desired_rate_rad_s=float(safe_rate),
        rate_error_rad_s=float(safe_rate - float(angular_velocity @ axis)),
        command_rad_s2=[float(value) for value in command],
        command_norm_rad_s2=float(np.linalg.norm(command)),
        saturated=bool(length > max_angular_accel),
    )
    return command


def finite(value):
    return float(value) if value is not None and np.isfinite(float(value)) else None


def telemetry(
    now,
    mode,
    gate,
    body_x,
    target_direction,
    accel_world,
    accel_body,
    diagnostics,
    speed,
    target_range,
    fuel,
    damage,
    track_count,
    emergency,
):
    def angle(first, second):
        first, second = unit(first), unit(second)
        return (
            None
            if np.linalg.norm(first) < 1e-9 or np.linalg.norm(second) < 1e-9
            else float(np.arccos(np.clip(first @ second, -1.0, 1.0)))
        )

    record = {
        "schema": "wsorb.mppi_attitude",
        "time_s": finite(now),
        "mode": mode,
        "gate": gate,
        "emergency": bool(emergency),
        "target_align_rad": finite(angle(body_x, target_direction)),
        "burn_align_rad": finite(angle(body_x, accel_world)),
        "accel_body_mps2": [finite(value) for value in accel_body],
        "speed_mps": finite(speed),
        "target_range_m": finite(target_range),
        "fuel_kg": finite(fuel),
        "damage_j": finite(damage),
        "track_count": int(track_count),
        **diagnostics,
    }
    record["warning"] = (
        "coast_misaligned_with_residual_rate"
        if mode == "coast"
        and (record.get("target_align_rad") or 0.0) > np.radians(5.0)
        and (record.get("rate_rad_s") or 0.0) > 0.02
        else None
    )
    record["line"] = (
        f"MPPI t={record['time_s'] or 0.0:.1f}s mode={mode} gate={gate} align={np.degrees(record.get('target_align_rad') or 0.0):.1f}deg rate={record.get('rate_rad_s') or 0.0:.3f} want={record.get('desired_rate_rad_s') or 0.0:.3f} cmd={record.get('command_norm_rad_s2') or 0.0:.3f} sat={int(record.get('saturated', False))} hold={int(record.get('hold', False))} emerg={int(emergency)} v={record.get('speed_mps') or 0.0:.2f} range={record.get('target_range_m') or 0.0:.0f}m fuel={record.get('fuel_kg') or 0.0:.1f} dmg={record.get('damage_j') or 0.0:.0f} rocks={track_count}"
    )
    return record


class GuidanceController:
    """One Ali guidance run with explicit phase and state-machine enums.

    Persistent data lives in ``state`` so the adapter keeps a run alive across
    ticks. The enums are derived at the start of each tick; they never hold a
    second copy of the state.
    """

    def __init__(self, settings=None, state=None):
        if state is None:
            state = {
                "burning": False,
                "attitude_hold": False,
                "last_control": np.zeros(3),
                "rng": np.random.default_rng(12345),
                "policy": {},
            }
        self.state = state
        if settings:
            self.state.setdefault("policy", {}).update(settings)
        self.phase = Phase.CRUISE
        self.guard_mode = GuardMode.CLEAR
        self.collision_mode = CollisionMode.CLEAR

    def _collision_mode(self, collision, now):
        match collision:
            case None:
                return CollisionMode.CLEAR
            case _ if now < self.state.get("collision_escape_until", -np.inf):
                return CollisionMode.ESCAPE
            case _:
                return CollisionMode.DETOUR

    def _arbitrate(
        self,
        collision,
        collision_mode,
        guard_mode,
        terminal,
        acceleration,
        burning,
        emergency_accel,
        max_accel,
        plan_active,
        avoider,
    ):
        """Select the command source by explicit priority, not nested ifs."""
        match (collision_mode, guard_mode, terminal):
            case (CollisionMode.ESCAPE, _, _):
                return max_accel * unit(collision["side"]), True, Phase.COLLISION, "collision"
            case (_, GuardMode.ESCAPE, _):
                return emergency_accel, True, Phase.EMERGENCY, "guard"
            case (_, _, True):
                return np.zeros(3), False, Phase.TERMINAL, "coast"
            case _ if plan_active:
                return acceleration, burning, Phase.PLAN, "plan"
            case _ if avoider:
                return acceleration, burning, Phase.AVOIDER, "avoider"
            case _:
                return acceleration, burning, Phase.CRUISE, "governor"

    def _gate(self, phase, burning, acceleration, alignment, angular_velocity):
        match phase:
            case Phase.COLLISION:
                return "collision"
            case _ if burning and not np.any(acceleration):
                return "alignment"
            case _ if burning:
                return "burn"
            case _ if self.state.get("attitude_hold"):
                return "hold"
            case _ if np.linalg.norm(angular_velocity) > ATTITUDE_HOLD_RATE_RAD_S:
                return "rate"
            case _ if alignment > ATTITUDE_HOLD_RAD:
                return "alignment"
            case _:
                return "off"

    def step(self, obs, rows, now, dt, to_body=None):
        state = self.state
        policy = {**DEFAULT_POLICY, **state.setdefault("policy", {})}
        position, velocity, target, body_x = (
            np.asarray(obs[key], float) for key in ("position", "velocity", "target", "body_x")
        )
        max_accel, max_angular = float(obs["max_accel"]), float(obs["max_angular"])
        radius = float(policy.get("arrival_radius_m", ARRIVAL_RADIUS_M))
        damage = float(obs["damage"])
        history = record_shuttle_state(state, now, position)
        rows = reconstruct_measurements(rows, history, position)
        tracks = update_estimates(state, rows, position, velocity, now, policy)

        hit, energy, change = collision_check(state, velocity, damage, dt, policy)
        collision = active_collision(state, now)
        if hit:
            collision = collision_route(
                state,
                position,
                velocity,
                target,
                body_x,
                rows,
                tracks,
                now,
                energy,
                change,
                max_accel,
                policy,
            )
        self.collision_mode = self._collision_mode(collision, now)
        remaining = float(np.linalg.norm(target - position))
        steer_target = (
            detour_target(target, collision)
            if collision is not None and remaining > policy["coast_range_m"]
            else target
        )
        state["steer_target"] = steer_target

        plan_active = bool(policy["plan_steering"] and policy["avoidance"] == "mppi")
        if plan_active:
            desired_accel = plan(
                state,
                position,
                velocity,
                target,
                body_x,
                float(obs["radius"]),
                max_accel,
                dt,
                float(obs["fuel"]),
                damage,
                float(obs["mass"]),
                now,
            )
        else:
            prepare_cruise(
                state, max(0.0, remaining - radius), max_accel, damage, float(obs["mass"]), policy
            )
            desired_accel = np.zeros(3)
        v_des = cruise_velocity(
            position, steer_target, state.get("cruise_speed_mps", CRUISE_SPEED_MPS)
        )
        steering = bool(
            plan_active and state.get("coast_risk", 0.0) > policy["plan_risk_threshold"]
        )
        avoider = policy["avoidance"] == "avoider"
        match policy["avoidance"]:
            case "avoider":
                nominal = avoid_velocity(v_des, tracks, position, velocity, state, policy)
            case _:
                nominal = v_des
                if not steering:
                    desired_accel = np.zeros(3)

        match policy["governor"]:
            case "proportional" if not np.any(desired_accel):
                acceleration, burning = proportional_govern(
                    nominal, velocity, state, max_accel, policy
                )
            case _:
                acceleration, burning = govern(
                    desired_accel, nominal, velocity, state.get("burning", False), max_accel
                )

        emergency_accel, emergency_active = emergency_guard(
            velocity,
            rows,
            now,
            max_accel,
            policy,
            tracks if policy["guard_source"] == "tracks" else None,
            position,
        )
        self.guard_mode = GuardMode.ESCAPE if emergency_active else GuardMode.CLEAR
        terminal = bool(
            remaining - radius <= policy["coast_range_m"]
            and np.linalg.norm(velocity) >= policy["coast_speed_mps"]
        )
        acceleration, burning, self.phase, source = self._arbitrate(
            collision,
            self.collision_mode,
            self.guard_mode,
            terminal,
            acceleration,
            burning,
            emergency_accel,
            max_accel,
            steering,
            avoider,
        )
        state["burning"] = burning

        desired = unit(
            collision["side"] if self.collision_mode is CollisionMode.ESCAPE else nominal
        )
        alignment = (
            np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0)))
            if np.linalg.norm(desired) > 1e-9
            else 0.0
        )
        if (
            policy["main_engine_gate_rad"] > 0.0
            and burning
            and self.guard_mode is not GuardMode.ESCAPE
            and self.collision_mode is not CollisionMode.ESCAPE
            and alignment > policy["main_engine_gate_rad"]
        ):
            acceleration = np.zeros(3)
        angular_velocity = np.asarray(obs["angular_velocity"], float)
        gate = self._gate(self.phase, burning, acceleration, alignment, angular_velocity)
        trace = {}
        if policy["attitude_control"]:
            angular = attitude(body_x, angular_velocity, desired, max_angular, dt, state, trace)
        else:
            angular = np.zeros(3)
            state["attitude_hold"] = False
            trace.update(
                angle_rad=0.0,
                angle_deg=0.0,
                rate_rad_s=0.0,
                desired_rate_rad_s=0.0,
                rate_error_rad_s=0.0,
                command_rad_s2=[0.0] * 3,
                command_norm_rad_s2=0.0,
                saturated=False,
                hold=False,
                released=False,
            )
        if gate == "off":
            angular = np.zeros(3)
            trace.update(command_rad_s2=[0.0] * 3, command_norm_rad_s2=0.0, saturated=False)
        to_body = to_body or (lambda value: value)
        body_accel = np.asarray(to_body(acceleration), float)
        body_angular = np.asarray(to_body(angular), float)
        record = telemetry(
            now,
            "burn" if burning else "coast",
            gate,
            body_x,
            desired,
            acceleration,
            body_accel,
            trace,
            float(np.linalg.norm(velocity)),
            remaining,
            float(obs["fuel"]),
            damage,
            len(rows),
            emergency_active,
        )
        record.update(
            collision=bool(collision),
            collision_detour=bool(
                collision is not None
                and self.collision_mode is CollisionMode.DETOUR
                and remaining > policy["coast_range_m"]
            ),
            collision_hits=int(state.get("collision_hits", 0)),
            collision_impact_j=finite(state.get("last_impact_j", 0.0)),
            collision_impact_mps=finite(state.get("last_impact_mps", 0.0)),
            collision_offset_m=finite(collision["offset"]) if collision is not None else None,
            collision_side_clearance_m=(
                finite(collision.get("side_score")) if collision is not None else None
            ),
            collision_side_other_m=(
                finite(collision.get("side_other")) if collision is not None else None
            ),
            collision_side_reason=collision.get("side_reason") if collision is not None else None,
        )
        record.update(
            tracker=policy["tracker"],
            guard_source=policy["guard_source"],
            avoidance=policy["avoidance"],
            governor=policy["governor"],
            estimate_count=len(tracks),
            phase=self.phase.value,
            source=source,
            guard_mode=self.guard_mode.value,
            collision_mode=self.collision_mode.value,
        )
        record["line"] += (
            f" phase={self.phase.value} src={source}"
            f" coll={int(record['collision'])} hits={record['collision_hits']}"
            f" off={record['collision_offset_m'] or 0.0:.0f}"
        )
        state["last_accel"] = acceleration.copy()
        return body_accel, body_angular, record


def step(state, obs, rows, now, dt, to_body=None):
    """Compatibility entrypoint for callers that keep the run state in a dict."""
    return GuidanceController(state=state).step(obs, rows, now, dt, to_body)


# END ALI MPPI CORE

t0 = (
    st.SimGlobals.GetSimEntity()
    .GetParam(st.VarType.timestamp, ["Time", "StartDateTAI"])
    .as_datetime()
)
loop_hz = this.GetParam(st.VarType.double, "LoopFreqHz")
state = {
    "burning": False,
    "attitude_hold": False,
    "last_control": np.zeros(3),
    "rng": np.random.default_rng(12345),
    "policy": {
        "time_budget_s": 2.5 * DEFAULT_SCORE["nominal_time_s"],
        "arrival_radius_m": ARRIVAL_RADIUS_M,
    },
}
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
    body_x = np.asarray(
        st.frames.FramedVector(np.array([1.0, 0.0, 0.0]), shuttleFrame).ExprIn(phobosFrame), float
    )
    mass = float(shuttle.getMass())
    mass = mass if np.isfinite(mass) and mass > 0.0 else 3388.7
    obs = {
        "position": position,
        "velocity": velocity,
        "target": target_position,
        "body_x": body_x,
        "angular_velocity": np.asarray(shuttle.getAngVelocity().WRT_ExprIn(phobosFrame), float),
        "fuel": float(shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"])),
        "damage": float(shuttle.GetParam(st.VarType.double, ["Stats", "Damage"])),
        "radius": float(shuttle.GetParam(st.VarType.double, ["Graphics", "VisualRadius_m"])),
        "max_accel": float(shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAccel_m_s2"])),
        "max_angular": float(
            shuttle.GetParam(st.VarType.double, ["Thrusters", "MaxAngAccel_rad_s2"])
        ),
        "mass": mass,
    }
    accel_body, angular_body, record = step(
        state,
        obs,
        GetNearestDebrisLocations(80),
        now,
        dt,
        lambda value: np.asarray(
            st.frames.FramedVector(value, phobosFrame).ExprIn(shuttleFrame), float
        ),
    )
    SetCommand(accel_body, angular_body)
    if now - last_status >= 0.5:
        last_status = now
        st.logger_info("MPPI " + json.dumps(record, separators=(",", ":"), allow_nan=False))
    if now - last_display >= 1.0:
        last_display = now
        st.OnScreenLogMessage(record["line"], "MPPI", st.Severity.Info)
    if record["warning"] is not None and now - last_alert >= 5.0:
        last_alert = now
        st.OnScreenAlert(
            f"MPPI warning {record['warning']} " + record["line"], "MPPI", st.Severity.Info
        )
