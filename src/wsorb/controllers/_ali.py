"""Functional radar MVP adapter for Ali's reviewed local profile."""

from __future__ import annotations

import numpy as np

from ..control_contract import body_to_reference, bounded, reference_to_body
from ._core import ExperimentalController, _Rejected
from ._sources import load_math

MAX_ACCEL_MPS2 = 0.5
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05
COMMAND_CAP_MPS2 = MAX_ACCEL_MPS2 - 4.0 * np.spacing(MAX_ACCEL_MPS2)
COMMAND_CAP_ANGULAR = MAX_ANGULAR_ACCEL_RAD_S2 - 4.0 * np.spacing(MAX_ANGULAR_ACCEL_RAD_S2)


class AliMvpController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.math = load_math("ali")["core"]
        self.state = self.math.initial_state()

    def _calculate(self, sample, rows, now):
        try:
            return self._functional_decision(sample, rows, now)
        except _Rejected:
            raise
        except Exception as error:
            raise _Rejected(f"Radar MVP calculation failed: {type(error).__name__}") from None

    def _functional_decision(self, sample, rows, now):
        selected = rows[:50]
        for epoch in np.unique(selected[:, 2, 0]):
            group = selected[selected[:, 2, 0] == epoch]
            position, _ = self.history.at(float(epoch))
            self.state["rocks"] = self.math.update_tracker(
                self.state["rocks"], group, position, float(epoch)
            )
        self.state["rocks"] = self.math.update_tracker(
            self.state["rocks"], [], sample.position_world_m, now
        )
        self.state, safe_velocity, emergency = self.math.decide(
            self.state,
            sample.position_world_m,
            sample.velocity_world_mps,
            sample.target_position_world_m,
            selected,
            now,
        )
        acceleration = self.math.controlled_acceleration(
            safe_velocity, sample.velocity_world_mps, COMMAND_CAP_MPS2
        )
        if not np.all(np.isfinite(acceleration)) or not np.all(np.isfinite(safe_velocity)):
            raise _Rejected("Radar MVP produced a non-finite guidance vector")
        body_x_world = body_to_reference(
            np.array([1.0, 0.0, 0.0]),
            sample.quaternion_xyzw,
            sample.rotation_convention,
        )
        pivot_active = False
        if not self.state["pivot_done"] and np.linalg.norm(acceleration) > 0.0:
            desired_direction = self.math.unit(acceleration)
            angular_world, alignment_angle = self.math.attitude_acceleration(
                body_x_world,
                sample.angular_velocity_world_rad_s,
                desired_direction,
                COMMAND_CAP_ANGULAR,
            )
            still_rotating = (
                np.linalg.norm(sample.angular_velocity_world_rad_s)
                > self.math.ATTITUDE_SETTLE_RATE_RAD_S
            )
            pivot_active = alignment_angle > self.math.ATTITUDE_DEADBAND_RAD or still_rotating
            if not pivot_active:
                self.state["pivot_done"] = True
                angular_world = np.zeros(3)
        else:
            angular_world, alignment_angle = np.zeros(3), 0.0
        body = bounded(
            reference_to_body(acceleration, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_MPS2,
        )
        angular_body = bounded(
            reference_to_body(angular_world, sample.quaternion_xyzw, sample.rotation_convention),
            COMMAND_CAP_ANGULAR,
        )
        self._report["guidance"] = {
            "track_count": len(self.state["rocks"]),
            "cruise_speed_mps": self.math.CRUISE_SPEED_MPS,
            "accel_norm_cap_mps2": MAX_ACCEL_MPS2,
            "angular_accel_cap_rad_s2": MAX_ANGULAR_ACCEL_RAD_S2,
            "selected_velocity_reference_mps": safe_velocity.tolist(),
            "accel_reference_mps2": acceleration.tolist(),
            "angular_accel_body_rad_s2": angular_body.tolist(),
            "alignment_angle_rad": alignment_angle,
            "angular_speed_world_rad_s": float(np.linalg.norm(sample.angular_velocity_world_rad_s)),
            "pivot_active": bool(pivot_active),
            "emergency_active": bool(emergency),
            "planner": "polyfit tracker, 10 s activation, maximum observed diameter plus margin",
            "emergency": "raw radar, forward 180 degrees, 150 m and 6 s lookahead",
        }
        return body, angular_body
