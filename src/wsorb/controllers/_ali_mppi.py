"""Local adapter for the compact MPPI candidate."""

from __future__ import annotations

import json
import os
import sys

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
    def __init__(self, metadata, settings=None):
        super().__init__(metadata)
        self.math = load_math("ali_mppi")["core"]
        settings = settings if isinstance(settings, dict) else {}
        policy = dict(settings)
        self.state = {
            "burning": False,
            "attitude_hold": False,
            "last_control": np.zeros(3),
            "rng": np.random.default_rng(12345),
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
        self.last_time = now
        current_position = np.asarray(sample.position_world_m, dtype=float)
        selected = np.asarray(rows, dtype=float)[:80].copy()
        if selected.ndim == 3:
            for epoch in np.unique(selected[:, 2, 0]):
                group = selected[:, 2, 0] == epoch
                position_at, _ = self.history.at(float(epoch))
                selected[group, 0] += position_at - current_position
        body_x = body_to_reference(
            np.array([1.0, 0.0, 0.0]),
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        observation = {
            "position": current_position,
            "velocity": np.asarray(sample.velocity_world_mps, dtype=float),
            "target": np.asarray(sample.target_position_world_m, dtype=float),
            "body_x": body_x,
            "angular_velocity": np.asarray(sample.angular_velocity_world_rad_s, dtype=float),
            "fuel": float(sample.fuel_used_kg),
            "damage": float(sample.damage_j),
            "mass": float(sample.mass_kg),
            "radius": SHIP_RADIUS_M,
            "max_accel": MAX_ACCEL_MPS2,
            "max_angular": MAX_ANGULAR_ACCEL_RAD_S2,
        }
        body, angular_body, record = self.math.step(
            self.state,
            observation,
            selected,
            now,
            dt,
            lambda value: reference_to_body(
                value, sample.quaternion_xyzw, sample.rotation_convention
            ),
        )
        body = bounded(body, COMMAND_CAP_MPS2)
        angular_body = bounded(angular_body, COMMAND_CAP_ANGULAR)
        record["accel_body_mps2"] = body.tolist()
        record["angular_command_rad_s2"] = angular_body.tolist()
        if os.environ.get("WSORB_MPPI_DEBUG") == "1":
            print(
                json.dumps(record, separators=(",", ":"), allow_nan=False),
                file=sys.stderr,
                flush=True,
            )
        policy = self.state.get("policy", {})
        nominal = self.math.cruise_velocity(
            current_position,
            np.asarray(sample.target_position_world_m, dtype=float),
            self.state.get("cruise_speed_mps", self.math.CRUISE_SPEED_MPS),
            policy.get("arrival_radius_m", self.math.ARRIVAL_RADIUS_M),
        )
        velocity_error = float(
            np.linalg.norm(nominal - np.asarray(sample.velocity_world_mps, dtype=float))
        )
        accel_reference = body_to_reference(
            body, sample.quaternion_xyzw, sample.rotation_convention
        )
        self._report["guidance"] = {
            "track_count": len(selected),
            "mppi_samples": self.math.MPPI_SAMPLES,
            "mppi_steps": self.math.MPPI_STEPS,
            "mode": record["mode"],
            "cruise_speed_mps": self.state.get("cruise_speed_mps", self.math.CRUISE_SPEED_MPS),
            "fuel_rate_kg_s": self.state.get("fuel_rate_kg_s"),
            "time_budget_s": policy.get("time_budget_s"),
            "velocity_error_mps": velocity_error,
            "alignment_angle_rad": record.get("target_align_rad") or 0.0,
            "accel_reference_mps2": accel_reference.tolist(),
            "angular_accel_body_rad_s2": angular_body.tolist(),
            "fuel_per_delta_v_kg_per_mps": float(sample.mass_kg) / (80.0 * 9.8),
            "capsule_radius_m": SHIP_RADIUS_M,
            "collision_hits": int(self.state.get("collision_hits", 0)),
            "collision_active": bool(self.state.get("collision")),
            "collision_impact_j": self.state.get("last_impact_j"),
            "collision_detour": bool(record.get("collision_detour", False)),
            "collision_offset_m": record.get("collision_offset_m"),
            "tracker": policy.get("tracker", "line"),
            "guard_source": policy.get("guard_source", "raw"),
            "avoidance": policy.get("avoidance", "mppi"),
            "governor": policy.get("governor", "full"),
            "risk_inflation": bool(policy.get("risk_inflation", False)),
            "fixed_samples": bool(policy.get("mppi_fixed_samples", False)),
            "attitude_control": bool(policy.get("attitude_control", True)),
            "model": "3-DOF translation MPPI, capsule clearance, burn/coast governor",
            "telemetry": record,
        }
        return body, angular_body
