"""Pure checks for the seed fuzz harness; native runs stay opt-in."""

from __future__ import annotations

import pytest

from wsorb.fuzz import (
    format_report,
    parse_seeds,
    run_seed_with_retry,
    score_points,
    summarize,
)


def test_parse_seeds_accepts_ranges_and_lists():
    assert parse_seeds("100-102,105") == (100, 101, 102, 105)
    assert parse_seeds("7") == (7,)


@pytest.mark.parametrize("value", ["", "100-99", "1,1", "100-999", "x"])
def test_parse_seeds_rejects_invalid_input(value):
    with pytest.raises(ValueError):
        parse_seeds(value)


def test_score_points_uses_the_published_formula():
    points = score_points({"time_s": 193.55, "fuel_used_kg": 31.661, "damage_j": 0.0})
    assert points == pytest.approx(69.7848, abs=1e-4)

    damaged = score_points({"time_s": 193.55, "fuel_used_kg": 31.661, "damage_j": 2092.0})
    assert damaged == pytest.approx(points - 2.0, abs=1e-4)


@pytest.mark.parametrize(
    "measurement",
    [
        {},
        {"time_s": 0.0, "fuel_used_kg": 1.0, "damage_j": 0.0},
        {"time_s": 10.0, "fuel_used_kg": None, "damage_j": 0.0},
        {"time_s": 10.0, "fuel_used_kg": 1.0, "damage_j": -1.0},
    ],
)
def test_score_points_rejects_incomplete_or_negative_measurements(measurement):
    assert score_points(measurement) is None


def test_summary_separates_failures_from_low_scores():
    results = [
        {"seed": 100, "native_success": True, "points": 69.0},
        {"seed": 101, "native_success": True, "points": 40.0},
        {"seed": 102, "native_success": False, "points": None},
    ]
    summary = summarize(results, 47.0)
    assert summary["successes"] == 2
    assert summary["failures"] == 1
    assert summary["below_threshold"] == [101, 102]
    assert summary["failed_seeds"] == [102]
    assert summary["all_passed"] is False
    assert summary["min_points"] == 40.0
    assert summary["max_points"] == 69.0


def test_summary_without_a_threshold_only_flags_missing_points():
    results = [
        {"seed": 100, "native_success": True, "points": 40.0},
        {"seed": 101, "native_success": True, "points": None},
    ]
    summary = summarize(results, None)
    assert summary["below_threshold"] == [101]
    assert summary["all_passed"] is False
    assert summary["min_points"] == 40.0


def test_report_prints_no_fixed_threshold_when_unset():
    report = {
        "threshold_points": None,
        "runs": [],
        "summary": {
            "min_points": None,
            "mean_points": None,
            "max_points": None,
            "successes": 0,
            "failures": 0,
            "below_threshold": [],
            "failed_seeds": [],
            "retried_seeds": [],
            "all_passed": True,
        },
    }
    assert "no fixed threshold" in format_report(report)


def test_retry_reruns_only_transport_failures(monkeypatch):
    calls = []

    def fake(seed, **options):
        calls.append(seed)
        if len(calls) == 1:
            return {
                "seed": seed,
                "native_success": False,
                "points": None,
                "error_code": "lease_expired",
            }
        return {
            "seed": seed,
            "native_success": True,
            "points": 55.0,
            "error_code": None,
        }

    monkeypatch.setattr("wsorb.fuzz.run_seed", fake)
    result = run_seed_with_retry(
        101,
        world_id="official",
        profile_id="ali_mppi",
        duration_s=300.0,
        wall_budget_s=420.0,
    )
    assert result["attempts"] == 2
    assert result["points"] == 55.0
    assert calls == [101, 101]


def test_retry_leaves_a_controller_result_alone(monkeypatch):
    calls = []

    def fake(seed, **options):
        calls.append(seed)
        return {
            "seed": seed,
            "native_success": True,
            "points": 40.0,
            "error_code": None,
        }

    monkeypatch.setattr("wsorb.fuzz.run_seed", fake)
    result = run_seed_with_retry(
        102,
        world_id="official",
        profile_id="ali_mppi",
        duration_s=300.0,
        wall_budget_s=420.0,
    )
    assert result["attempts"] == 1
    assert calls == [102]


def test_report_prints_the_threshold_result():
    report = {
        "threshold_points": 47.0,
        "runs": [
            {
                "seed": 100,
                "state": "native_terminal",
                "points": 69.0,
                "measurement": {
                    "time_s": 193.5,
                    "fuel_used_kg": 31.6,
                    "damage_j": 0.0,
                    "range_m": 49.9,
                    "relative_speed_mps": 6.8,
                },
                "error_code": None,
            }
        ],
        "summary": {
            "min_points": 69.0,
            "mean_points": 69.0,
            "max_points": 69.0,
            "successes": 1,
            "failures": 0,
            "below_threshold": [],
            "failed_seeds": [],
            "retried_seeds": [],
            "all_passed": True,
        },
    }
    text = format_report(report)
    assert "69.00" in text
    assert "below threshold: none" in text
    assert "failed seeds: none" in text
    assert "retried seeds: none" in text
