"""Local clearance, progress and fuel objective from the October minutes.

This module is a pure decision aid. It does not command a spacecraft, read
scenario data, or model a thruster suite.

The 26 and 28 October minutes ask the optimiser to consider collision
clearance, progress towards the station and fuel use, and to favour a safe
single-axis translation where one exists. They do not agree objective weights.
Every weight in this module is therefore caller-supplied; there are no default
competition weights.

The fuel quantity is a local L1 proxy: the sum of absolute body-axis
acceleration components. It excludes ignition cost and thruster allocation, so
it is not measured propellant. The quoted 41.4% figure is likewise geometry,
not a fuel measurement: at 45 degrees between two orthogonal axes, an
equal-resultant two-axis burn has L1 ``sqrt(2) - 1`` higher than a single-axis
burn.

The minutes mention a floor-related penalty but give no formula or parameters.
``FloorPenaltyProxy`` is a configurable quadratic local proxy, not an official
competition rule.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
import numpy.typing as npt

from ._validation import count, scalar, vector

_ACTIVE_AXIS_TOLERANCE = 1.0e-12

DIAGONAL_L1_INCREASE_AT_45_DEGREES = math.sqrt(2.0) - 1.0
"""Geometric L1 increase for an equal-resultant 45-degree two-axis burn.

This is ``0.4142...``, the exact value behind the rounded 41.4% statement in
the minutes. It is not a measured propellant cost.
"""


@dataclass(frozen=True, slots=True)
class FuelProxy:
    """Local L1 effort stand-in for propellant.

    The sum of absolute body-axis components measures geometric effort, not
    measured fuel. It deliberately excludes ignition cost and thruster
    allocation; the minutes say the simulator has no separate ignition cost.
    """

    l1_effort_mps2: float
    active_axes: int
    ignition_cost_included: ClassVar[bool] = False
    thruster_allocation_included: ClassVar[bool] = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "l1_effort_mps2",
            scalar(self.l1_effort_mps2, "fuel_proxy.l1_effort_mps2"),
        )
        object.__setattr__(
            self,
            "active_axes",
            count(self.active_axes, "fuel_proxy.active_axes", maximum=3),
        )


def active_axis_count(
    acceleration: npt.ArrayLike,
    *,
    tolerance: float = _ACTIVE_AXIS_TOLERANCE,
) -> int:
    """Count nonzero body-axis components above an explicit tolerance."""
    value = np.abs(vector(acceleration, "acceleration"))
    limit = scalar(tolerance, "tolerance", minimum=0.0)
    return int(np.count_nonzero(value > limit))


def local_fuel_proxy(
    acceleration: npt.ArrayLike,
    *,
    tolerance: float = _ACTIVE_AXIS_TOLERANCE,
) -> FuelProxy:
    """Return the local L1 fuel proxy for one acceleration command.

    This is geometric effort only. It excludes ignition cost and thruster
    allocation, and it is not a substitute for a validated thruster model.
    """
    value = vector(acceleration, "acceleration")
    return FuelProxy(
        l1_effort_mps2=float(np.sum(np.abs(value))),
        active_axes=active_axis_count(value, tolerance=tolerance),
    )


@dataclass(frozen=True, slots=True)
class ManeuverCandidate:
    """One candidate translation and the mission quantities it trades off.

    ``clearance_m`` is a nonnegative separation and ``progress_m`` is
    nonnegative progress towards the station, not a signed route deviation.
    The acceleration components may be negative because body axes are signed.
    """

    acceleration_mps2: tuple[float, float, float]
    clearance_m: float
    progress_m: float
    label: str = ""

    def __post_init__(self) -> None:
        acceleration = vector(self.acceleration_mps2, "candidate.acceleration_mps2")
        object.__setattr__(
            self,
            "acceleration_mps2",
            tuple(float(component) for component in acceleration),
        )
        object.__setattr__(
            self,
            "clearance_m",
            scalar(self.clearance_m, "candidate.clearance_m"),
        )
        object.__setattr__(
            self,
            "progress_m",
            scalar(self.progress_m, "candidate.progress_m"),
        )
        if not isinstance(self.label, str):
            raise ValueError("candidate.label must be a string")

    @property
    def active_axes(self) -> int:
        """Number of nonzero body-axis acceleration components."""
        return active_axis_count(self.acceleration_mps2)

    @property
    def fuel_proxy(self) -> FuelProxy:
        """Local L1 effort proxy; excludes ignition cost and allocation."""
        return local_fuel_proxy(self.acceleration_mps2)

    @property
    def l1_effort_mps2(self) -> float:
        """Component-sum effort, kept explicit for Pareto comparisons."""
        return self.fuel_proxy.l1_effort_mps2


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Caller-supplied scalarisation weights; no competition weights exist.

    The minutes did not agree how safety, route progress and fuel should be
    balanced. These fields intentionally have no defaults so that every use
    states the trade-off being made. A zero weight is allowed and disables that
    term.
    """

    clearance_weight: float
    progress_weight: float
    fuel_weight: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "clearance_weight",
            scalar(self.clearance_weight, "weights.clearance_weight"),
        )
        object.__setattr__(
            self,
            "progress_weight",
            scalar(self.progress_weight, "weights.progress_weight"),
        )
        object.__setattr__(
            self,
            "fuel_weight",
            scalar(self.fuel_weight, "weights.fuel_weight"),
        )


