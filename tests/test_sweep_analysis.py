"""Evidence and denominator checks for seeded flight summaries."""

import csv
import json
import math
import sys
from copy import deepcopy
from pathlib import Path
from runpy import run_path

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
    assert result["final_score"] is None
    assert result["score_collision_penalty_points"] is None


def test_recorded_score_is_retained_without_reconstructing_missing_historical_terms():
    trial = run("success")
    trial["samples"][-1]["observation"]["mission"]["score"] = -1234.5
    before = deepcopy(trial)
    result = analyze_run(trial)
    assert result["final_score"] == -1234.5
    assert result["score_formula"] is None
    assert result["score_progress_points"] is None
    assert result["score_fuel_penalty_points"] is None
    assert trial == before


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


def test_repeated_seed_attempts_do_not_get_an_independent_draw_wilson_interval():
    trials = [run("success", 1), run("success", 1), run("timeout", 2), run("timeout", 2)]
    for index, trial in enumerate(trials):
        trial["repetition"] = index % 2 + 1
    result = summarize_runs(trials)[0]
    assert result["runs"] == 4
    assert result["success_rate"] == 0.5
    assert result["success_rate_wilson95_low"] is None
    assert result["success_rate_wilson95_high"] is None
    assert "not independent" in result["success_rate_wilson95_unavailable_reason"]
    unique = summarize_runs([run("success", 1), run("timeout", 2)])[0]
    assert unique["success_rate_wilson95_low"] is not None
    assert unique["success_rate_wilson95_unavailable_reason"] is None
    assert analyze_run(trials[1])["repetition"] == 2


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
    assert "Repeated attempts at the same seed are not independent" in report.read_text()
    assert "bounds are omitted" in report.read_text()
    with pytest.raises(ValueError, match="no saved observations"):
        write_report(tmp_path, trial="trial-00000")
    assert write_report(tmp_path, trial="trial-00001") == report


def save_report_fixture(directory, trials):
    """Small saved receipts, not simulator runs or reconstructed production data."""
    summaries = []
    for index, original in enumerate(trials):
        trial = {
            "repetition": 1,
            "preset": "competition_reference",
            "step_s": 1,
            "duration_s": 2,
            "settings": {"ship": {"mass_kg": 100}, "debris": {"count": 500}},
            "algorithm_settings": {},
            "execution_kind": "fixture_only",
            "engine": {
                "engine_build": {"sha256": "a" * 64, "bytes": 123},
                "scenario_source_sha256": "b" * 64,
            },
            **deepcopy(original),
        }
        for sample in trial["samples"]:
            observation = sample["observation"]
            time = observation["time_s"]
            observation.update(frame="fixture_rotating", rotation_convention="active_xyzw")
            observation["target"] = {"position": [110, 20, 30]}
            observation["shuttle"].update(
                position=[10 + 10 * time, 20 + 4 * time, 30 + 3 * time],
                rotation=[0, 0, 0, 1],
            )
            sample["accel_command"] = [-0.2, 0, 0] if time else None
        path = directory / f"trial-{index:05d}"
        path.mkdir()
        (path / "run.json").write_text(
            json.dumps({key: value for key, value in trial.items() if key != "samples"})
        )
        (path / "trace.jsonl").write_text(
            "".join(json.dumps(sample) + "\n" for sample in trial["samples"])
        )
        summary = analyze_run(trial)
        summary["trial_directory"] = path.name
        summaries.append(summary)
    with (directory / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)
    (directory / "summary.json").write_text(json.dumps(summarize_runs(summaries)))


def report_spec(directory, *, trial=None):
    pytest.importorskip("altair")
    from wsorb.sweep_analysis import write_report

    content = write_report(directory, trial=trial).read_text()
    start = '<script id="report-spec" type="application/json">'
    specification = json.loads(content.split(start, 1)[1].split("</script>", 1)[0])
    return specification, content


def trace_line_layers(specification):
    for chart in specification["vconcat"]:
        if chart.get("data", {}).get("name") != "trace":
            continue
        for layer in chart["layer"]:
            if layer.get("mark", {}).get("type") == "line" and "detail" in layer["encoding"]:
                yield layer


