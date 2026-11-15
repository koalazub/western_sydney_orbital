"""Pure checks for the parameter-search objective and grid."""

from __future__ import annotations

import math

import pytest

from wsorb.tune import MAX_DAMAGE_J, candidate_grid, compact_runs, objective


def test_candidate_grid_expands_all_combinations():
    candidates = candidate_grid({"cruise_fraction": [0.45, 0.5], "plan_steering": [False, True]})
    assert candidates == [
        {"cruise_fraction": 0.45, "plan_steering": False},
        {"cruise_fraction": 0.45, "plan_steering": True},
        {"cruise_fraction": 0.5, "plan_steering": False},
        {"cruise_fraction": 0.5, "plan_steering": True},
    ]


@pytest.mark.parametrize("grid", [{}, {"a": []}, {"a": 1}, {"a": list(range(65))}])
def test_candidate_grid_rejects_invalid_or_oversized_input(grid):
    with pytest.raises(ValueError):
        candidate_grid(grid)


def _report(points, damages):
    runs = [
        {
            "seed": 100 + index,
            "state": "native_terminal",
            "native_success": True,
            "points": point,
            "error_code": None,
            "measurement": {
                "time_s": 160.0,
                "fuel_used_kg": 40.0,
                "damage_j": damage,
                "range_m": 49.9,
                "relative_speed_mps": 8.2,
            },
        }
        for index, (point, damage) in enumerate(zip(points, damages, strict=True))
    ]
    return {
        "runs": runs,
        "summary": {
            "min_points": min(points),
            "mean_points": sum(points) / len(points),
            "max_points": max(points),
            "successes": len(runs),
            "failures": 0,
            "below_threshold": [],
            "failed_seeds": [],
            "retried_seeds": [],
            "all_passed": True,
        },
    }


def test_objective_is_the_worst_seed_when_all_are_clean():
    assert objective(_report([60.0, 70.0], [0.0, 0.0])) == 60.0


def test_objective_uses_the_official_damage_limit():
    assert objective(_report([60.0, 70.0], [0.0, 1.0])) == 60.0
    assert math.isinf(objective(_report([60.0], [MAX_DAMAGE_J + 1.0]))) is True
    report = _report([60.0], [0.0])
    report["summary"]["failures"] = 1
    assert math.isinf(objective(report)) is True


def test_compact_runs_drops_analytics():
    report = _report([60.0], [0.0])
    report["runs"][0]["analytics"] = {"samples": [[0.0, 0.0]]}
    compact = compact_runs(report)
    assert compact[0]["seed"] == 100
    assert compact[0]["points"] == 60.0
    assert "analytics" not in compact[0]
    assert compact[0]["measurement"] == {
        "time_s": 160.0,
        "fuel_used_kg": 40.0,
        "damage_j": 0.0,
        "range_m": 49.9,
        "relative_speed_mps": 8.2,
    }