@dataclass(frozen=True, slots=True)
class FloorPenaltyProxy:
    """Configurable quadratic local proxy for the unexplained floor penalty.

    The 28 October minutes note a floor-related penalty but provide no formula
    or parameters, so the official rule is unknown. This local assumption is
    zero at or above ``threshold_m`` and

    ``scale * ((threshold_m - clearance_m) / threshold_m) ** 2``

    below it, reaching ``scale`` at zero clearance. A zero threshold or zero
    scale disables it. It is not an official competition rule.
    """

    threshold_m: float
    scale: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "threshold_m",
            scalar(self.threshold_m, "floor.threshold_m"),
        )
        object.__setattr__(self, "scale", scalar(self.scale, "floor.scale"))

    def penalty(self, clearance_m: float) -> float:
        """Return the local penalty for a nonnegative clearance."""
        clearance = scalar(clearance_m, "clearance_m")
        if self.threshold_m == 0.0 or clearance >= self.threshold_m:
            return 0.0
        deficit = (self.threshold_m - clearance) / self.threshold_m
        return float(self.scale * deficit * deficit)


@dataclass(frozen=True, slots=True)
class ManeuverScore:
    """Scalar score with every weighted term exposed for inspection.

    ``total`` is higher-is-better. The individual terms remain available so a
    caller can see how the scalar was assembled instead of treating it as the
    only decision signal.
    """

    candidate: ManeuverCandidate
    clearance_term: float
    progress_term: float
    fuel_term: float
    floor_penalty: float

    @property
    def total(self) -> float:
        """Weighted reward minus fuel and floor costs."""
        return self.clearance_term + self.progress_term - self.fuel_term - self.floor_penalty


def score_maneuver(
    candidate: ManeuverCandidate,
    *,
    weights: ObjectiveWeights,
    floor_penalty: FloorPenaltyProxy,
) -> ManeuverScore:
    """Score one candidate with explicit weights and a floor proxy.

    All terms are retained in the result. The Pareto helper is the intended
    way to discuss trade-offs without relying on this scalar alone.
    """
    if not isinstance(candidate, ManeuverCandidate):
        raise TypeError("candidate must be a ManeuverCandidate")
    if not isinstance(weights, ObjectiveWeights):
        raise TypeError("weights must be ObjectiveWeights")
    if not isinstance(floor_penalty, FloorPenaltyProxy):
        raise TypeError("floor_penalty must be FloorPenaltyProxy")
    return ManeuverScore(
        candidate=candidate,
        clearance_term=weights.clearance_weight * candidate.clearance_m,
        progress_term=weights.progress_weight * candidate.progress_m,
        fuel_term=weights.fuel_weight * candidate.l1_effort_mps2,
        floor_penalty=floor_penalty.penalty(candidate.clearance_m),
    )


def rank_maneuvers(
    candidates: Iterable[ManeuverCandidate],
    *,
    weights: ObjectiveWeights,
    floor_penalty: FloorPenaltyProxy,
) -> tuple[ManeuverScore, ...]:
    """Rank candidates by the local scalar score, highest first.

    Ties retain input order. Use :func:`pareto_frontier` for the trade-offs
    that this ordering deliberately collapses.
    """
    scores = [
        score_maneuver(candidate, weights=weights, floor_penalty=floor_penalty)
        for candidate in candidates
    ]
    return tuple(sorted(scores, key=lambda score: score.total, reverse=True))