def test_report_pairs_explicit_repetition_and_never_joins_trials_by_algorithm(tmp_path):
    trials = []
    for algorithm in ("baseline", "other_policy"):
        for repetition in (1, 2):
            trials.append({**run(), "algorithm": algorithm, "repetition": repetition})
    save_report_fixture(tmp_path, trials)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    spec, content = report_spec(tmp_path, trial="trial-00001")
    rows = spec["datasets"]["trace"]
    assert {row["series_id"] for row in rows} == {"trial-00001", "trial-00003"}
    assert {row["repetition"] for row in rows} == {2}
    assert all("rep 2" in row["series_label"] for row in rows)
    assert spec["usermeta"]["excluded_trials"] == ["trial-00000", "trial-00002"]
    for layer in trace_line_layers(spec):
        detail = [field["field"] for field in layer["encoding"]["detail"]]
        assert detail[0] == "series_id"
        assert detail[1] == "repetition"
        assert detail[2].endswith("_segment")
    assert "Other repetitions are not overlaid or averaged" in content
    assert all(path.read_bytes() == value for path, value in before.items())


def test_report_requires_matching_settings_schedule_and_build_not_policy_settings(tmp_path):
    selected = run()
    peer = {**run(), "algorithm": "other_policy", "algorithm_settings": {"gain": 0.4}}
    settings = {**run(), "settings": {"ship": {"mass_kg": 200}, "debris": {"count": 500}}}
    build = {**run(), "engine": {"engine_build": {"sha256": "c" * 64, "bytes": 123}}}
    scenario = {
        **run(),
        "engine": {
            "engine_build": {"sha256": "a" * 64, "bytes": 123},
            "scenario_source_sha256": "c" * 64,
        },
    }
    schedule = {**run(), "duration_s": 10}
    preset = {**run(), "preset": "other_preset"}
    unknown_repetition = {**run(), "repetition": None}
    save_report_fixture(
        tmp_path, [selected, peer, settings, build, scenario, schedule, preset, unknown_repetition]
    )
    spec, _ = report_spec(tmp_path, trial="trial-00000")
    assert {row["series_id"] for row in spec["datasets"]["trace"]} == {"trial-00000", "trial-00001"}
    assert len(spec["usermeta"]["excluded_trials"]) == 6


def test_duplicate_algorithm_and_repetition_still_have_unique_trial_detail(tmp_path):
    save_report_fixture(tmp_path, [run(), run()])
    spec, _ = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    assert len({row["series_id"] for row in rows}) == 2
    assert len({row["series_label"] for row in rows}) == 2
    first_segments = {row["target_distance_m_segment"] for row in rows if row["sample_index"] == 0}
    assert len(first_segments) == 2


def test_report_projects_actual_ownship_in_shared_equal_scale_route_frame(tmp_path):
    save_report_fixture(tmp_path, [run()])
    spec, content = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    assert [row["along_m"] for row in rows] == [0, 10, 20]
    assert [row["lateral_m"] for row in rows] == [0, 4, 8]
    assert [row["normal_m"] for row in rows] == [0, 3, 6]
    frame = spec["usermeta"]["route_frame"]
    assert frame["origin"] == [10, 20, 30]
    assert frame["along"] == [1, 0, 0]
    assert frame["length_m"] == 100
    assert len(spec["usermeta"]["route_chart_indices"]) == 2
    for index in spec["usermeta"]["route_chart_indices"]:
        chart = spec["vconcat"][index]
        path = next(layer for layer in chart["layer"] if "detail" in layer.get("encoding", {}))
        x = path["encoding"]["x"]["scale"]["domain"]
        y = path["encoding"]["y"]["scale"]["domain"]
        assert x[1] - x[0] == pytest.approx(y[1] - y[0])
        assert chart["width"] == chart["height"]
    assert "direct reference is not a policy" in content
    assert "not collision replay" in content
    assert "initial target direction" in content


def test_report_command_intervals_use_preceding_state_and_keep_missing_initial_command(tmp_path):
    save_report_fixture(tmp_path, [run()])
    path = tmp_path / "trial-00000/trace.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[1]["observation"]["shuttle"]["rotation"] = [0, 0, math.sqrt(0.5), math.sqrt(0.5)]
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    spec, content = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    assert rows[0]["requested_accel_mps2"] is None
    assert rows[0]["command_start_s"] is None
    assert rows[0]["command_end_s"] is None
    assert rows[1]["command_start_s"] == 0
    assert rows[1]["command_end_s"] == 1
    assert rows[1]["command_along_mps2"] == pytest.approx(-0.2)
    assert rows[2]["command_along_mps2"] == pytest.approx(0, abs=1e-12)
    command_chart = next(
        chart for chart in spec["vconcat"] if chart["title"] == "Command norm (m/s²)"
    )
    command_rule = next(
        layer
        for layer in command_chart["layer"]
        if layer.get("encoding", {}).get("x", {}).get("field") == "command_start_s"
    )
    assert command_rule["mark"]["type"] == "rule"
    assert command_rule["encoding"]["x2"]["field"] == "command_end_s"
    assert "Stored commands accompany post-step state for the PRECEDING interval" in content
    assert "norm alone cannot show direction" in content
    assert "not measured actuator output" in content


