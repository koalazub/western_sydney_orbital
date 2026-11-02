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
    """Explicit gains and limits; no values here are competition recommendations.

    Velocity error is in m/s, its integral in m and derivative in m/s². Thus
    ``kp`` has units 1/s, ``ki`` has units 1/s² and ``kd`` is dimensionless.
    ``integral_limit`` clamps each integral component in metres. This is not
    saturation-aware back-calculation or conditional integration.
    """

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
        """Return bounded acceleration in the common input velocity frame.

        Keep that frame fixed across updates. This loop does not rotate stored
        integral or derivative state when the vehicle turns. A flight adapter
        must convert the returned reference-frame vector to body coordinates.
        ``dt_s`` is the actual control interval. A nonpositive or nonfinite
        interval is rejected rather than silently reusing an old derivative.
        Unrepresentable arithmetic raises ValueError without changing state.
        """
        desired = vector(desired_velocity_mps, "desired_velocity_mps")
        velocity = vector(velocity_mps, "velocity_mps")
        dt = scalar(dt_s, "dt_s", positive=True)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
            error = vector(desired - velocity, "PID velocity error")
            accumulated = vector(self._integral + error * dt, "PID integral")
            raw_derivative = (
                np.zeros(3, dtype=np.float64)
                if self._previous_error is None
                else vector((error - self._previous_error) / dt, "PID raw derivative")
            )
        integral = np.clip(
            accumulated,
            -self.config.integral_limit,
            self.config.integral_limit,
        )
        # Equivalent to dt / (tau + dt), without overflowing that sum.
        if dt <= self.config.derivative_tau_s:
            ratio = dt / self.config.derivative_tau_s
            alpha = ratio / (1.0 + ratio)
        else:
            alpha = 1.0 / (1.0 + self.config.derivative_tau_s / dt)
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            derivative = vector(
                self._derivative + alpha * (raw_derivative - self._derivative),
                "PID filtered derivative",
            )
            request = (
                self.config.kp * error + self.config.ki * integral + self.config.kd * derivative
            )
        command = _bounded(request, self.config.accel_limit_mps2)
        self._integral = integral
        self._derivative = derivative
        self._previous_error = error
        return command