def dominates(left: ManeuverCandidate, right: ManeuverCandidate) -> bool:
    """Whether ``left`` Pareto-dominates ``right``.

    The Pareto objectives are higher clearance, higher progress and lower L1
    fuel effort. ``left`` must be no worse on all three and strictly better on
    at least one. The floor proxy is a function of clearance, so it is not a
    separate Pareto axis.
    """
    if not isinstance(left, ManeuverCandidate) or not isinstance(
        right,
        ManeuverCandidate,
    ):
        raise TypeError("Pareto comparisons require ManeuverCandidate values")
    no_worse = (
        left.clearance_m >= right.clearance_m
        and left.progress_m >= right.progress_m
        and left.l1_effort_mps2 <= right.l1_effort_mps2
    )
    strictly_better = (
        left.clearance_m > right.clearance_m
        or left.progress_m > right.progress_m
        or left.l1_effort_mps2 < right.l1_effort_mps2
    )
    return no_worse and strictly_better


def pareto_frontier(
    candidates: Iterable[ManeuverCandidate],
) -> tuple[ManeuverCandidate, ...]:
    """Return the non-dominated candidates in input order.

    Clearance, progress and the L1 fuel proxy define dominance. The floor
    proxy is derived from clearance and is therefore not a fourth axis.
    Duplicate non-dominated candidates both remain in the result.
    """
    items = tuple(candidates)
    if not all(isinstance(item, ManeuverCandidate) for item in items):
        raise TypeError("Pareto frontiers require ManeuverCandidate values")
    return tuple(
        candidate
        for candidate in items
        if not any(dominates(other, candidate) for other in items if other is not candidate)
    )


@dataclass(frozen=True, slots=True)
class DiagonalL1Geometry:
    """Equal-resultant L1 geometry for two orthogonal axes.

    The diagonal acceleration is ``(a cos(angle), a sin(angle), 0)``. This is
    ideal geometry: it assumes orthogonal axes, no ignition cost and no
    thruster allocation. It is not measured propellant.
    """

    resultant_acceleration_mps2: float
    angle_rad: float
    single_axis_l1_mps2: float
    diagonal_l1_mps2: float
    l1_increase_mps2: float
    l1_increase_fraction: float
    l1_increase_percent: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resultant_acceleration_mps2",
            scalar(
                self.resultant_acceleration_mps2,
                "geometry.resultant_acceleration_mps2",
                positive=True,
            ),
        )
        object.__setattr__(
            self,
            "angle_rad",
            scalar(self.angle_rad, "geometry.angle_rad"),
        )
        for name in (
            "single_axis_l1_mps2",
            "diagonal_l1_mps2",
            "l1_increase_mps2",
            "l1_increase_fraction",
            "l1_increase_percent",
        ):
            object.__setattr__(self, name, scalar(getattr(self, name), f"geometry.{name}"))


def diagonal_l1_geometry(
    resultant_acceleration_mps2: float,
    angle_rad: float,
) -> DiagonalL1Geometry:
    """Compare equal-resultant single-axis and diagonal L1 effort.

    At 45 degrees the increase fraction is ``sqrt(2) - 1``, approximately
    0.4142 or 41.4%. This is geometry, not measured fuel, and it excludes
    ignition cost and thruster allocation.
    """
    resultant = scalar(
        resultant_acceleration_mps2,
        "resultant_acceleration_mps2",
        positive=True,
    )
    angle = scalar(angle_rad, "angle_rad")
    diagonal = resultant * (abs(math.cos(angle)) + abs(math.sin(angle)))
    increase = diagonal - resultant
    fraction = increase / resultant
    return DiagonalL1Geometry(
        resultant_acceleration_mps2=resultant,
        angle_rad=angle,
        single_axis_l1_mps2=resultant,
        diagonal_l1_mps2=diagonal,
        l1_increase_mps2=increase,
        l1_increase_fraction=fraction,
        l1_increase_percent=100.0 * fraction,
    )


def diagonal_l1_increase_fraction(angle_rad: float) -> float:
    """Fractional L1 increase at an angle, for an equal resultant magnitude."""
    return diagonal_l1_geometry(1.0, angle_rad).l1_increase_fraction
