"""Bounded PID velocity control from the 26 and 28 October minutes.

The minutes describe proportional, integral and derivative control but do not
select gains. This module therefore has no tuned default policy: callers must
provide gains, and the existing flight controller remains unchanged. The
implementation includes integral clamping, a first-order derivative filter and
the native acceleration cap so the research path cannot emit an unbounded burn.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from ._validation import scalar, vector
from .control import _bounded


@dataclass(frozen=True, slots=True)
class PIDConfig:
    """Explicit gains and limits; no values here are competition recommendations."""

    kp: float
    ki: float = 0.0
    kd: float = 0.0
    integral_limit: float = 0.5
    derivative_tau_s: float = 0.05
    accel_limit_mps2: float = 0.5

    def __post_init__(self) -> None:
        for name in ("kp", "ki", "kd"):
            object.__setattr__(self, name, scalar(getattr(self, name), name))
        object.__setattr__(
            self,
            "integral_limit",
            scalar(self.integral_limit, "integral_limit", positive=True),
        )
        object.__setattr__(
            self,
            "derivative_tau_s",
            scalar(self.derivative_tau_s, "derivative_tau_s", positive=True),
        )
        object.__setattr__(
            self,
            "accel_limit_mps2",
            scalar(self.accel_limit_mps2, "accel_limit_mps2", positive=True),
        )
        if self.accel_limit_mps2 > 0.5:
            raise ValueError("accel_limit_mps2 cannot exceed the native 0.5 m/s² cap")


class PIDVelocityController:
    """One stateful PID loop; reset or construct fresh between trials."""

    def __init__(self, config: PIDConfig) -> None:
        if not isinstance(config, PIDConfig):
            raise ValueError("config must be a PIDConfig")
        self.config = config
        self.reset()

    def reset(self) -> None:
        self._integral = np.zeros(3, dtype=np.float64)
        self._derivative = np.zeros(3, dtype=np.float64)
        self._previous_error: npt.NDArray[np.float64] | None = None

    def update(
        self,
        desired_velocity_mps: npt.ArrayLike,
        velocity_mps: npt.ArrayLike,
        dt_s: float,
    ) -> npt.NDArray[np.float64]:
        """Return a bounded body-frame acceleration request.

        ``dt_s`` is the actual control interval. A nonpositive or nonfinite
        interval is rejected rather than silently reusing an old derivative.
        """
        desired = vector(desired_velocity_mps, "desired_velocity_mps")
        velocity = vector(velocity_mps, "velocity_mps")
        dt = scalar(dt_s, "dt_s", positive=True)
        error = desired - velocity
        integral = np.clip(
            self._integral + error * dt,
            -self.config.integral_limit,
            self.config.integral_limit,
        )
        if self._previous_error is None:
            raw_derivative = np.zeros(3, dtype=np.float64)
        else:
            raw_derivative = (error - self._previous_error) / dt
        alpha = dt / (self.config.derivative_tau_s + dt)
        derivative = self._derivative + alpha * (raw_derivative - self._derivative)
        request = self.config.kp * error + self.config.ki * integral + self.config.kd * derivative
        self._integral = integral
        self._derivative = derivative
        self._previous_error = error
        return _bounded(request, self.config.accel_limit_mps2)
