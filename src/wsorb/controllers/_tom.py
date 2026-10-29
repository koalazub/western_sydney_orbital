"""Tom's tracker/candidates/governor with bounded-transition candidate assessment."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from ..control_contract import bounded, reference_to_body
from ._core import ExperimentalController, _Rejected
from ._sources import load_math


def _batch_governor(target, velocity, firing, governor):
    """Vectorized prediction of the source governor's exact hysteresis and cap.

    Execution calls the real source BurnGovernor.accel. This batched form lets
    every candidate start from the real current velocity and firing state.
    """
    error = target - velocity
    magnitude = np.linalg.norm(error, axis=1)
    firing = np.where(firing, magnitude >= governor.stop_err, magnitude > governor.start_err)
    accel = governor.gain * error
    lengths = np.linalg.norm(accel, axis=1)
    accel *= (np.minimum(1.0, governor.maximum / np.maximum(lengths, 1e-300)) * firing)[:, None]
    return accel, firing


def _candidate_clearances(candidates, snapshot, velocity, governor, avoider):
    """Sweep bounded-governor chords, with acceleration sag allowance per chord.

    This replaces the old 0.5 s wait followed by an instantaneous velocity jump.
    It remains a sampled constant-debris-velocity estimate with capped source
    uncertainty, not a proof of clearance from all debris or terrain.
    """
    count = len(candidates)
    position = np.zeros((count, 3))
    vel = np.broadcast_to(velocity, (count, 3)).copy()
    firing = np.full(count, governor.firing, dtype=bool)
    clearance = np.full(count, np.inf)
    grid = np.linspace(0.0, avoider.horizon, int(np.ceil(avoider.horizon / 0.25)) + 1)
    p_rel, v_abs = snapshot["p_rel"], snapshot["v_abs"]
    for t0, t1 in zip(grid[:-1], grid[1:], strict=True):
        dt = t1 - t0
        accel, firing = _batch_governor(candidates, vel, firing, governor)
        next_position = position + vel * dt + 0.5 * accel * dt * dt
        r0 = p_rel[:, None, :] + t0 * v_abs[:, None, :] - position[None, :, :]
        r1 = p_rel[:, None, :] + t1 * v_abs[:, None, :] - next_position[None, :, :]
        delta = r1 - r0
        denominator = np.einsum("mni,mni->mn", delta, delta)
        fraction = np.clip(
            -np.einsum("mni,mni->mn", r0, delta) / np.maximum(denominator, 1e-300), 0.0, 1.0
        )
        distance = np.linalg.norm(r0 + fraction[..., None] * delta, axis=2)
        inflation = avoider.k_sigma * (snapshot["sigma_p"] + snapshot["sigma_v"] * t1)
        radius = (
            snapshot["diameter"] / 2 + avoider.margin + np.minimum(inflation, avoider.max_inflate)
        )
        sag = np.linalg.norm(accel, axis=1) * dt * dt / 8.0
        clearance = np.minimum(clearance, (distance - radius[:, None] - sag[None, :]).min(axis=0))
        position = next_position
        vel += accel * dt
    return clearance


@lru_cache(maxsize=1)
def _profile():
    modules = load_math("tom")
    source = modules["avoidance"]

    class BoundedGovernor(source.BurnGovernor):
        # Apply the same headroom in prediction and execution, before ranking,
        # so an orthogonal reference-to-body transform cannot cross the cap.
        maximum = 0.5 * (1.0 - 8.0 * np.finfo(float).eps)

        def __init__(self):
            super().__init__(gain=2.0)

        def accel(self, v_cmd, vel):
            return bounded(super().accel(v_cmd, vel), self.maximum)

    class BoundedAvoider(source.Avoider):
        def __init__(self, governor):
            super().__init__()
            self.governor = governor

        def assess(self, candidates, snapshot, velocity):
            return _candidate_clearances(candidates, snapshot, velocity, self.governor, self)

        def pick(self, v_des, snap, vel=None):
            v_des = np.asarray(v_des, float)
            vel = v_des.copy() if vel is None else np.asarray(vel, float)
            if np.linalg.norm(v_des) < 1e-3 or len(snap["id"]) == 0:
                return super().pick(v_des, snap, vel)
            # Generation is the actual source method. These are the source's
            # unchanged delta-v cost, previous-slot hysteresis and least-bad
            # fallback; only candidate assessment above changes the dynamics.
            candidates = self._candidates(v_des, vel)
            clearance = self.assess(candidates, snap, vel)
            safe = clearance > 0.0
            cost = np.linalg.norm(candidates - vel, axis=1) + self.w_aim * np.linalg.norm(
                candidates - v_des, axis=1
            )
            if safe.any():
                selected = int(np.argmin(np.where(safe, cost, np.inf)))
                if safe[2] and cost[2] <= cost[selected] + self.switch_margin:
                    selected = 2
            else:
                selected = int(np.argmax(clearance))
            self.v_prev = candidates[selected].copy()
            self.last_info = {
                "blocked": not bool(safe[0]),
                "any_safe": bool(safe.any()),
                "clearance": float(clearance[selected]),
                "dv": float(np.linalg.norm(candidates[selected] - vel)),
                "n_tracks": len(snap["id"]),
            }
            return self.v_prev.copy()

    class ExpiringTracker(modules["debris_tracker"].DebrisTracker):
        def expire(self, now):
            self.tracks = [tr for tr in self.tracks if now - tr.last_seen <= self.drop_after_s]

    return modules, BoundedGovernor, BoundedAvoider, ExpiringTracker


class TomController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.modules, governor, avoider, tracker = _profile()
        self.governor = governor()
        self.avoider = avoider(self.governor)
        self.tracker = tracker()

    def _ingest(self, epoch, rows):
        position, velocity = self.history.at(epoch)
        # One actual row epoch per update, not max(epoch) assigned to every row.
        self.tracker.update(position, velocity, list(rows))

    def _expire(self, now):
        self.tracker.expire(now)

    def _calculate(self, sample, rows, now):
        if not self._track_rows(rows, limit=50, now=now):
            raise _Rejected("Waiting for actual observer history at radar row epochs")
        snapshot = self.tracker.snapshot(
            sample.elapsed_time_s, sample.position_world_m, sample.velocity_world_mps
        )
        displacement = sample.target_position_world_m - sample.position_world_m
        distance = float(np.linalg.norm(displacement))
        v_des = 17.0 * displacement / distance if distance else np.zeros(3)
        v_cmd = self.avoider.pick(v_des, snapshot, sample.velocity_world_mps)
        accel_ref = self.governor.accel(v_cmd, sample.velocity_world_mps)
        body = reference_to_body(accel_ref, sample.quaternion_xyzw, sample.rotation_convention)
        info = self.avoider.last_info
        self._report["guidance"] = {
            "track_count": len(snapshot["id"]),
            "cruise_speed_mps": 17.0,
            "governor_gain_s": self.governor.gain,
            "accel_norm_cap_mps2": 0.5,
            "governor_firing": bool(self.governor.firing),
            "desired_velocity_reference_mps": v_des.tolist(),
            "selected_velocity_reference_mps": v_cmd.tolist(),
            "predicted_min_clearance_m": info.get("clearance"),
            "any_candidate_positive_predicted_gap": info.get("any_safe"),
            "prediction": "bounded gain-2 governor from real current velocity and firing state",
            "prediction_step_s": 0.25,
            "source_instantaneous_velocity_lag_model_used": False,
            "arrival": "author cruise has no station braking; native arrival is not docking",
        }
        return body, np.zeros(3)
