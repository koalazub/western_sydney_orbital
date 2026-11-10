"""Local adapter for the compact MPPI candidate."""

from __future__ import annotations

import numpy as np

from ..control_contract import body_to_reference, bounded, reference_to_body
from ._core import ExperimentalController, _Rejected
from ._sources import load_math

MAX_ACCEL_MPS2 = 0.5
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
COMMAND_CAP_MPS2 = MAX_ACCEL_MPS2 - 4.0 * np.spacing(MAX_ACCEL_MPS2)
COMMAND_CAP_ANGULAR = MAX_ANGULAR_ACCEL_RAD_S2 - 4.0 * np.spacing(MAX_ANGULAR_ACCEL_RAD_S2)
SHIP_RADIUS_M = 2.0


class AliMppiController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.math = load_math("ali_mppi")["core"]
        self.state = {"rocks": []}
        self.last_time = None
        self.last_fuel = 0.0
        self.fuel_rate = 0.0

    def _calculate(self, sample, rows, now):
        try:
            return self._functional_decision(sample, rows, now)
        except _Rejected:
            raise
        except Exception as error:
            raise _Rejected(f"MPPI calculation failed: {type(error).__name__}") from None

    def _functional_decision(self, sample, rows, now):
        dt = 0.1 if self.last_time is None else max(1e-3, now - self.last_time)
        fuel = float(sample.fuel_used_kg)
        if self.last_time is not None and now > self.last_time:
            observed = max(0.0, (fuel - self.last_fuel) / (now - self.last_time))
            self.fuel_rate = 0.8 * self.fuel_rate + 0.2 * observed
        self.last_time, self.last_fuel = now, fuel
        selected = np.asarray(rows, dtype=float)[:80]
        for epoch in np.unique(selected[:, 2, 0]):
            group = selected[selected[:, 2, 0] == epoch]
            position_at, _ = self.history.at(float(epoch))
            self.state = self.math.update_belief(self.state, group, position_at, float(epoch))
        self.state = self.math.update_belief(
            self.state, np.empty((0, 3, 3)), sample.position_world_m, now
        )
        body_x = body_to_reference(
            np.array([1.0, 0.0, 0.0]),
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        acceleration = self.math.plan(
            self.state,
            sample.position_world_m,
            sample.velocity_world_mps,
            sample.target_position_world_m,
            body_x,
            SHIP_RADIUS_M,
            MAX_ACCEL_MPS2,
            dt,
            fuel,
            float(sample.damage_j),
            self.fuel_rate,
            now,
        )
        if not np.all(np.isfinite(acceleration)):
            raise _Rejected("MPPI produced a non-finite acceleration")
        angular = (
            self.math.attitude(
                body_x,
                sample.angular_velocity_world_rad_s,
                self.math.unit(acceleration),
                COMMAND_CAP_ANGULAR,
                dt,
            )
            if np.linalg.norm(acceleration) > 0.0
            else np.zeros(3)
        )
        body = bounded(
            reference_to_body(acceleration, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_MPS2,
        )
        angular_body = bounded(
            reference_to_body(angular, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_ANGULAR,
        )
        self._report["guidance"] = {
            "track_count": len(self.state["rocks"]),
            "mppi_samples": self.math.MPPI_SAMPLES,
            "mppi_steps": self.math.MPPI_STEPS,
            "accel_reference_mps2": acceleration.tolist(),
            "angular_accel_body_rad_s2": angular_body.tolist(),
            "fuel_rate_kg_s": self.fuel_rate,
            "capsule_radius_m": SHIP_RADIUS_M,
            "model": "3-DOF translation MPPI, capsule clearance, time-optimal pivot",
        }
        return body, angular_body
