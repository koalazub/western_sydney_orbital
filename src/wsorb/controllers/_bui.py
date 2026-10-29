"""Bui's real math with a pre-assessment 0.25 hold + 0.25 sidestep profile."""

from __future__ import annotations

from functools import lru_cache
from threading import RLock

import numpy as np

from ..control_contract import bounded, reference_to_body
from ._core import ExperimentalController, _Rejected
from ._sources import load_math

_PROFILE_LOCK = RLock()


@lru_cache(maxsize=1)
def _profile():
    modules = load_math("bui")
    accel = modules["accel_controller"]
    trajectory = modules["trajectory"]
    guidance = modules["guidance"]
    avoidance = modules["avoidance"]
    original_propagate = trajectory.propagate
    original_offset = accel.maneuver_offset

    def propagate(p0, v0, ts, v_des=None, along=None, burns=(), t_now=0.0):
        # The hold acts only along vhat while avoiding. Sidestep burns are
        # perpendicular, so their exact piecewise-constant contribution adds to
        # the author's original hold integrator without changing its hold law.
        p, v = original_propagate(p0, v0, ts, v_des=v_des, along=along, t_now=t_now)
        ts = np.asarray(ts, dtype=float)
        for burn in burns:
            start, end = max(float(burn.t0), t_now), max(float(burn.t1), t_now)
            duration = max(0.0, end - start)
            active = np.clip(t_now + ts - start, 0.0, duration)
            coast = np.maximum(t_now + ts - end, 0.0)
            p += (0.5 * active**2 + duration * coast)[:, None] * burn.accel
            v += active[:, None] * burn.accel
        return p, v

    def maneuver_offset(ts, a_dir, a_mag, tA, v_lat):
        off, tB, dB = original_offset(ts, a_dir, a_mag, tA, v_lat)
        # The author's planner omits burn B when <= 0.05 s. Assess that same
        # omitted-burn path BEFORE the source planner ranks its candidates.
        omitted = tB <= 0.05
        if np.any(omitted):
            times = np.asarray(ts)[None, :, None]
            duration = tA[omitted, None, None]
            a = (a_mag[omitted, None] * a_dir[omitted])[:, None, :]
            active = np.clip(times, 0.0, duration)
            off[omitted] = a * (0.5 * active**2 + duration * np.maximum(times - duration, 0.0))
        return off, tB, dB

    trajectory.propagate = guidance.propagate = propagate
    accel.maneuver_offset = avoidance.maneuver_offset = maneuver_offset

    class ProjectedHold(accel.VelocityHold):
        def __init__(self, owner):
            super().__init__()
            self.owner = owner

        def step(self, velocity):
            if self.owner.mode == "AVOID":
                # Project the error BEFORE the source hold caps it. This is the
                # same law as trajectory.propagate, unlike the original loop's
                # cap-first-then-project execution.
                along = self.owner.vhat0
                error = self.v_des - velocity
                velocity = self.v_des - float(error @ along) * along
            return super().step(velocity)

    class BoundedGuidance(guidance.Guidance):
        def __init__(self):
            super().__init__()
            self.vhold = ProjectedHold(self)

        def _plan(self, *args, **kwargs):
            adopted = super()._plan(*args, **kwargs)
            if adopted:
                burns = self.seq.burns
                for index, burn in enumerate(burns):
                    if np.linalg.norm(burn.accel) > 0.25:
                        raise ValueError("Sidestep exceeds the reviewed profile")
                    if any(burn.t0 < other.t1 and other.t0 < burn.t1 for other in burns[:index]):
                        raise ValueError("Sidestep burns must not overlap")
            return adopted

    class NormReorienter(modules["attitude"].Reorienter):
        def command(self, target_dir_body, angvel_body):
            return bounded(super().command(target_dir_body, angvel_body), 0.05)

    class ExpiringTracks(modules["state_estimator"].DebrisTracks):
        def expire(self, now):
            keep = now - self.last_seen <= modules["config"].MAX_COAST_S
            for field in ("p", "v", "n", "last_seen", "diam", "ids"):
                setattr(self, field, getattr(self, field)[keep])

    return modules, BoundedGuidance, NormReorienter, ExpiringTracks


class BuiController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        with _PROFILE_LOCK:
            self.modules, guidance, reorienter, tracks = _profile()
        self.shuttle = self.modules["state_estimator"].ShuttleState()
        self.tracks = tracks()
        self.guidance = guidance()
        self.reorienter = reorienter()

    def _observe(self, sample):
        self.shuttle.update(
            sample.elapsed_time_s, sample.position_world_m, sample.velocity_world_mps
        )

    def _ingest(self, epoch, rows):
        if not self.history.covers(epoch):
            raise _Rejected("Observer history does not cover a radar row epoch")
        # Every call contains one epoch, so the author's max(row time) cannot
        # retime older rows. ShuttleState.at interpolates only covered history.
        self.tracks.ingest(list(rows), self.shuttle)

    def _expire(self, now):
        self.tracks.expire(now)

    def _calculate(self, sample, rows, now):
        if not self._track_rows(rows, limit=500, now=now):
            raise _Rejected("Waiting for actual observer history at radar row epochs")
        snapshot = self.tracks.snapshot(sample.elapsed_time_s)
        accel_ref, telemetry = self.guidance.update(
            sample.elapsed_time_s, self.shuttle, snapshot, sample.target_position_world_m
        )
        body = reference_to_body(accel_ref, sample.quaternion_xyzw, sample.rotation_convention)
        target_body = reference_to_body(
            sample.target_position_world_m - sample.position_world_m,
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        spin_body = reference_to_body(
            sample.angular_velocity_world_rad_s,
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        angular = self.reorienter.command(target_body, spin_body)
        plan = telemetry["plan"]
        self._report["guidance"] = {
            "mode": telemetry["mode"],
            "track_count": len(snapshot.pos),
            "cruise_speed_mps": 17.0,
            "hold_cap_mps2": 0.25,
            "nonoverlapping_sidestep_cap_mps2": 0.25,
            "angular_norm_cap_rad_s2": 0.05,
            "source_plan_estimated_feasible": None if plan is None else bool(plan.feasible),
            "prediction": "same projected bounded hold plus exact timed sidestep burns",
            "arrival": "author cruise has no station braking; native arrival is not docking",
        }
        return body, angular
