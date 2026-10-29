"""Native receipt validation only. No substitute scorer, physics or network."""

from copy import deepcopy

import pytest

from wsorb.checkpoint import native_measurement
from wsorb.scoring import FORMULA, native_score_breakdown


def terms():
    return {
        "formula": FORMULA,
        "progress_basis": "remaining_distance_ratio",
        "progress_points": -50.0,
        "collision_penalty_points": 300.0,
        "fuel_penalty_points": 0.0,
        "offset_points": 50.0,
        "total": -300.0,
        "nominal_time_s": 120.0,
        "nominal_fuel_kg": 1.15,
        "impact_threshold_j": 209200.0,
    }


def test_signed_native_score_and_terms_are_copied_without_recalculation():
    receipt = terms()
    before = deepcopy(receipt)
    measured = native_measurement(
        {"observation": {"mission": {"score": -300, "score_breakdown": receipt}}}
    )
    assert measured["score"] == -300
    assert measured["score_breakdown"] == receipt
    measured["score_breakdown"]["total"] = 1
    assert receipt == before


def test_old_or_uncompleted_observations_do_not_acquire_an_invented_score_breakdown():
    for mission in ({}, {"score": None}, {"score": -300}):
        measured = native_measurement({"observation": {"mission": mission}})
        assert measured["score"] == mission.get("score")
        assert measured["score_breakdown"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("formula", "private-url.invalid/secret"),
        ("progress_basis", "arrival_velocity"),
        ("total", -299.0),
        ("progress_points", True),
        ("collision_penalty_points", -1.0),
        ("fuel_penalty_points", float("inf")),
        ("nominal_time_s", 0.0),
        ("nominal_fuel_kg", float("nan")),
        ("impact_threshold_j", -1.0),
    ],
)
def test_invalid_or_mismatched_native_terms_are_unavailable_not_reconstructed(field, value):
    receipt = {**terms(), field: value}
    assert native_score_breakdown(receipt, score=-300) is None


def test_incomplete_or_extra_payload_fields_cannot_enter_the_score_receipt():
    receipt = terms()
    receipt.pop("fuel_penalty_points")
    assert native_score_breakdown(receipt, score=-300) is None
    assert native_score_breakdown({**terms(), "raw_payload": "secret"}, score=-300) is None
    assert native_score_breakdown(terms(), score=None) is None
