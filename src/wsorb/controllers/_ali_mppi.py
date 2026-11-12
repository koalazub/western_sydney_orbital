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
SHIP_RADIUS_M = 3.0


class AliMppiController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.math = load_math("ali_mppi")["core"]
        self.state = {
            "rocks": [],
            "burning": False,
            "last_control": np.zeros(3),
            "rng": np.random.default_rng(12345),
        }
        self.last_time = None

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
        self.last_time = now
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
        desired_accel = self.math.plan(
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
            float(sample.mass_kg),
            now,
        )
        nominal_velocity = self.math.cruise_velocity(
            np.asarray(sample.position_world_m), np.asarray(sample.target_position_world_m)
        )
        acceleration, self.state["burning"] = self.math.govern(
            desired_accel,
            nominal_velocity,
            np.asarray(sample.velocity_world_mps),
            self.state["burning"],
            MAX_ACCEL_MPS2,
        )
        if not np.all(np.isfinite(acceleration)):
            raise _Rejected("MPPI produced a non-finite acceleration")
        desired = self.math.unit(nominal_velocity)
        alignment = (
            np.arccos(float(np.clip(body_x @ desired, -1.0, 1.0)))
            if np.linalg.norm(desired) > 1e-9
            else 0.0
        )
        angular_velocity = np.asarray(sample.angular_velocity_world_rad_s)
        gate = "off"
        if self.state["burning"]:
            gate = "burn"
        elif np.linalg.norm(angular_velocity) > self.math.RATE_DEADBAND_RAD_S:
            gate = "rate"
        elif alignment > self.math.ATTITUDE_DEADBAND_RAD:
            gate = "alignment"
        attitude_trace = {}
        angular = self.math.attitude(
            body_x,
            angular_velocity,
            desired,
            COMMAND_CAP_ANGULAR,
            dt,
            attitude_trace,
        )
        if gate == "off":
            angular = np.zeros(3)
            attitude_trace["command_rad_s2"] = [0.0, 0.0, 0.0]
            attitude_trace["command_norm_rad_s2"] = 0.0
            attitude_trace["saturated"] = False
        body = bounded(
            reference_to_body(acceleration, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_MPS2,
        )
        angular_body = bounded(
            reference_to_body(angular, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_ANGULAR,
        )
        velocity_error = np.linalg.norm(nominal_velocity - np.asarray(sample.velocity_world_mps))
        target_range = float(
            np.linalg.norm(
                np.asarray(sample.target_position_world_m) - np.asarray(sample.position_world_m)
            )
        )
        record = self.math.telemetry_record(
            now,
            dt,
            "burn" if self.state["burning"] else "coast",
            gate,
            body_x,
            desired,
            acceleration,
            body,
            angular_velocity,
            attitude_trace,
            float(velocity_error),
            float(np.linalg.norm(np.asarray(sample.velocity_world_mps))),
            target_range,
            len(self.state["rocks"]),
            fuel,
            float(sample.damage_j),
        )
        self._report["guidance"] = {
            "track_count": len(self.state["rocks"]),
            "mppi_samples": self.math.MPPI_SAMPLES,
            "mppi_steps": self.math.MPPI_STEPS,
            "mode": "burn" if self.state["burning"] else "coast",
            "cruise_speed_mps": self.math.CRUISE_SPEED_MPS,
            "velocity_error_mps": velocity_error,
            "alignment_angle_rad": alignment,
            "accel_reference_mps2": acceleration.tolist(),
            "angular_accel_body_rad_s2": angular_body.tolist(),
            "fuel_per_delta_v_kg_per_mps": float(sample.mass_kg) / (80.0 * 9.8),
            "capsule_radius_m": SHIP_RADIUS_M,
            "model": "3-DOF translation MPPI, capsule clearance, burn/coast governor",
            "telemetry": record,
        }
        return body, angular_body
