"""Existing HY-selected commands and an explicitly capped UserAPI template."""

from __future__ import annotations

import numpy as np

from ..flight.control import basic_command, coast_command
from ._core import ExperimentalController


class SimpleController(ExperimentalController):
    def __init__(self, metadata, settings):
        super().__init__(metadata)
        self.settings = dict(settings)

    def _calculate(self, sample, rows, now):
        if self.metadata["id"] == "coast":
            return coast_command()
        if self.metadata["id"] == "userapi_body_z":
            self._report["template"] = {
                "target_seeking": False,
                "original_accel_body_mps2": [0.0, 0.0, 1.0],
                "profile_accel_body_mps2": [0.0, 0.0, 0.5],
            }
            return np.array([0.0, 0.0, 0.5]), np.zeros(3)
        # These are actual numeric fields from the supplied sample, not a
        # simulated SDK object or a fabricated sensor-health receipt.
        return basic_command(
            {
                "time_s": now,
                "shuttle": {
                    "position": sample.position_world_m,
                    "velocity": sample.velocity_world_mps,
                    "rotation": sample.quaternion_xyzw,
                },
                "target": {"position": sample.target_position_world_m},
                "rotation_convention": sample.rotation_convention,
                "radar": rows,
                "max_accel_mps2": 0.5,
                "max_angular_accel_rad_s2": 0.05,
            },
            **self.settings,
        )
