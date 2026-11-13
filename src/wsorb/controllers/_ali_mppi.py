"""Local adapter for the compact MPPI candidate."""

from __future__ import annotations

import json
import os
import sys

import numpy as np

from ..control_contract import body_to_reference, bounded
from ._core import ExperimentalController, _Rejected
from ._sources import load_math

MAX_ACCEL_MPS2 = 0.5
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
COMMAND_CAP_MPS2 = MAX_ACCEL_MPS2 - 4.0 * np.spacing(MAX_ACCEL_MPS2)
COMMAND_CAP_ANGULAR = MAX_ANGULAR_ACCEL_RAD_S2 - 4.0 * np.spacing(MAX_ANGULAR_ACCEL_RAD_S2)
SHIP_RADIUS_M = 3.0


class AliMppiController(ExperimentalController):
    def __init__(self, metadata, settings=None):
        super().__init__(metadata)
        self.math = load_math("ali_mppi")["core"]
        settings = settings if isinstance(settings, dict) else {}
        policy = {}
        for key in ("time_budget_s", "arrival_radius_m"):
            value = settings.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                policy[key] = float(value)
        self.state = {
            "rocks": [],
            "burning": False,
            "attitude_hold": False,
            "last_control": np.zeros(3),
            "rng": np.random.default_rng(12345),
            "last_velocity": None,
            "last_accel_world": None,
            "last_accel_time": None,
            "angular_velocity_world": np.zeros(3),
            "policy": policy,
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
        rotation = np.column_stack(
            [
                body_to_reference(
                    vector,
                    sample.quaternion_xyzw,
                    sample.rotation_convention,
                )
                for vector in np.eye(3)
            ]
        )
        body_x = rotation[:, 0]
        angular_velocity = np.asarray(sample.angular_velocity_world_rad_s)
        if (
            self.state.get("last_velocity") is not None
            and self.state.get("last_accel_time") is not None
        ):
            elapsed = now - self.state["last_accel_time"]
            applied = self.state.get("last_accel_world")
            if elapsed > 1e-6 and applied is not None:
                observed = (
                    np.asarray(sample.velocity_world_mps) - self.state["last_velocity"]
                ) / elapsed
                estimate = observed - np.asarray(applied, dtype=float)
                if np.all(np.isfinite(estimate)):
                    estimate = np.clip(estimate, -MAX_ACCEL_MPS2, MAX_ACCEL_MPS2)
                    if np.linalg.norm(estimate) < 0.02:
                        estimate = np.zeros(3)
                    prior = self.state.get("disturbance_world")
                    self.state["disturbance_world"] = (
                        estimate
                        if prior is None
                        else 0.95 * np.asarray(prior, dtype=float) + 0.05 * estimate
                    )
        self.state["last_velocity"] = np.asarray(sample.velocity_world_mps).copy()
        self.state["last_accel_time"] = now
        self.state["angular_velocity_world"] = angular_velocity
        accel_body, angular_body = self.math.plan_coupled(
            self.state,
            sample.position_world_m,
            sample.velocity_world_mps,
            sample.target_position_world_m,
            body_x,
            rotation,
            angular_velocity,
            SHIP_RADIUS_M,
            MAX_ACCEL_MPS2,
            COMMAND_CAP_ANGULAR,
            dt,
            fuel,
            float(sample.damage_j),
            float(sample.mass_kg),
            now,
            self.state.get("disturbance_world"),
        )
        nominal_velocity = self.math.cruise_velocity(
            np.asarray(sample.position_world_m),
            np.asarray(sample.target_position_world_m),
            self.state.get("cruise_speed_mps", self.math.CRUISE_SPEED_MPS),
            self.state.get("policy", {}).get("arrival_radius_m", self.math.ARRIVAL_RADIUS_M),
            MAX_ACCEL_MPS2,
        )
        governed_world, burning = self.math.govern(
            rotation @ accel_body,
            nominal_velocity,
            np.asarray(sample.velocity_world_mps),
            self.state["burning"],
            MAX_ACCEL_MPS2,
        )
        accel_body = rotation.T @ governed_world
        self.state["burning"] = burning
        emergency_accel, emergency_active = self.math.emergency_guard(
            np.asarray(sample.velocity_world_mps), rows, now, MAX_ACCEL_MPS2
        )
        if emergency_active:
            accel_body = rotation.T @ emergency_accel
            self.state["burning"] = True
        accel_world = rotation @ accel_body
        self.state["last_accel_world"] = accel_world.copy()
        desired = self.state.get("attitude_desired_world", self.math.unit(nominal_velocity))
        alignment = (
            float(np.arccos(np.clip(body_x @ desired, -1.0, 1.0)))
            if np.linalg.norm(desired) > 1e-9
            else 0.0
        )
        attitude_trace = dict(self.state.get("attitude_trace", {}))
        attitude_trace["applied_angular_body_rad_s2"] = [float(value) for value in angular_body]
        gate = (
            "burn"
            if self.state["burning"]
            else "hold"
            if attitude_trace.get("hold", False)
            else "off"
        )
        body = bounded(accel_body, COMMAND_CAP_MPS2)
        angular_body = bounded(angular_body, COMMAND_CAP_ANGULAR)
        self.state["last_accel_body"] = body.copy()
        self.state["last_alpha_body"] = angular_body.copy()
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
            accel_world,
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
        record["emergency"] = bool(emergency_active)
        raw_ranges = [
            float(np.linalg.norm(np.asarray(row[0], dtype=float)))
            for row in rows
            if np.all(np.isfinite(row))
        ]
        track_ranges = [
            float(
                np.linalg.norm(
                    np.asarray(rock["position"], dtype=float)
                    - np.asarray(sample.position_world_m, dtype=float)
                )
            )
            for rock in self.state["rocks"]
        ]
        record["nearest_raw_range_m"] = min(raw_ranges) if raw_ranges else None
        record["nearest_track_range_m"] = min(track_ranges) if track_ranges else None
        record["line"] = self.math.telemetry_line(record)
        if os.environ.get("WSORB_MPPI_DEBUG") == "1":
            print(
                json.dumps(record, separators=(",", ":"), allow_nan=False),
                file=sys.stderr,
                flush=True,
            )
        self._report["guidance"] = {
            "track_count": len(self.state["rocks"]),
            "mppi_samples": self.math.MPPI_SAMPLES,
            "mppi_steps": self.math.MPPI_STEPS,
            "mode": "burn" if self.state["burning"] else "coast",
            "cruise_speed_mps": self.state.get("cruise_speed_mps", self.math.CRUISE_SPEED_MPS),
            "exhaust_velocity_mps": self.state.get("exhaust_velocity_mps", 80.0 * 9.8),
            "time_budget_s": self.state.get("policy", {}).get("time_budget_s"),
            "velocity_error_mps": velocity_error,
            "alignment_angle_rad": alignment,
            "accel_reference_mps2": accel_world.tolist(),
            "accel_body_mps2": body.tolist(),
            "angular_accel_body_rad_s2": angular_body.tolist(),
            "disturbance_world_mps2": list(
                np.asarray(self.state.get("disturbance_world", np.zeros(3)), dtype=float)
            ),
            "capsule_radius_m": SHIP_RADIUS_M,
            "model": (
                "coupled body-frame translation/attitude MPPI, L1 nozzle fuel, capsule clearance"
            ),
            "telemetry": record,
        }
        return body, angular_body
