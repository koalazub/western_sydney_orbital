"""Bounded public series backed by native physics-step counters.

No commands, transport, wall clock or reconstructed actuation history lives here.
Missing legacy receipts stay unavailable. A recorder belongs to one fresh trial.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

DEFINITION_ID = "native_actuation_rotation_episodes_v1"
SOURCE_KIND = "native_physics_step_counters_with_sampled_public_series"
COLUMNS = (
    "time_s",
    "fuel_used_kg",
    "burn_duration_s",
    "angular_burn_duration_s",
    "turn_duration_s",
    "total_turn_angle_rad",
    "relative_speed_mps",
    "range_m",
    "damage_j",
)
EPISODES = ("burn", "angular_burn", "turn")
COUNTERS = tuple(f"{name}_count" for name in EPISODES)
NATIVE_NUMBERS = (
    "observed_sim_s",
    *(field for name in EPISODES for field in (f"{name}_duration_s", f"longest_{name}_s")),
    "total_turn_angle_rad",
    "peak_angular_speed_rad_s",
    "propellant_impulse_ns",
    "angular_impulse_nms",
    *(f"current_{name}_s" for name in EPISODES),
)
NATIVE_KEYS = {
    "schema_version",
    "definition_id",
    *COUNTERS,
    *NATIVE_NUMBERS,
    *(f"{name}_active" for name in EPISODES),
}
RUN_KEYS = {
    "schema_version",
    "definition_id",
    "source_kind",
    "totals",
    "columns",
    "samples",
    "sample_count",
}
_CONTEXT = ("seed", "trial_revision", "tuning_revision", "manual_interventions")
_CUMULATIVE_COLUMNS = {
    2: "burn_duration_s",
    3: "angular_burn_duration_s",
    4: "turn_duration_s",
    5: "total_turn_angle_rad",
}


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _tolerance(*values: float) -> float:
    return 1e-7 * max(1.0, *values)


def _same(left: float, right: float) -> bool:
    return abs(left - right) <= _tolerance(left, right)


def validate_native_analytics(value: Any) -> dict | None:
    """Return a detached, known native DTO, never a partial or inferred receipt."""
    if (
        not isinstance(value, Mapping)
        or set(value) != NATIVE_KEYS
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("definition_id") != DEFINITION_ID
    ):
        return None
    result = {"schema_version": 1, "definition_id": DEFINITION_ID}
    for name in COUNTERS:
        # The native DTO uses u64 episode counters, not unbounded JSON integers.
        if type(value[name]) is not int or not 0 <= value[name] <= 0xFFFFFFFFFFFFFFFF:
            return None
        result[name] = value[name]
    for name in NATIVE_NUMBERS:
        number = _number(value[name])
        if number is None:
            return None
        result[name] = number
    for name in EPISODES:
        active = value[f"{name}_active"]
        if type(active) is not bool:
            return None
        result[f"{name}_active"] = active
        count = result[f"{name}_count"]
        duration, longest, current = (
            result[key] for key in (f"{name}_duration_s", f"longest_{name}_s", f"current_{name}_s")
        )
        tolerance = _tolerance(duration, result["observed_sim_s"])
        if (
            duration > result["observed_sim_s"] + tolerance
            or longest > duration + tolerance
            or current > longest + tolerance
            or (not active and current != 0)
            or (active and (current <= 0 or count == 0))
            or (count == 0 and (duration != 0 or longest != 0))
            or (count > 0 and (duration <= 0 or longest <= 0))
            or (count > 0 and longest + tolerance < duration / count)
        ):
            return None
    if result["observed_sim_s"] == 0 and any(
        result[key] != 0 for key in (*NATIVE_NUMBERS, *COUNTERS)
    ):
        return None
    return result


def validate_run_analytics(value: Any) -> dict | None:
    """Validate a complete bounded run report using only its declared evidence."""
    if (
        not isinstance(value, Mapping)
        or set(value) != RUN_KEYS
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("definition_id") != DEFINITION_ID
        or value.get("source_kind") != SOURCE_KIND
        or type(value.get("columns")) is not list
        or value["columns"] != list(COLUMNS)
        or type(value.get("samples")) is not list
        or not 1 <= len(value["samples"]) <= 256
        or type(value.get("sample_count")) is not int
        or value["sample_count"] != len(value["samples"])
    ):
        return None
    totals = validate_native_analytics(value.get("totals"))
    if totals is None:
        return None
    samples = []
    previous_time = None
    previous_cumulative = {}
    for raw in value["samples"]:
        if type(raw) is not list or len(raw) != len(COLUMNS):
            return None
        row = []
        for index, cell in enumerate(raw):
            number = _number(cell)
            if number is None and (cell is not None or index == 0):
                return None
            row.append(number)
        now = row[0]
        if (previous_time is not None and now <= previous_time) or now > totals[
            "observed_sim_s"
        ] + _tolerance(now, totals["observed_sim_s"]):
            return None
        for index in (1, 2, 3, 4, 5, 8):
            number = row[index]
            if number is None:
                continue
            previous = previous_cumulative.get(index)
            if previous is not None and number < previous:
                return None
            if index in _CUMULATIVE_COLUMNS:
                total = totals[_CUMULATIVE_COLUMNS[index]]
                if number > total + _tolerance(number, total):
                    return None
                if index in (2, 3, 4) and number > now + _tolerance(number, now):
                    return None
                if not samples and number != 0:
                    return None
                if index in (2, 3, 4) and previous is not None:
                    # Missing rows cannot establish a shorter interval bound.
                    old_time = previous_cumulative[(index, "time")]
                    if number - previous > now - old_time + _tolerance(now, number):
                        return None
            previous_cumulative[index] = number
            previous_cumulative[(index, "time")] = now
        samples.append(row)
        previous_time = now
    if samples[0][0] != 0 or not _same(samples[-1][0], totals["observed_sim_s"]):
        return None
    for index, field in _CUMULATIVE_COLUMNS.items():
        if samples[-1][index] is not None and not _same(samples[-1][index], totals[field]):
            return None
    return {
        "schema_version": 1,
        "definition_id": DEFINITION_ID,
        "source_kind": SOURCE_KIND,
        "totals": totals,
        "columns": list(COLUMNS),
        "samples": samples,
        "sample_count": len(samples),
    }


class FlightAnalyticsRecorder:
    """Observe one trial from time zero. Invalid sources never silently restart.

    Compaction retains actual first/last observations and drops interior rows.
    Native totals are copied intact, independent of sample retention or polling.
    """

    def __init__(self, max_samples: int = 64):
        if type(max_samples) is not int or not 2 <= max_samples <= 256:
            raise ValueError("max_samples must be an integer between 2 and 256")
        self._max_samples = max_samples
        self._invalid = False
        self._context = None
        self._totals = None
        self._samples = []
        self._last_public_cumulative = {}

    def observe(self, observation: Any) -> None:
        if self._invalid:
            return
        if not isinstance(observation, Mapping):
            self._invalid = True
            return
        context = tuple(observation.get(key) for key in _CONTEXT)
        mission = observation.get("mission")
        mission = mission if isinstance(mission, Mapping) else {}
        totals = validate_native_analytics(mission.get("flight_analytics"))
        now = _number(observation.get("time_s"))
        if (
            any(type(value) is not int or not 0 <= value <= 0xFFFFFFFF for value in context)
            or (self._context is not None and context != self._context)
            or totals is None
            or now is None
            or not _same(now, totals["observed_sim_s"])
            or (self._totals is None and now != 0)
        ):
            self._invalid = True
            return
        # Use the native integrated clock, never wall time or a poll counter.
        row = [
            totals["observed_sim_s"],
            _number(mission.get("fuel_used_kg")),
            *(totals[_CUMULATIVE_COLUMNS[index]] for index in (2, 3, 4, 5)),
            _number(mission.get("relative_speed_mps")),
            _number(mission.get("target_distance_m")),
            _number(mission.get("damage")),
        ]
        if self._totals is not None:
            elapsed = totals["observed_sim_s"] - self._totals["observed_sim_s"]
            monotonic = (
                *COUNTERS,
                *(key for key in NATIVE_NUMBERS if not key.startswith("current_")),
            )
            if elapsed < 0 or any(totals[key] < self._totals[key] for key in monotonic):
                self._invalid = True
                return
            for name in EPISODES:
                delta = totals[f"{name}_duration_s"] - self._totals[f"{name}_duration_s"]
                if delta > elapsed + _tolerance(now, delta) or (
                    totals[f"{name}_count"] > self._totals[f"{name}_count"] and delta <= 0
                ):
                    self._invalid = True
                    return
            if elapsed == 0:
                if totals != self._totals or row != self._samples[-1]:
                    self._invalid = True
                return
            for index in (1, 8):
                old, new = self._last_public_cumulative.get(index), row[index]
                if old is not None and new is not None and new < old:
                    self._invalid = True
                    return
        for index in (1, 8):
            if row[index] is not None:
                self._last_public_cumulative[index] = row[index]
        self._context, self._totals = context, totals
        self._samples.append(row)
        if len(self._samples) > self._max_samples:
            self._samples = [self._samples[0], *self._samples[2:-1:2], self._samples[-1]]
            if self._max_samples == 2:
                self._samples = [self._samples[0], self._samples[-1]]

    @property
    def report(self) -> dict | None:
        if self._invalid or self._totals is None:
            return None
        return validate_run_analytics(
            {
                "schema_version": 1,
                "definition_id": DEFINITION_ID,
                "source_kind": SOURCE_KIND,
                "totals": deepcopy(self._totals),
                "columns": list(COLUMNS),
                "samples": deepcopy(self._samples),
                "sample_count": len(self._samples),
            }
        )
