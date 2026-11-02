"""Checks for the local maneuver objective; these are decision-aid tests."""

from __future__ import annotations

import inspect
import math
from dataclasses import MISSING, fields

import numpy as np
import pytest

from wsorb.flight.maneuver_objective import (
    DIAGONAL_L1_INCREASE_AT_45_DEGREES,
    FloorPenaltyProxy,
    ManeuverCandidate,
    ObjectiveWeights,
    active_axis_count,
    diagonal_l1_geometry,
    diagonal_l1_increase_fraction,
    dominates,
    local_fuel_proxy,
    pareto_frontier,
    rank_maneuvers,
    score_maneuver,
)


def test_objective_weights_have_no_hidden_defaults() -> None:
    weight_fields = fields(ObjectiveWeights)
    assert tuple(field.name for field in weight_fields) == (
        "clearance_weight",
        "progress_weight",
        "fuel_weight",
    )
    assert all(
        field.default is MISSING and field.default_factory is MISSING for field in weight_fields
    )
    with pytest.raises(TypeError):
        ObjectiveWeights(clearance_weight=1.0, progress_weight=1.0)  # type: ignore[call-arg]
    for function in (score_maneuver, rank_maneuvers):
        parameters = inspect.signature(function).parameters
        assert parameters["weights"].default is inspect.Parameter.empty
        assert parameters["floor_penalty"].default is inspect.Parameter.empty


def test_clearance_and_progress_weights_change_the_ranking() -> None:
    direct = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=5.0,
        progress_m=10.0,
        label="direct",
    )
    detour = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=9.0,
        progress_m=3.0,
        label="detour",
    )
    no_floor = FloorPenaltyProxy(threshold_m=0.0, scale=0.0)
    clearance_first = rank_maneuvers(
        [direct, detour],
        weights=ObjectiveWeights(
            clearance_weight=1.0,
            progress_weight=0.0,
            fuel_weight=0.0,
        ),
        floor_penalty=no_floor,
    )
    assert tuple(score.candidate for score in clearance_first) == (detour, direct)
    progress_first = rank_maneuvers(
        [direct, detour],
        weights=ObjectiveWeights(
            clearance_weight=0.0,
            progress_weight=1.0,
            fuel_weight=0.0,
        ),
        floor_penalty=no_floor,
    )
    assert tuple(score.candidate for score in progress_first) == (direct, detour)


def test_fuel_weight_can_overturn_a_small_progress_advantage() -> None:
    single = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=5.0,
        progress_m=10.0,
        label="single",
    )
    diagonal = ManeuverCandidate(
        (math.sqrt(0.5), math.sqrt(0.5), 0.0),
        clearance_m=5.0,
        progress_m=10.5,
        label="diagonal",
    )
    no_floor = FloorPenaltyProxy(threshold_m=0.0, scale=0.0)
    progress_first = rank_maneuvers(
        [single, diagonal],
        weights=ObjectiveWeights(
            clearance_weight=0.0,
            progress_weight=1.0,
            fuel_weight=0.0,
        ),
        floor_penalty=no_floor,
    )
    assert progress_first[0].candidate == diagonal
    fuel_first = rank_maneuvers(
        [single, diagonal],
        weights=ObjectiveWeights(
            clearance_weight=0.0,
            progress_weight=1.0,
            fuel_weight=10.0,
        ),
        floor_penalty=no_floor,
    )
    assert fuel_first[0].candidate == single


def test_pareto_dominance_and_frontier() -> None:
    dominating = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=10.0,
        progress_m=5.0,
        label="dominating",
    )
    dominated = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=8.0,
        progress_m=4.0,
        label="dominated",
    )
    tradeoff = ManeuverCandidate(
        (0.5, 0.0, 0.0),
        clearance_m=12.0,
        progress_m=3.0,
        label="tradeoff",
    )
    frugal = ManeuverCandidate(
        (0.5, 0.0, 0.0),
        clearance_m=10.0,
        progress_m=5.0,
        label="frugal",
    )
    twin = ManeuverCandidate(
        (1.0, 0.0, 0.0),
        clearance_m=10.0,
        progress_m=5.0,
        label="twin",
    )
    assert dominates(dominating, dominated)
    assert not dominates(dominated, dominating)
    assert not dominates(dominating, tradeoff)
    assert not dominates(tradeoff, dominating)
    assert dominates(frugal, dominating)
    assert not dominates(dominating, twin)
    assert pareto_frontier([dominated, tradeoff, dominating]) == (
        tradeoff,
        dominating,
    )


def test_floor_penalty_proxy_threshold_and_scale() -> None:
    proxy = FloorPenaltyProxy(threshold_m=5.0, scale=2.0)
    assert proxy.penalty(6.0) == 0.0
    assert proxy.penalty(5.0) == 0.0
    just_below = proxy.penalty(4.9)
    lower = proxy.penalty(2.5)
    assert 0.0 < just_below < lower < 2.0
    assert proxy.penalty(0.0) == pytest.approx(2.0)
    assert FloorPenaltyProxy(threshold_m=0.0, scale=2.0).penalty(0.0) == 0.0
    assert FloorPenaltyProxy(threshold_m=5.0, scale=0.0).penalty(0.0) == 0.0


