"""Pure axis-aligned maneuver scoring for the 26 and 28 October minutes.

This module is a decision aid, not a flight controller. It models the geometric
fuel proxy discussed in the minutes: for the same resultant acceleration, a
two-axis diagonal burn has a larger L1 component sum than a single-axis burn.
At 45 degrees the extra is ``sqrt(2) - 1``, about 41.4%. That is geometry, not a
validated propellant measurement, and this module adds no ignition cost.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from ._validation import scalar, vector

_AXIS_TOLERANCE = 1.0e-12


@dataclass(frozen=True, slots=True)
class AxisManeuver:
    """One candidate burn with the quantities the minutes ask us to compare."""

    acceleration_mps2: tuple[float, float, float]
    clearance_m: float
    progress_m: float

    def __post_init__(self) -> None:
        acceleration = vector(self.acceleration_mps2, "maneuver.acceleration_mps2")
        object.__setattr__(self, "acceleration_mps2", tuple(float(value) for value in acceleration))
        object.__setattr__(
            self,
            "clearance_m",
            scalar(self.clearance_m, "maneuver.clearance_m"),
        )
        object.__setattr__(
            self,
            "progress_m",
            scalar(self.progress_m, "maneuver.progress_m"),
        )

    @property
    def active_axes(self) -> int:
        return component_count(self.acceleration_mps2)

    @property
    def l1_effort(self) -> float:
        """Component-sum effort proxy; not measured fuel or ignition count."""
        return float(np.sum(np.abs(np.asarray(self.acceleration_mps2))))


def component_count(
    acceleration: npt.ArrayLike,
    *,
    tolerance: float = _AXIS_TOLERANCE,
) -> int:
    """Count nonzero body-axis components of an acceleration command."""
    value = np.abs(vector(acceleration, "acceleration"))
    tolerance = scalar(tolerance, "tolerance", minimum=0.0)
    return int(np.count_nonzero(value > tolerance))


def axis_aligned_accelerations(maximum: float) -> tuple[np.ndarray, ...]:
    """Return the six single-axis burns at the supplied magnitude."""
    limit = scalar(maximum, "maximum", positive=True)
    axes = np.eye(3, dtype=np.float64)
    return tuple(sign * axis * limit for axis in axes for sign in (1.0, -1.0))


def diagonal_extra_fraction(angle_rad: float = math.pi / 4.0) -> float:
    """Extra L1 effort for a two-axis burn at the same resultant magnitude.

    For a unit resultant split between two orthogonal axes at ``angle_rad``,
    L1 = |cos| + |sin|. A single-axis burn of the same resultant has L1 = 1.
    The returned fraction is therefore geometric; it is not a measured fuel
    penalty and assumes ideal axis-aligned thrusters.
    """
    angle = scalar(angle_rad, "angle_rad")
    return float(abs(math.cos(angle)) + abs(math.sin(angle)) - 1.0)


def rank_maneuvers(
    maneuvers: Iterable[AxisManeuver],
    *,
    clearance_weight: float,
    progress_weight: float,
    fuel_weight: float,
    minimum_clearance_m: float = 0.0,
    prefer_single_axis: bool = True,
) -> tuple[AxisManeuver, ...]:
    """Rank safe candidates by clearance, progress and the L1 fuel proxy.

    The weights are caller-supplied because the minutes did not agree objective
    weights. Unsafe candidates are removed first. When ``prefer_single_axis`` is
    true and at least one safe single-axis candidate exists, only those are
    ranked; otherwise every safe candidate remains eligible.
    """
    clearance_weight = scalar(clearance_weight, "clearance_weight")
    progress_weight = scalar(progress_weight, "progress_weight")
    fuel_weight = scalar(fuel_weight, "fuel_weight")
    minimum_clearance = scalar(minimum_clearance_m, "minimum_clearance_m")
    safe = [item for item in maneuvers if item.clearance_m >= minimum_clearance]
    if prefer_single_axis:
        single_axis = [item for item in safe if item.active_axes <= 1]
        if single_axis:
            safe = single_axis
    return tuple(
        sorted(
            safe,
            key=lambda item: (
                clearance_weight * item.clearance_m
                + progress_weight * item.progress_m
                - fuel_weight * item.l1_effort
            ),
            reverse=True,
        )
    )