def test_report_nulls_break_each_metric_and_projection_without_imputation(tmp_path):
    save_report_fixture(tmp_path, [run()])
    path = tmp_path / "trial-00000/trace.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[0]["observation"]["radar"] = [[[1, 0, 0], [5, 0, 1], [0, 0, 0]]]
    samples[1]["observation"]["mission"].pop("fuel_used_kg")
    samples[1]["observation"]["shuttle"].pop("position")
    samples[2]["observation"]["radar"] = [[[1, 0, 0], [-0.3, 0, 1], [3, 0, 0]]]
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    spec, content = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    for metric in (
        "fuel_used_kg_estimate",
        "nearest_radar_range_m",
        "oldest_radar_age_s",
        "lateral_m",
        "normal_m",
    ):
        assert rows[1][metric] is None
        assert rows[1][f"{metric}_segment"] is None
        assert rows[0][f"{metric}_segment"] != rows[2][f"{metric}_segment"]
    assert rows[2]["nearest_radar_range_m"] == -0.3
    assert rows[2]["oldest_radar_age_s"] == -1
    assert rows[2]["future_radar_rows"] == 1
    for layer in trace_line_layers(spec):
        assert layer["mark"]["invalid"] == "break-paths-show-domains"
        assert "transform" not in layer
    assert "Missing means unavailable" in content
    assert "Empty or missing radar stays missing" in content


def test_missing_time_breaks_path_and_does_not_invent_a_command_interval(tmp_path):
    save_report_fixture(tmp_path, [run()])
    path = tmp_path / "trial-00000/trace.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[1]["observation"].pop("time_s")
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    spec, _ = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    assert rows[1]["time_s"] is None
    assert rows[1]["target_distance_m_segment"] is None
    assert rows[0]["target_distance_m_segment"] != rows[2]["target_distance_m_segment"]
    assert rows[2]["command_start_s"] is None
    assert rows[2]["command_end_s"] is None


def test_report_marks_first_damage_bounds_and_actual_terminal_endpoint(tmp_path):
    save_report_fixture(tmp_path, [run("failed_damage")])
    spec, content = report_spec(tmp_path)
    rows = spec["datasets"]["trace"]
    event = spec["datasets"]["events"][0]
    assert event["first_damage_after_s"] == 1
    assert event["first_damage_by_s"] == 2
    assert rows[1]["damage_bound"] == "after"
    assert rows[2]["damage_bound"] == "by"
    assert [row["terminal"] for row in rows] == [False, False, True]
    assert event["terminal_time_s"] == 2
    assert event["outcome"] == "failed_damage"
    assert "unknown lower bound stays missing" in content
    assert "not official STP points" in content
    assert "report does not re-score trials" in content
    assert "formula to local" not in content
    assert "Correlated changes do not establish" in content


def test_report_does_not_invent_an_initial_route_or_lower_damage_bound(tmp_path):
    save_report_fixture(tmp_path, [run()])
    path = tmp_path / "trial-00000/trace.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[0]["observation"].pop("target")
    samples[0]["observation"]["mission"]["damage"] = 3
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    spec, content = report_spec(tmp_path)
    assert spec["usermeta"]["route_frame"] is None
    assert spec["usermeta"]["route_chart_indices"] == []
    assert all(row["along_m"] is None for row in spec["datasets"]["trace"])
    assert spec["datasets"]["events"][0]["first_damage_after_s"] is None
    assert "No route is invented from later samples" in content


def test_report_route_frame_comes_from_chosen_trial_not_first_peer(tmp_path):
    save_report_fixture(tmp_path, [run(), {**run(), "algorithm": "other_policy"}])
    path = tmp_path / "trial-00001/trace.jsonl"
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    samples[0]["observation"]["target"]["position"] = [10, 120, 30]
    path.write_text("".join(json.dumps(sample) + "\n" for sample in samples))
    spec, _ = report_spec(tmp_path, trial="trial-00001")
    assert spec["usermeta"]["route_frame"]["along"] == [0, 1, 0]
    rows = spec["datasets"]["trace"]
    assert [row["along_m"] for row in rows if row["series_id"] == "trial-00000"] == [0, 4, 8]
    assert [row["along_m"] for row in rows if row["series_id"] == "trial-00001"] == [0, 4, 8]


