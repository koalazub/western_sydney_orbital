"""Fixed-gain alpha-beta and alpha-beta-gamma tracking from 29 October.

The meeting proposed a readable fixed-gain filter for asteroid motion because
the shuttle state is already available. No final gains, timestep or acceleration
model were agreed, and the spoken gamma example was inconsistent. This module
therefore provides the equations as a pure reference with caller-supplied
gains. It estimates no covariance and makes no claim about a validated 300 s
forecast.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from ._validation import scalar, vector


@dataclass(frozen=True, slots=True)
class AlphaBetaGains:
    """Caller-supplied dimensionless gains; not tuned competition values.

    The component bounds alone do not guarantee a stable gamma filter.
    """

    alpha: float
    beta: float
    gamma: float = 0.0

    def __post_init__(self) -> None:
        for name in ("alpha", "beta", "gamma"):
            value = scalar(getattr(self, name), name)
            if value > 1.0:
                raise ValueError(f"{name} must be in [0, 1] for this reference")
            object.__setattr__(self, name, value)


class AlphaBetaTracker:
    """Constant-velocity alpha-beta tracker for one anonymous target."""

    def __init__(
        self,
        position_m: npt.ArrayLike,
        velocity_mps: npt.ArrayLike,
        gains: AlphaBetaGains,
    ) -> None:
        if not isinstance(gains, AlphaBetaGains):
            raise ValueError("gains must be AlphaBetaGains")
        self.position_m = vector(position_m, "position_m")
        self.velocity_mps = vector(velocity_mps, "velocity_mps")
        self.gains = gains

    def update(
        self,
        measured_position_m: npt.ArrayLike,
        dt_s: float,
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        """Correct a CV prediction; rejected arithmetic leaves both states unchanged."""
        measurement = vector(measured_position_m, "measured_position_m")
        dt = scalar(dt_s, "dt_s", positive=True)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
            predicted = vector(self.position_m + self.velocity_mps * dt, "predicted position")
            residual = vector(measurement - predicted, "position residual")
            position = vector(predicted + self.gains.alpha * residual, "corrected position")
            velocity = vector(
                self.velocity_mps + self.gains.beta * residual / dt,
                "corrected velocity",
            )
        self.position_m = position
        self.velocity_mps = velocity
        return self.position_m.copy(), self.velocity_mps.copy()


class AlphaBetaGammaTracker:
    """Constant-acceleration alpha-beta-gamma tracker for one target.

    This uses the common convention ``a += 2 * gamma * residual / dt²``. The
    factor is part of this reference convention, not a validated derivation from
    the meeting transcript. With ``gamma == 0`` acceleration stays at its initial
    value. This reduces to alpha-beta only when that initial value is zero.
    A nonzero frozen acceleration still gives a constant-acceleration predictor.
    """

    def __init__(
        self,
        position_m: npt.ArrayLike,
        velocity_mps: npt.ArrayLike,
        acceleration_mps2: npt.ArrayLike,
        gains: AlphaBetaGains,
    ) -> None:
        if not isinstance(gains, AlphaBetaGains):
            raise ValueError("gains must be AlphaBetaGains")
        self.position_m = vector(position_m, "position_m")
        self.velocity_mps = vector(velocity_mps, "velocity_mps")
        self.acceleration_mps2 = vector(acceleration_mps2, "acceleration_mps2")
        self.gains = gains

    def update(
        self,
        measured_position_m: npt.ArrayLike,
        dt_s: float,
    ) -> tuple[
        npt.NDArray[np.float64],
        npt.NDArray[np.float64],
        npt.NDArray[np.float64],
    ]:
        """Correct a CA prediction; rejected arithmetic leaves all states unchanged."""
        measurement = vector(measured_position_m, "measured_position_m")
        dt = scalar(dt_s, "dt_s", positive=True)
        with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
            predicted = vector(
                self.position_m + self.velocity_mps * dt + 0.5 * self.acceleration_mps2 * dt * dt,
                "predicted position",
            )
            predicted_velocity = vector(
                self.velocity_mps + self.acceleration_mps2 * dt,
                "predicted velocity",
            )
            residual = vector(measurement - predicted, "position residual")
            position = vector(predicted + self.gains.alpha * residual, "corrected position")
            velocity = vector(
                predicted_velocity + self.gains.beta * residual / dt,
                "corrected velocity",
            )
            # Do not form dt²: it can underflow to zero for a valid tiny step.
            # Gamma zero must leave even a nonzero acceleration exactly frozen.
            acceleration = (
                self.acceleration_mps2.copy()
                if self.gains.gamma == 0.0
                else vector(
                    self.acceleration_mps2 + 2.0 * (self.gains.gamma * residual / dt) / dt,
                    "corrected acceleration",
                )
            )
        self.position_m = position
        self.velocity_mps = velocity
        self.acceleration_mps2 = acceleration
        return (
            self.position_m.copy(),
            self.velocity_mps.copy(),
            self.acceleration_mps2.copy(),
        )