def test_score_exposes_terms_and_applies_the_floor_proxy() -> None:
    candidate = ManeuverCandidate(
        (3.0, 4.0, 0.0),
        clearance_m=4.0,
        progress_m=7.0,
    )
    floor = FloorPenaltyProxy(threshold_m=5.0, scale=10.0)
    score = score_maneuver(
        candidate,
        weights=ObjectiveWeights(
            clearance_weight=2.0,
            progress_weight=3.0,
            fuel_weight=0.5,
        ),
        floor_penalty=floor,
    )
    assert score.clearance_term == pytest.approx(8.0)
    assert score.progress_term == pytest.approx(21.0)
    assert score.fuel_term == pytest.approx(3.5)
    assert score.floor_penalty == pytest.approx(floor.penalty(4.0))
    assert score.total == pytest.approx(8.0 + 21.0 - 3.5 - floor.penalty(4.0))


def test_45_degree_geometry_is_exactly_sqrt_two_minus_one() -> None:
    geometry = diagonal_l1_geometry(2.0, math.pi / 4.0)
    expected = math.sqrt(2.0) - 1.0
    assert pytest.approx(expected) == DIAGONAL_L1_INCREASE_AT_45_DEGREES
    assert geometry.single_axis_l1_mps2 == pytest.approx(2.0)
    assert geometry.diagonal_l1_mps2 == pytest.approx(2.0 * math.sqrt(2.0))
    assert geometry.l1_increase_mps2 == pytest.approx(2.0 * expected)
    assert geometry.l1_increase_fraction == pytest.approx(expected)
    assert geometry.l1_increase_percent == pytest.approx(100.0 * expected)
    assert geometry.l1_increase_fraction == pytest.approx(0.41421356237309503)
    assert round(geometry.l1_increase_percent, 1) == 41.4
    assert diagonal_l1_increase_fraction(math.pi / 4.0) == pytest.approx(expected)
    assert diagonal_l1_increase_fraction(0.0) == pytest.approx(0.0)
    assert diagonal_l1_increase_fraction(math.pi / 2.0) == pytest.approx(0.0)


def test_fuel_proxy_is_l1_and_excludes_ignition_and_allocation() -> None:
    proxy = local_fuel_proxy((3.0, -4.0, 0.0))
    assert proxy.l1_effort_mps2 == pytest.approx(7.0)
    assert proxy.active_axes == 2
    assert proxy.ignition_cost_included is False
    assert proxy.thruster_allocation_included is False
    assert active_axis_count((0.5, 1.0e-13, 0.0)) == 1
    assert active_axis_count((0.5, 1.0e-13, 0.0), tolerance=0.0) == 2


@pytest.mark.parametrize(
    "acceleration",
    [
        (np.nan, 0.0, 0.0),
        (np.inf, 0.0, 0.0),
        (-np.inf, 0.0, 0.0),
    ],
)
def test_candidate_rejects_nonfinite_acceleration(
    acceleration: tuple[float, float, float],
) -> None:
    with pytest.raises(ValueError):
        ManeuverCandidate(acceleration, clearance_m=1.0, progress_m=1.0)


@pytest.mark.parametrize(
    ("clearance_m", "progress_m"),
    [
        (-0.1, 1.0),
        (1.0, -0.1),
        (np.nan, 1.0),
        (1.0, np.inf),
    ],
)
def test_candidate_rejects_negative_or_nonfinite_mission_values(
    clearance_m: float,
    progress_m: float,
) -> None:
    with pytest.raises(ValueError):
        ManeuverCandidate(
            (1.0, 0.0, 0.0),
            clearance_m=clearance_m,
            progress_m=progress_m,
        )


@pytest.mark.parametrize("value", [-1.0, np.nan, np.inf, -np.inf])
def test_objective_weights_reject_negative_or_nonfinite_values(value: float) -> None:
    with pytest.raises(ValueError):
        ObjectiveWeights(
            clearance_weight=value,
            progress_weight=1.0,
            fuel_weight=1.0,
        )
    with pytest.raises(ValueError):
        ObjectiveWeights(
            clearance_weight=1.0,
            progress_weight=value,
            fuel_weight=1.0,
        )
    with pytest.raises(ValueError):
        ObjectiveWeights(
            clearance_weight=1.0,
            progress_weight=1.0,
            fuel_weight=value,
        )


@pytest.mark.parametrize(
    ("threshold_m", "scale"),
    [
        (-1.0, 1.0),
        (1.0, -1.0),
        (np.nan, 1.0),
        (1.0, np.inf),
    ],
)
def test_floor_proxy_rejects_invalid_configuration(
    threshold_m: float,
    scale: float,
) -> None:
    with pytest.raises(ValueError):
        FloorPenaltyProxy(threshold_m=threshold_m, scale=scale)


def test_floor_proxy_rejects_negative_or_nonfinite_clearance() -> None:
    proxy = FloorPenaltyProxy(threshold_m=5.0, scale=1.0)
    with pytest.raises(ValueError):
        proxy.penalty(-0.1)
    with pytest.raises(ValueError):
        proxy.penalty(np.nan)


@pytest.mark.parametrize("resultant", [-1.0, 0.0, np.nan, np.inf])
def test_geometry_rejects_invalid_resultant(resultant: float) -> None:
    with pytest.raises(ValueError):
        diagonal_l1_geometry(resultant, math.pi / 4.0)


@pytest.mark.parametrize("angle", [-0.1, np.nan, np.inf])
def test_geometry_rejects_invalid_angle(angle: float) -> None:
    with pytest.raises(ValueError):
        diagonal_l1_geometry(1.0, angle)


def test_fuel_helpers_reject_nonfinite_components_and_negative_tolerance() -> None:
    with pytest.raises(ValueError):
        local_fuel_proxy((np.nan, 0.0, 0.0))
    with pytest.raises(ValueError):
        active_axis_count((np.inf, 0.0, 0.0))
    with pytest.raises(ValueError):
        active_axis_count((1.0, 0.0, 0.0), tolerance=-1.0)
