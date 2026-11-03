"""Fenced-run adapters for the actual saved Ali and published Basic policies."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from ..control_contract import scalar
from ..flight.official_profile import official_basic_command
from ._core import ExperimentalController, _Rejected
from .observation import sample_from_observation
from .official_profile import official_profile_policy


class OfficialController(ExperimentalController):
    def __init__(self, metadata):
        super().__init__(metadata)
        self.policy = (
            official_profile_policy() if metadata["id"] == "ali_official_profile" else None
        )

    def _calculate(self, sample, rows, now):
        if self.policy is None:
            # Genuine numeric sample fields only. Basic reads no radar or health.
            return official_basic_command(
                {
                    "time_s": now,
                    "navigation_sample_time_s": sample.elapsed_time_s,
                    "shuttle": {
                        "position": sample.position_world_m,
                        "velocity": sample.velocity_world_mps,
                        "rotation": sample.quaternion_xyzw,
                        "angular_velocity_rad_s": sample.angular_velocity_world_rad_s,
                    },
                    "target": {"position": sample.target_position_world_m},
                    "rotation_convention": sample.rotation_convention,
                    "max_accel_mps2": 0.5,
                    "max_angular_accel_rad_s2": 0.05,
                }
            )
        observation = getattr(sample, "source_observation", None)
        if not isinstance(observation, Mapping) or observation.get("time_s") != now:
            raise _Rejected("Ali requires the exact original public observation")
        original_sample = sample_from_observation(observation, source_kind=sample.source_kind)
        if any(
            not np.array_equal(getattr(sample, name), getattr(original_sample, name))
            for name in (
                "elapsed_time_s",
                "position_world_m",
                "velocity_world_mps",
                "quaternion_xyzw",
                "angular_velocity_world_rad_s",
                "target_position_world_m",
                "mass_kg",
                "inertia_body_kg_m2",
                "fuel_used_kg",
                "damage_j",
                "frame",
                "public_context",
                "radar_sample_time_s",
                "radar_rolling",
                "navigation_sequence",
                "radar_sequence",
            )
        ) or not np.array_equal(rows, np.asarray(observation["radar"]).reshape(-1, 3, 3)):
            raise _Rejected("Ali's public observation differs from the validated source sample")
        command = self.policy(observation)
        receipt = self.policy.report
        self._report["guidance"] = receipt
        if receipt.get("can_command") is not True:
            raise _Rejected("Ali's original guidance did not approve this decision")
        until = scalar(receipt.get("decision_valid_until_s"), "Ali decision validity")
        self._report["decision_valid_until_elapsed_s"] = min(
            self._report["decision_valid_until_elapsed_s"], until
        )
        if until - now < 0.05:
            raise _Rejected("Ali's original decision has no minimum lease remaining")
        return command
