"""Validate detached native score receipts, without maintaining another scorer.

The engine applies the published STP score algebra to the local model's fuel
and damage estimates. Python never fills missing terms from an archived flight.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

FORMULA = "practice_normalized_time_damage_fuel_v1"
POINT_FIELDS = (
    "progress_points",
    "collision_penalty_points",
    "fuel_penalty_points",
    "offset_points",
    "total",
)
REFERENCE_FIELDS = ("nominal_time_s", "nominal_fuel_kg", "impact_threshold_j")


def native_score_breakdown(value: Any, *, score: float | None) -> dict[str, Any] | None:
    """Copy a complete, finite native receipt or report its absence as None."""
    if (
        not isinstance(value, Mapping)
        or set(value) != {"formula", "progress_basis", *POINT_FIELDS, *REFERENCE_FIELDS}
        or value["formula"] != FORMULA
        or value["progress_basis"] not in ("time_ratio", "remaining_distance_ratio")
        or score is None
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
    ):
        return None
    result = {"formula": FORMULA, "progress_basis": value["progress_basis"]}
    for field in (*POINT_FIELDS, *REFERENCE_FIELDS):
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
        try:
            number = float(number)
        except OverflowError:
            return None
        if not math.isfinite(number):
            return None
        result[field] = number
    if (
        result["total"] != score
        or result["collision_penalty_points"] < 0
        or result["fuel_penalty_points"] < 0
        or any(result[field] <= 0 for field in REFERENCE_FIELDS)
    ):
        return None
    return result