def test_report_keeps_recorded_legacy_scores_without_reconstructing_stp_points(tmp_path):
    trial = run()
    trial["samples"][-1]["observation"]["mission"]["score"] = -1234.5
    save_report_fixture(tmp_path, [trial])
    spec, content = report_spec(tmp_path)
    assert spec["datasets"]["runs"][0]["final_score"] == -1234.5
    assert spec["datasets"]["runs"][0]["score_fuel_penalty_points"] is None
    chart = next(
        chart for chart in spec["vconcat"] if chart["title"] == "Legacy local score receipts"
    )
    assert "not STP points" in chart["encoding"]["y"]["title"]
    assert "not official STP points" in content
    assert "report does not re-score trials" in content


@pytest.mark.parametrize("archived_range", ["", "0"])
def test_report_rederives_incomplete_archived_run_rows_from_saved_traces(tmp_path, archived_range):
    trial = run()
    trial["samples"][-1]["observation"]["mission"]["score"] = -1234.5
    save_report_fixture(tmp_path, [trial])
    archived = {
        "trial_directory": "trial-00000",
        "algorithm": "baseline",
        "seed": 1,
        "debris_count": 500,
        "repetition": 1,
        "outcome": "timeout",
        "sample_count": 0,
        "final_target_range_m": archived_range,
        "final_relative_speed_mps": "",
        "final_score": "",
    }
    with (tmp_path / "runs.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(archived))
        writer.writeheader()
        writer.writerow(archived)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    spec, content = report_spec(tmp_path)
    displayed = spec["datasets"]["runs"][0]
    assert displayed["sample_count"] == 3
    assert displayed["archived_sample_count"] == 0
    assert displayed["final_target_range_m"] == 98
    assert displayed["final_relative_speed_mps"] == 2
    assert displayed["final_score"] == -1234.5
    assert displayed["score_formula"] is None
    assert displayed["report_metric_source"] == "analyze_run(load_trial(saved trace))"
    assert displayed["archived_table_incomplete"] is True
    assert "metadata-only rows with sample_count=0" in content
    assert "Those blanks are not zero measurements" in content
    assert "No source CSV, summary or trace is overwritten" in content
    assert all(path.read_bytes() == value for path, value in before.items())


def test_report_color_shape_dash_domains_and_cursor_stay_shared(tmp_path):
    save_report_fixture(tmp_path, [run(), {**run(), "algorithm": "other_policy"}])
    spec, content = report_spec(tmp_path)
    domains = {"color": [], "shape": [], "strokeDash": []}
    for chart in spec["vconcat"]:
        for layer in chart.get("layer", [chart]):
            for channel in domains:
                encoding = layer.get("encoding", {}).get(channel)
                if encoding is not None:
                    assert encoding["field"] == "algorithm"
                    domains[channel].append(encoding["scale"])
    for scales in domains.values():
        assert scales
        assert all(scale == scales[0] for scale in scales)
    assert spec["params"] == [{"name": "cursor_time", "value": 0}]
    assert 'label for="cursor-time"' in content
    assert 'label for="cursor-number"' in content
    assert 'id="cursor-rows"' in content
    assert 'role="region"' in content
    assert '<th scope="col">' in content
    assert 'lang="en"' in content


def test_analyze_only_trial_does_not_read_config_or_start_simulator(tmp_path, monkeypatch, capsys):
    example = Path(__file__).resolve().parents[1] / "examples/flight_sweep.py"
    main = run_path(str(example))["main"]
    calls = []

    def no_simulator(*args, **kwargs):
        pytest.fail("analyze-only started the simulator")

    def report(directory, *, trial=None):
        calls.append((directory, trial))
        return Path(directory) / "report.html"

    monkeypatch.setitem(main.__globals__, "run_sweep", no_simulator)
    monkeypatch.setitem(main.__globals__, "write_report", report)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(example),
            "--analyze-only",
            "--output",
            str(tmp_path),
            "--trial",
            "trial-00003",
            "--config",
            str(tmp_path / "does-not-exist.json"),
        ],
    )
    main()
    assert calls == [(str(tmp_path), "trial-00003")]
    assert str(tmp_path / "report.html") in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", [str(example), "--trial", "trial-00003"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert "requires --analyze-only" in capsys.readouterr().err
