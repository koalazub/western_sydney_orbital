"""Validate recorded local score receipts, including competition-preset estimates.

The formula ID distinguishes current algebra from archived practice scoring.
Neither establishes STP actuator or damage-counter parity. Never fill missing
terms or infer official counters.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

FORMULA = "practice_normalized_time_damage_fuel_v1"
COMPETITION_FORMULA = "official_time_damage_fuel_v1"
POINT_FIELDS = (
    "progress_points",
    "collision_penalty_points",
    "fuel_penalty_points",
    "offset_points",
    "total",
)
REFERENCE_FIELDS = ("nominal_time_s", "nominal_fuel_kg", "impact_threshold_j")


def score_compatibility() -> dict[str, Any]:
    """Detached publication policy. Equal formula IDs do not establish counter parity."""
    reference = json.loads(files("wsorb").joinpath("scoring_reference.json").read_text())
    return reference["local_comparison"]["score_compatibility"]


def native_score_breakdown(value: Any, *, score: float | None) -> dict[str, Any] | None:
    """Copy a complete, finite native receipt or report its absence as None."""
    if (
        not isinstance(value, Mapping)
        or set(value) != {"formula", "progress_basis", *POINT_FIELDS, *REFERENCE_FIELDS}
        or value["formula"] not in (FORMULA, COMPETITION_FORMULA)
        or value["progress_basis"] not in ("time_ratio", "remaining_distance_ratio")
        or score is None
        or isinstance(score, bool)
        or not isinstance(score, (int, float))
    ):
        return None
    result = {"formula": value["formula"], "progress_basis": value["progress_basis"]}
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
