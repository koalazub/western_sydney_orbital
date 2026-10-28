"""Evidence and denominator checks for seeded flight summaries."""

import csv
import json
from copy import deepcopy

import pytest

from wsorb.sweep_analysis import analyze_run, summarize_runs


def sample(time, damage=0, *, phase="ascending", radar=None):
    return {
        "observation": {
            "time_s": time,
            "mission": {
                "phase": phase,
                "damage": damage,
                "fuel_used_kg": time / 10,
                "target_distance_m": 100 - time,
                "relative_speed_mps": 2,
            },
            "shuttle": {"position": [0, 0, 12 + time]},
            "geometry": {"terrain_radius_at_shuttle_m": 10},
            "radar": radar or [],
        },
        "policy_wall_s": 0.2 if time else None,
    }


def run(outcome="timeout", seed=1, count=500):
    return {
        "algorithm": "baseline",
        "seed": seed,
        "debris_count": count,
        "outcome": outcome,
        "step_s": 0.1,
        "samples": [sample(0), sample(1), sample(2, 3, phase="transit")],
    }


def test_damage_is_an_interval_and_minima_are_explicitly_sampled():
    trial = run("failed_damage")
    trial["samples"][1]["observation"]["radar"] = [[[3, 0, 0], [-0.3, -1, 1], [0.25, 0, 0]]]
    before = deepcopy(trial)
    result = analyze_run(trial)
    assert result["first_damage_after_s"] == 1
    assert result["first_damage_by_s"] == 2
    assert result["first_damage_phase"] == "transit"
    assert result["min_sampled_terrain_center_clearance_m"] == 2
    assert result["closest_sampled_target_range_m"] == 98
    assert result["min_observed_radar_range_m"] == -0.3
    assert result["max_radar_age_s"] == 0.75
    assert result["max_damage_j_estimate"] == 3
    assert result["policy_calls_over_step_budget"] == 2
    assert trial == before
    json.dumps(result, allow_nan=False)


def test_missing_readings_stay_missing_and_do_not_become_zero():
    trial = run("engine_error")
    trial["samples"] = [{"observation": {"time_s": 0, "mission": {"damage": 0}}}]
    result = analyze_run(trial)
    for key in (
        "final_target_range_m",
        "final_relative_speed_mps",
        "min_sampled_terrain_center_clearance_m",
        "fuel_used_kg_estimate",
        "min_observed_radar_range_m",
        "first_damage_by_s",
    ):
        assert result[key] is None
    assert result["max_damage_j_estimate"] == 0


def test_initial_damage_has_no_invented_lower_bound_or_contact_cause():
    trial = run()
    trial["samples"] = [sample(5, damage=3)]
    result = analyze_run(trial)
    assert result["first_damage_by_s"] == 5
    assert result["first_damage_after_s"] is None
    assert "collision_cause" not in result


def test_fallback_relative_metrics_and_nonfinite_measurements():
    trial = run()
    observation = trial["samples"][-1]["observation"]
    observation["mission"] = {"damage": float("nan"), "fuel_used_kg": float("inf")}
    observation["shuttle"] = {"position": [0, 0, 0], "velocity": [1, 0, 0]}
    observation["target"] = {"position": [3, 4, 0], "velocity": [1, 2, 0]}
    result = analyze_run(trial)
    assert result["final_target_range_m"] == 5
    assert result["final_relative_speed_mps"] == 2
    assert result["fuel_used_kg_estimate"] is None
    json.dumps(result, allow_nan=False)


def test_grouping_preserves_infrastructure_errors_and_uncertainty():
    trials = [run("success", 1), run("failed_damage", 2), run("policy_error", 3)]
    trials.append(run("timeout", 1, count=1000))
    groups = summarize_runs(trials)
    assert len(groups) == 2
    group = groups[0]
    assert group["runs"] == 3
    assert group["success_rate"] == pytest.approx(1 / 3)
    assert group["physical_runs"] == 2
    assert group["physical_success_rate"] == 0.5
    assert group["outcomes"] == {"success": 1, "failed_damage": 1, "policy_error": 1}
    assert group["success_rate_wilson95_low"] < 1 / 3
    assert group["success_rate_wilson95_high"] > 1 / 3
    assert group["median_success_time_s"] == 2
    assert group["median_damage_failure_time_s"] == 2
    assert group["median_final_relative_speed_mps"] == 2
    assert group["median_fuel_used_kg_estimate"] == 0.2
    assert group["first_damage_phase_counts"] == {"transit": 3}
    assert group["final_phase_counts"] == {"transit": 3}
    json.dumps(groups, allow_nan=False)


def test_empty_and_all_error_sweeps():
    assert summarize_runs([]) == []
    trial = run("engine_error")
    trial["samples"] = []
    result = summarize_runs([trial])[0]
    assert result["physical_success_rate"] is None
    assert result["median_success_time_s"] is None
    assert result["success_rate_wilson95_high"] > 0


def test_partial_software_failures_do_not_distort_physical_comparison_medians():
    complete = run("success", seed=1)
    error = run("policy_error", seed=2)
    error["samples"] = [sample(0)]
    result = summarize_runs([complete, error])[0]
    assert result["median_final_target_range_m"] == 98
    assert result["median_fuel_used_kg_estimate"] == 0.2


def test_precomputed_summaries_match_raw_runs_and_accept_interruptions():
    trials = [run("success", 1), run("interrupted", 2), run("timeout", 3)]
    summaries = [analyze_run(trial) for trial in trials]
    before = deepcopy(summaries)
    assert summarize_runs(summaries) == summarize_runs(trials)
    result = summarize_runs(summaries)[0]
    assert result["outcomes"]["interrupted"] == 1
    assert result["physical_runs"] == 2
    assert result["success_rate"] == pytest.approx(1 / 3)
    assert result["physical_success_rate"] == 0.5
    assert summaries == before


def test_report_skips_empty_failure_trace_without_changing_trial_selection(tmp_path):
    pytest.importorskip("altair")
    from wsorb.sweep_analysis import write_report

    trials = [run("engine_error", seed=1), run("timeout", seed=2)]
    trials[0]["samples"] = []
    summaries = []
    for index, trial in enumerate(trials):
        path = tmp_path / f"trial-{index:05d}"
        path.mkdir()
        (path / "run.json").write_text(json.dumps({"algorithm": trial["algorithm"]}))
        (path / "trace.jsonl").write_text(
            "".join(json.dumps(sample) + "\n" for sample in trial["samples"])
        )
        summary = analyze_run(trial)
        summary["trial_directory"] = path.name
        summaries.append(summary)
    with (tmp_path / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (tmp_path / "summary.json").write_text(json.dumps(summarize_runs(summaries)))
    report = write_report(tmp_path)
    assert "Paired trace: seed 2, count 500" in report.read_text()
    with pytest.raises(ValueError, match="no saved observations"):
        write_report(tmp_path, trial="trial-00000")
    assert write_report(tmp_path, trial="trial-00001") == report
