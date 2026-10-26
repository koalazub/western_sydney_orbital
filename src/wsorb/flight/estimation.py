"""Bounded ownship history and a direct-state-anchored constant-velocity model.

No obstacle associations are made. A radar row is never a persistent object ID,
and a radial sample never supplies tangential velocity. Uncertainty below is an
explicit model allowance, not a calibrated probability or a physics guarantee.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np

from ._validation import Vec3, distance, point, scalar, vector
from .config import ResilienceConfig


@dataclass(frozen=True, slots=True)
class NavigationSample:
    time_s: float
    position_m: Vec3
    velocity_mps: Vec3
    rotation_xyzw: tuple[float, float, float, float]

    @classmethod
    def from_public(cls, time_s: float, ship) -> NavigationSample:
        timestamp = scalar(time_s, "navigation_sample_time_s")
        position = point(ship["position"], "shuttle.position")
        velocity = point(ship["velocity"], "shuttle.velocity")
        rotation = vector(ship["rotation"], "shuttle.rotation", length=4)
        scale = float(max(abs(rotation)))
        if scale == 0.0:
            raise ValueError("Navigation rotation must be nonzero")
        rotation = rotation / scale
        rotation /= math.hypot(*rotation)
        return cls(timestamp, position, velocity, tuple(float(value) for value in rotation))


@dataclass(frozen=True, slots=True)
class StateEstimate:
    time_s: float
    source_time_s: float
    position_m: Vec3
    velocity_mps: Vec3
    rotation_xyzw: tuple[float, float, float, float]
    position_uncertainty_m: float
    velocity_uncertainty_mps: float

    def to_report(self) -> dict:
        return {
            "time_s": self.time_s,
            "source_time_s": self.source_time_s,
            "age_s": abs(self.time_s - self.source_time_s),
            "position_world_m": list(self.position_m),
            "velocity_world_mps": list(self.velocity_mps),
            "rotation_xyzw": list(self.rotation_xyzw),
            "position_uncertainty_m": self.position_uncertainty_m,
            "velocity_uncertainty_mps": self.velocity_uncertainty_mps,
            "input_kind": "unreplicated_direct_public_navigation_state",
            "model": "constant_velocity_between_genuinely_new_navigation_samples",
            "uncertainty_kind": "model_allowances_not_calibrated_sensor_confidence",
            "attitude_model": "last_source_rotation_no_attitude_prediction",
        }


class StateEstimator:
    """Correct from new direct-state reports; never count polling as new data."""

    def __init__(self, config: ResilienceConfig) -> None:
        self.config = config
        self._history: deque[NavigationSample] = deque(maxlen=config.history_limit)
        self.accel_allowance_mps2 = config.model_accel_allowance_mps2
        self.last_innovation_m: float | None = None
        self.last_velocity_innovation_mps: float | None = None

    @property
    def history(self) -> tuple[NavigationSample, ...]:
        return tuple(self._history)

    def correct(self, sample: NavigationSample) -> bool:
        if self._history:
            previous = self._history[-1]
            if sample.time_s < previous.time_s:
                raise ValueError("Navigation source clock rewound")
            if sample.time_s == previous.time_s:
                if sample != previous:
                    raise ValueError("Navigation changed without a new source timestamp")
                return False
            predicted = self.estimate(sample.time_s)
            self.last_innovation_m = distance(sample.position_m, predicted.position_m)
            self.last_velocity_innovation_mps = distance(
                sample.velocity_mps, predicted.velocity_mps
            )
        self._history.append(sample)
        while len(self._history) > 1 and (
            sample.time_s - self._history[0].time_s > self.config.history_window_s
        ):
            self._history.popleft()
        return True

    def _project(self, sample: NavigationSample, now: float) -> StateEstimate:
        elapsed = now - sample.time_s
        age = abs(elapsed)
        position = point(
            tuple(
                p + elapsed * v for p, v in zip(sample.position_m, sample.velocity_mps, strict=True)
            ),
            "estimated position",
        )
        position_uncertainty = (
            self.config.position_allowance_m
            + self.config.velocity_allowance_mps * age
            + 0.5 * self.accel_allowance_mps2 * age * age
        )
        velocity_uncertainty = self.config.velocity_allowance_mps + self.accel_allowance_mps2 * age
        return StateEstimate(
            now,
            sample.time_s,
            position,
            sample.velocity_mps,
            sample.rotation_xyzw,
            scalar(position_uncertainty, "position uncertainty"),
            scalar(velocity_uncertainty, "velocity uncertainty"),
        )

    def estimate(self, time_s: float) -> StateEstimate:
        now = scalar(time_s, "estimate time_s")
        if not self._history:
            raise ValueError("No genuine navigation sample")
        return self._project(self._history[-1], now)

    def at(self, time_s: float) -> StateEstimate | None:
        """Use an original ownship sample at/before radar time, never row identity.

        No earlier sample means no historical anchor. Callers must then retain
        their existing backdating allowance, not invent measurement-time state.
        """
        timestamp = scalar(time_s, "historical time_s")
        source = next(
            (sample for sample in reversed(self._history) if sample.time_s <= timestamp), None
        )
        return None if source is None else self._project(source, timestamp)

    def uncertainty_within_limits(self, estimate: StateEstimate) -> bool:
        return (
            estimate.position_uncertainty_m <= self.config.max_position_uncertainty_m
            and estimate.velocity_uncertainty_mps <= self.config.max_velocity_uncertainty_mps
        )

    def backdating_allowance(
        self,
        measurement_time_s: float,
        observation_time_s: float,
        raw_position: Vec3,
        raw_velocity: Vec3,
        horizon_s: float,
    ) -> float:
        """Enclose a history-anchored alternative without moving the old envelope.

        The existing predictor uses current raw velocity for observer backdating
        and radial ego-motion removal. Inflate by their disagreement with actual
        timestamped ownship history, including that history's model uncertainty.
        This adds no debris velocity components and never rewrites radar rows.
        """
        historical = self.at(measurement_time_s)
        if historical is None:
            return 0.0
        age = observation_time_s - measurement_time_s
        assumed_then = point(
            np.array(raw_position) - age * np.array(raw_velocity), "backdated observer"
        )
        interval = age + horizon_s
        return scalar(
            distance(historical.position_m, assumed_then)
            + historical.position_uncertainty_m
            + interval
            * (
                distance(historical.velocity_mps, raw_velocity)
                + historical.velocity_uncertainty_mps
            ),
            "history backdating allowance",
        )
