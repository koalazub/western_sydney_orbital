"""Saved-data publication tests. Fixtures do not start or emulate a simulator."""

import csv
import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path

import pytest

import wsorb.path_comparison as publication
from wsorb.path_comparison import (
    COLUMNS,
    MAX_BYTES,
    MAX_RUNS,
    MAX_SAMPLES,
    TRIAL_COLUMNS,
    build_comparison,
    main,
    validate_comparison,
    write_comparison,
)
from wsorb.sweep_analysis import analyze_run, summarize_runs

SECRET = "PRIVATE_fixture_token_https://private.invalid/never-publish"
PROFILES = ("wso_official_profile", "basic_pd_radar")
BUILD = {"bytes": 27, "sha256": hashlib.sha256(b"saved fixture, not an engine").hexdigest()}
ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text())


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def trace(directory, index=0):
    return [
        json.loads(line)
        for line in (directory / f"trial-{index:05d}" / "trace.jsonl").read_text().splitlines()
    ]


def write_trace(directory, index, rows):
    (directory / f"trial-{index:05d}" / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def mutate_trace(directory, index, mutate):
    rows = trace(directory, index)
    mutate(rows)
    write_trace(directory, index, rows)


def mutate_run(directory, index, mutate):
    path = directory / f"trial-{index:05d}" / "run.json"
    run = read_json(path)
    mutate(run)
    write_json(path, run)


def write_summaries(directory):
    analyses = []
    for path in sorted(directory.glob("trial-*")):
        run = read_json(path / "run.json")
        run["samples"] = [
            json.loads(line) for line in (path / "trace.jsonl").read_text().splitlines()
        ]
        analyses.append({**analyze_run(run), "trial_directory": path.name})
    write_csv(directory / "runs.csv", analyses)
    groups = summarize_runs(analyses)
    write_json(directory / "summary.json", groups)
    write_csv(directory / "summary.csv", groups)


def snapshot(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def make_sweep(tmp_path, *, profiles=PROFILES, seeds=(7,), repetitions=2, duration=4):
    directory = tmp_path / "private-saved-sweep"
    directory.mkdir()
    settings = {
        "ship": {"mass_kg": 1000, "inertia_scale": 1},
        "debris": {"count": 5, "distribution": {"kind": "official_cloud"}},
    }
    config = {
        "schema_version": 1,
        "preset": "competition_reference",
        "seeds": list(seeds),
        "repetitions": repetitions,
        "debris_counts": [5],
        "duration_s": duration,
        "step_s": 1,
        "settings": settings,
        "algorithms": {profile: {} for profile in profiles},
    }
    write_json(directory / "configuration.json", config)
    write_json(directory / "policies.json", {"source": SECRET})
    write_json(directory / "python.json", {"package_sources": SECRET, "token": SECRET})
    index = 0
    for seed in seeds:
        for profile in profiles:
            for repetition in range(1, repetitions + 1):
                outcome = ("success", "failed_damage", "timeout", "policy_error")[index % 4]
                path = directory / f"trial-{index:05d}"
                path.mkdir()
                run = {
                    "schema_version": 1,
                    "execution_kind": "owned_native_manual_step",
                    "algorithm": profile,
                    "algorithm_settings": {},
                    "seed": seed,
                    "repetition": repetition,
                    "debris_count": 5,
                    "preset": config["preset"],
                    "duration_s": duration,
                    "step_s": 1,
                    "settings": deepcopy(settings),
                    "outcome": outcome,
                    "error": SECRET if outcome == "policy_error" else None,
                    "private_descriptor": SECRET,
                    "engine": {
                        "engine_build": BUILD,
                        "wasm_sha256": BUILD["sha256"],
                        "scenario_source_sha256": "c" * 64,
                        "backend": "native_headless",
                        "execution_kind": "owned_native_manual_step",
                        "control_mode": "owned_native_API_manual_step",
                        "scenario_source": {
                            "integration": {"fixed_step_s": 0.25},
                            "mission": {"arrival_radius_m": 20},
                            "private_source": SECRET,
                        },
                    },
                }
                write_json(path / "run.json", run)
                rows = []
                horizon = duration // 2 if outcome == "policy_error" else duration
                for now in range(horizon + 1):
                    fraction = now / duration
                    position = [10 + 100 * fraction, 20 + 8 * fraction, 30 - 12 * fraction]
                    final = now == duration and outcome in {"success", "failed_damage"}
                    damage = 0
                    if outcome == "failed_damage":
                        damage = 7 if now == duration else 3 if now >= duration / 2 else 0
                    radar = [
                        [[40, 0, 0], [40, -1, 4], [now, 0, 0]],
                        [[20, 0, 0], [20, 1, 2], [max(0, now - 0.25), 0, 0]],
                    ]
                    rows.append(
                        {
                            "observation": {
                                "time_s": now,
                                "seed": seed,
                                "trial_revision": 2,
                                "tuning_revision": 0,
                                "manual_interventions": 0,
                                "frame": "recorded reference fixture",
                                "flight_preset": config["preset"],
                                "experiment_settings": deepcopy(settings),
                                "debris_distribution": {"kind": "official_cloud"},
                                "shuttle": {
                                    "position": position,
                                    "velocity": [25, 2, -3],
                                    "fuel_kg": 999,
                                    "private": SECRET,
                                },
                                "target": {"position": [110, 20, 30], "velocity": [0, 0, 0]},
                                "mission": {
                                    "status": outcome if final else "running",
                                    "phase": "arrived"
                                    if final and outcome == "success"
                                    else "transit",
                                    "target_distance_m": math.dist(position, [110, 20, 30]),
                                    "relative_speed_mps": math.hypot(25, 2, -3),
                                    "fuel_used_kg": fraction,
                                    "damage": damage,
                                    "private": SECRET,
                                },
                                "geometry": {
                                    "terrain_radius_at_shuttle_m": 30,
                                    "arrival_radius_m": 20,
                                },
                                "radar": radar,
                                "radar_backup": SECRET,
                                "camera": SECRET,
                                "descriptor": SECRET,
                                "debris_truth": SECRET,
                                "token": SECRET,
                            },
                            "accel_command": [fraction * 0.2, -0.1, 0.2] if now else None,
                            "angular_command": [0, 0, 0.01] if now else None,
                            "policy_wall_s": 0.01 if now else None,
                            "exception": SECRET,
                        }
                    )
                write_trace(directory, index, rows)
                index += 1
    write_summaries(directory)
    return directory


@pytest.fixture
def saved_sweep(tmp_path):
    return make_sweep(tmp_path)


def test_report_can_use_a_new_file_without_overwriting_saved_evidence(saved_sweep, tmp_path):
    from wsorb.sweep_analysis import write_report

    (saved_sweep / "report.html").write_text("Preserve this earlier report")
    before = snapshot(saved_sweep)
    output = tmp_path / "review" / "comparison.html"
    assert write_report(saved_sweep, trial="trial-00000", report_file=output) == output
    assert "Comparison charts" in output.read_text()
    assert snapshot(saved_sweep) == before
    with pytest.raises(FileExistsError):
        write_report(saved_sweep, trial="trial-00000", report_file=output)


def test_historical_split_distribution_uses_both_actual_receipts(saved_sweep):
    for index in range(4):

        def split(rows):
            for row in rows:
                del row["observation"]["experiment_settings"]["debris"]["distribution"]

        mutate_trace(saved_sweep, index, split)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert "beside experiment_settings" in data["comparison"]["limitations"][-1]
    mutate_trace(saved_sweep, 0, lambda rows: rows[0]["observation"].pop("debris_distribution"))
    with pytest.raises(ValueError, match="partial or mixed world settings"):
        build_comparison(saved_sweep, comparison_id="official")


def test_exact_public_contract_repetitions_original_ids_and_owner_labels(saved_sweep):
    before = snapshot(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert set(data) == {"schema", "schema_version", "id", "label", "comparison", "columns", "runs"}
    assert data["schema"] == "wsorb.path_comparison"
    assert data["schema_version"] == 1
    assert data["id"] == "official"
    assert (
        data["columns"]
        == list(COLUMNS)
        == [
            "time_s",
            "along_m",
            "lateral_m",
            "normal_m",
            "range_m",
            "relative_speed_mps",
            "fuel_kg",
            "damage_j",
            "accel_mps2",
            "angular_accel_rad_s2",
            "nearest_radar_m",
            "max_radar_age_s",
            "terrain_clearance_m",
            "ax_mps2",
            "ay_mps2",
            "az_mps2",
            "command_from_s",
        ]
    )
    assert set(data["comparison"]) == {
        "preset",
        "distribution",
        "debris_count",
        "seeds",
        "repetitions",
        "duration_s",
        "step_s",
        "engine_sha256",
        "arrival_radius_m",
        "route_length_m",
        "basis",
        "frame",
        "limitations",
    }
    assert data["comparison"]["engine_sha256"] == BUILD["sha256"]
    assert data["comparison"]["seeds"] == [7]
    assert data["comparison"]["repetitions"] == 2
    assert [(row["algorithm"], row["repetition"]) for row in data["runs"]] == [
        (PROFILES[0], 1),
        (PROFILES[0], 2),
        (PROFILES[1], 1),
        (PROFILES[1], 2),
    ]
    assert {row["outcome"] for row in data["runs"]} == {
        "success",
        "failed_damage",
        "timeout",
        "policy_error",
    }
    assert data["runs"][0]["owner"] == "Ali"
    assert data["runs"][0]["label"] == "Ali official profile"
    assert data["runs"][0]["algorithm"] == "wso_official_profile"
    assert data["runs"][2]["owner"] == "Harry"
    assert data["runs"][2]["label"] == "Harry basic PD/radar"
    assert validate_comparison(data) == data
    assert before == snapshot(saved_sweep)
    for run in data["runs"]:
        assert set(run) == {
            "id",
            "algorithm",
            "owner",
            "label",
            "seed",
            "repetition",
            "outcome",
            "source_sample_count",
            "samples",
            "events",
            "final",
            "source_sha256",
        }
        assert set(run["final"]) == {
            "time_s",
            "range_m",
            "relative_speed_mps",
            "fuel_kg",
            "damage_j",
        }
        for name in ("run", "trace"):
            source = saved_sweep / run["id"] / ("run.json" if name == "run" else "trace.jsonl")
            assert run["source_sha256"][name] == hashlib.sha256(source.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("profile", "owner", "label"),
    [
        ("ali_official_profile", "Ali", "Ali official profile"),
        ("official_basic_beeline", "Published", "Published BasicController"),
        ("baseline", "WSO", "WSO checkpoint baseline"),
        ("body-axis", "WSO", "WSO checkpoint body-axis"),
        ("bui_alpha_beta_mvp", "Bui", "Bui alpha-beta + sidestep"),
        ("tom_kalman_velocity_mvp", "Tom", "Tom Kalman + velocity governor"),
    ],
)
def test_reviewed_labels_do_not_rename_original_algorithm_ids(tmp_path, profile, owner, label):
    directory = make_sweep(tmp_path, profiles=(profile,), repetitions=1)
    run = build_comparison(directory, comparison_id="team")["runs"][0]
    assert (run["algorithm"], run["owner"], run["label"]) == (profile, owner, label)


def test_fixed_frame_has_true_start_goal_and_signed_orthogonal_components(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    comparison = data["comparison"]
    assert comparison["route_length_m"] == 100
    assert comparison["basis"] == {
        "origin_reference_m": [10, 20, 30],
        "target_initial_reference_m": [110, 20, 30],
        "along_axis": [1, 0, 0],
        "lateral_axis": [0, 1, 0],
        "normal_axis": [0, 0, 1],
    }
    assert data["runs"][0]["samples"][0][1:4] == [0, 0, 0]
    assert data["runs"][0]["samples"][1][1:4] == [25, 2, -3]
    notes = " ".join(comparison["limitations"])
    assert "signed, not geographic or terrain normals" in notes
    assert "e3=e1 cross e2" in notes


def test_vertical_route_basis_is_deterministic_and_right_handed(saved_sweep):
    for index in range(4):

        def rotate(rows):
            for sample in rows:
                observation = sample["observation"]
                for family in ("shuttle", "target"):
                    x, y, z = observation[family]["position"]
                    observation[family]["position"] = [-z, y, x]

        mutate_trace(saved_sweep, index, rotate)
    write_summaries(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["comparison"]["basis"]["along_axis"] == [0, 0, 1]
    assert data["comparison"]["basis"]["lateral_axis"] == [1, 0, 0]
    assert data["comparison"]["basis"]["normal_axis"] == [0, 1, 0]
    assert data["runs"][0]["samples"][1][1:4] == [25, 3, 2]


def test_oblique_route_basis_projects_signed_components_without_radial_assumptions(saved_sweep):
    root_two = math.sqrt(2)
    for index in range(4):

        def rotate(rows):
            for sample in rows:
                for family in ("shuttle", "target"):
                    x, y, z = sample["observation"][family]["position"]
                    sample["observation"][family]["position"] = [
                        (x - y) / root_two,
                        (x + y) / root_two,
                        z,
                    ]

        mutate_trace(saved_sweep, index, rotate)
    write_summaries(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    basis = data["comparison"]["basis"]
    assert data["comparison"]["route_length_m"] == pytest.approx(100)
    assert basis["along_axis"] == pytest.approx([1 / root_two, 1 / root_two, 0])
    assert basis["lateral_axis"] == pytest.approx([0, 0, 1])
    assert basis["normal_axis"] == pytest.approx([1 / root_two, -1 / root_two, 0])
    assert data["runs"][0]["samples"][1][1:4] == pytest.approx([25, -3, -2])


def test_command_timing_requested_body_components_and_fuel_semantics(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    first, second = data["runs"][0]["samples"][:2]
    assert first[8:10] == [None, None]
    assert first[13:16] == [None, None, None]
    assert second[0] == 1
    assert second[13:16] == [0.05, -0.1, 0.2]
    assert first[16] is None
    assert second[16] == 0
    assert second[8] == pytest.approx(math.hypot(0.05, -0.1, 0.2))
    assert second[9] == 0.01
    assert second[6] == 0.25
    assert second[6] != trace(saved_sweep)[1]["observation"]["shuttle"]["fuel_kg"]
    notes = " ".join(data["comparison"]["limitations"])
    assert "PREVIOUS interval, not the next interval" in notes
    assert "requested body-frame components" in notes
    assert "cumulative mission.fuel_used_kg" in notes


def test_radar_reductions_use_only_public_rows_and_clearance_uses_reported_radius(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    row = data["runs"][0]["samples"][1]
    assert row[10] == 20
    assert row[11] == 0.25
    assert row[12] == pytest.approx(math.hypot(35, 22, 27) - 30)
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: rows[1]["observation"]["geometry"].pop("terrain_radius_at_shuttle_m"),
    )
    write_summaries(saved_sweep)
    row = build_comparison(saved_sweep, comparison_id="official")["runs"][0]["samples"][1]
    assert row[12] is None


def test_negative_noisy_radar_ranges_and_future_timestamp_ages_are_preserved(saved_sweep):
    def future_and_noisy(rows):
        rows[1]["observation"]["radar"] = [
            [[0, 0, 0], [-4.125, 0, 0], [2.25, 0, 0]],
            [[0, 0, 0], [-1.5, 0, 0], [3.5, 0, 0]],
        ]

    mutate_trace(saved_sweep, 0, future_and_noisy)
    write_summaries(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    row = data["runs"][0]["samples"][1]
    assert row[10] == -4.125
    assert row[11] == -1.25
    assert validate_comparison(data) == data
    assert any("not discarded or clamped" in note for note in data["comparison"]["limitations"])


def test_all_damage_increases_keep_true_observation_brackets(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    run = data["runs"][1]
    assert run["events"] == [
        {"kind": "damage", "after_s": 1, "by_s": 2, "value": 3},
        {"kind": "damage", "after_s": 3, "by_s": 4, "value": 7},
        {"kind": "terminal", "after_s": None, "by_s": 4, "value": None},
    ]
    assert run["final"]["damage_j"] == 7
    assert data["runs"][3]["events"][-1] == {
        "kind": "terminal",
        "after_s": None,
        "by_s": 2,
        "value": None,
    }
    assert data["runs"][3]["final"]["time_s"] == 2


def test_damage_bracket_spans_missing_damage_without_claiming_contact_time(saved_sweep):
    mutate_trace(saved_sweep, 1, lambda rows: rows[1]["observation"]["mission"].update(damage=None))
    write_summaries(saved_sweep)
    events = build_comparison(saved_sweep, comparison_id="official")["runs"][1]["events"]
    assert events[0] == {"kind": "damage", "after_s": 0, "by_s": 2, "value": 3}


def test_initial_positive_damage_has_no_fabricated_lower_bound(saved_sweep):
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: [row["observation"]["mission"].update(damage=2) for row in rows],
    )
    write_summaries(saved_sweep)
    assert build_comparison(saved_sweep, comparison_id="official")["runs"][0]["events"][0] == {
        "kind": "damage",
        "after_s": None,
        "by_s": 0,
        "value": 2,
    }


def test_missing_measurements_stay_null_and_do_not_use_private_fallbacks(saved_sweep):
    def remove_measurements(rows):
        observation = rows[1]["observation"]
        observation["shuttle"].update(position=None, velocity=None)
        observation["mission"].update(
            target_distance_m=None, relative_speed_mps=None, fuel_used_kg=None, damage=None
        )
        observation["geometry"].pop("terrain_radius_at_shuttle_m")
        observation["geometry"]["phobos_enclosing_radius_m"] = 1
        observation["radar"] = []
        observation["radar_backup"] = [[[1, 2, 3], [1, 0, 0], [1, 0, 0]]]
        rows[1].update(accel_command=None, angular_command=None)

    mutate_trace(saved_sweep, 0, remove_measurements)
    write_summaries(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["runs"][0]["samples"][1] == [1, *([None] * (len(COLUMNS) - 1))]


def test_public_navigation_is_the_only_missing_range_and_speed_fallback(saved_sweep):
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: rows[-1]["observation"]["mission"].update(
            target_distance_m=None, relative_speed_mps=None
        ),
    )
    write_summaries(saved_sweep)
    final = build_comparison(saved_sweep, comparison_id="official")["runs"][0]["final"]
    assert final["range_m"] == pytest.approx(math.hypot(0, 8, -12))
    assert final["relative_speed_mps"] == pytest.approx(math.hypot(25, 2, -3))


def test_missing_final_measurements_remain_null_in_final_and_samples(saved_sweep):
    def remove_final(rows):
        observation = rows[-1]["observation"]
        observation["shuttle"].update(position=None, velocity=None)
        observation["mission"].update(
            target_distance_m=None, relative_speed_mps=None, fuel_used_kg=None, damage=None
        )

    mutate_trace(saved_sweep, 0, remove_final)
    write_summaries(saved_sweep)
    run = build_comparison(saved_sweep, comparison_id="official")["runs"][0]
    assert run["final"] == {
        "time_s": 4,
        "range_m": None,
        "relative_speed_mps": None,
        "fuel_kg": None,
        "damage_j": None,
    }


def test_moving_public_target_is_documented_but_does_not_rotate_route_frame(saved_sweep):
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: rows[-1]["observation"]["target"].update(position=[110, 21, 30]),
    )
    write_summaries(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["comparison"]["basis"]["target_initial_reference_m"] == [110, 20, 30]
    assert any(
        "Public target navigation moved more than 1e-6 m" in note
        for note in data["comparison"]["limitations"]
    )
    assert data["runs"][0]["samples"][-1][1:4] == [100, 8, -12]


def test_thinning_retains_actual_endpoints_every_damage_bracket_and_null_gap(tmp_path):
    directory = make_sweep(tmp_path, duration=600)

    def add_gap(rows):
        for index in range(200, 250):
            observation = rows[index]["observation"]
            observation["mission"]["fuel_used_kg"] = None
            observation["radar"] = None

    mutate_trace(directory, 1, add_gap)
    write_summaries(directory)
    data = build_comparison(directory, comparison_id="official", max_samples=32)
    run = data["runs"][1]
    assert run["source_sample_count"] == 601
    assert len(run["samples"]) == 32
    kept = {row[0] for row in run["samples"]}
    assert {0, 1, 199, 200, 249, 250, 299, 300, 599, 600} <= kept
    assert run["events"][:2] == [
        {"kind": "damage", "after_s": 299, "by_s": 300, "value": 3},
        {"kind": "damage", "after_s": 599, "by_s": 600, "value": 7},
    ]
    original = trace(directory, 1)
    for row in run["samples"]:
        source = original[int(row[0])]
        assert row[6] == source["observation"]["mission"]["fuel_used_kg"]
        assert row[13:16] == (source["accel_command"] or [None, None, None])
    assert build_comparison(directory, comparison_id="official", max_samples=32) == data


def test_preservation_over_budget_is_rejected_instead_of_dropping_events(saved_sweep):
    with pytest.raises(ValueError, match="Damage brackets and null gaps"):
        build_comparison(saved_sweep, comparison_id="official", max_samples=2)


@pytest.mark.parametrize("max_samples", [0, 1, 513, True, 4.5])
def test_retained_sample_limit_is_strict(saved_sweep, max_samples):
    with pytest.raises(ValueError):
        build_comparison(saved_sweep, comparison_id="official", max_samples=max_samples)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update(accel_command=[1, 2]),
        lambda row: row.update(angular_command=[1, None, 3]),
        lambda row: row["observation"]["shuttle"].update(position=[1, 2]),
        lambda row: row["observation"]["target"].update(velocity=[0, 1, "bad"]),
        lambda row: row["observation"].update(radar="not rows"),
        lambda row: row["observation"].update(radar=[[[0, 0, 0], [1, 0, 0]]]),
        lambda row: row["observation"]["radar"][0].__setitem__(1, [1, 0]),
        lambda row: row["observation"]["mission"].update(damage=-1),
        lambda row: row["observation"]["mission"].update(fuel_used_kg=-1),
        lambda row: row["observation"]["mission"].update(target_distance_m=-1),
        lambda row: row["observation"]["mission"].update(relative_speed_mps=True),
        lambda row: row["observation"].update(time_s=-1),
        lambda row: row["observation"].update(time_s=float("inf")),
        lambda row: row["observation"]["shuttle"].update(position=[float("nan"), 0, 0]),
        lambda row: row["observation"].update(private_nonfinite=float("-inf")),
    ],
)
def test_malformed_nonfinite_and_negative_saved_measurements_fail_closed(saved_sweep, mutation):
    mutate_trace(saved_sweep, 0, lambda rows: mutation(rows[1]))
    with pytest.raises(ValueError):
        build_comparison(saved_sweep, comparison_id="official")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[2]["observation"].update(time_s=1),
        lambda rows: rows[2]["observation"].update(time_s=0.5),
        lambda rows: rows[2]["observation"].update(time_s=2.25),
        lambda rows: rows[0].update(accel_command=[0, 0, 0]),
        lambda rows: rows[2]["observation"]["mission"].update(fuel_used_kg=0),
        lambda rows: rows[3]["observation"]["mission"].update(damage=0),
        lambda rows: rows[2]["observation"].update(manual_interventions=1),
        lambda rows: rows[2]["observation"].update(seed=10),
        lambda rows: rows[2]["observation"].update(trial_revision=3),
        lambda rows: rows[2]["observation"].update(tuning_revision=1),
        lambda rows: rows[2]["observation"].pop("experiment_settings"),
        lambda rows: rows[2]["observation"]["experiment_settings"]["ship"].update(mass_kg=2000),
        lambda rows: rows[2]["observation"].update(flight_preset="far_side_l2"),
        lambda rows: rows[2]["observation"]["geometry"].update(arrival_radius_m=30),
    ],
)
def test_changed_clocks_cumulative_values_and_public_context_are_rejected(saved_sweep, mutation):
    mutate_trace(saved_sweep, 1, mutation)
    with pytest.raises(ValueError):
        build_comparison(saved_sweep, comparison_id="official")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda run: run.update(repetition=1),
        lambda run: run.update(seed=8),
        lambda run: run.update(preset="far_side_l2"),
        lambda run: run.update(duration_s=5),
        lambda run: run.update(step_s=0.5),
        lambda run: run.update(algorithm_settings={"different": 1}),
        lambda run: run.update(outcome="success"),
        lambda run: run.update(engine=None),
        lambda run: run.update(execution_kind="other"),
        lambda run: run["settings"]["ship"].update(mass_kg=2000),
        lambda run: run["engine"]["engine_build"].update(bytes=28),
        lambda run: run["engine"].update(wasm_sha256="d" * 64),
        lambda run: run["engine"].update(scenario_source_sha256="d" * 64),
        lambda run: run["engine"]["scenario_source"]["integration"].update(fixed_step_s=0.125),
        lambda run: run["engine"]["scenario_source"]["mission"].update(arrival_radius_m=21),
    ],
)
def test_duplicate_pair_and_mixed_cohort_metadata_are_rejected(saved_sweep, mutation):
    mutate_run(saved_sweep, 1, mutation)
    with pytest.raises(ValueError):
        build_comparison(saved_sweep, comparison_id="official")


@pytest.mark.parametrize("family", ["shuttle", "target"])
def test_even_unpaired_seed_cases_must_share_published_start_and_target(saved_sweep, family):
    mutate_trace(
        saved_sweep, 1, lambda rows: rows[0]["observation"][family]["position"].__setitem__(1, 21)
    )
    with pytest.raises(ValueError, match="anchors and basis"):
        build_comparison(saved_sweep, comparison_id="official")


def test_initial_anchor_drift_inside_declared_tolerance_is_accepted(saved_sweep):
    mutate_trace(
        saved_sweep,
        1,
        lambda rows: rows[0]["observation"]["shuttle"]["position"].__setitem__(0, 10 + 1e-7),
    )
    write_summaries(saved_sweep)
    assert (
        build_comparison(saved_sweep, comparison_id="official")["comparison"]["route_length_m"]
        == 100
    )


def test_missing_attempt_and_partial_trial_are_not_silently_omitted(saved_sweep):
    path = saved_sweep / "trial-00001" / "trace.jsonl"
    path.write_text("")
    with pytest.raises(ValueError, match="initial saved observation"):
        build_comparison(saved_sweep, comparison_id="official")


def test_missing_configured_pair_is_not_replaced_with_a_zero_trial(saved_sweep):
    config = read_json(saved_sweep / "configuration.json")
    config["seeds"].append(8)
    write_json(saved_sweep / "configuration.json", config)
    with pytest.raises(ValueError, match="every paired attempt"):
        build_comparison(saved_sweep, comparison_id="official")


def test_configuration_run_limit_is_checked_before_loading_trials(saved_sweep):
    config = read_json(saved_sweep / "configuration.json")
    config["seeds"] = list(range(33))
    write_json(saved_sweep / "configuration.json", config)
    with pytest.raises(ValueError, match="128 runs"):
        build_comparison(saved_sweep, comparison_id="official")


def test_json_duplicate_keys_and_overflow_are_rejected(saved_sweep):
    path = saved_sweep / "configuration.json"
    original = path.read_text()
    path.write_text(
        original.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    )
    with pytest.raises(ValueError, match="duplicate object"):
        build_comparison(saved_sweep, comparison_id="official")
    path.write_text(original.replace('"duration_s": 4', '"duration_s": 1e999'))
    with pytest.raises(ValueError, match="finite"):
        build_comparison(saved_sweep, comparison_id="official")


@pytest.mark.parametrize(
    ("filename", "key", "value"),
    [
        ("runs.csv", "outcome", "timeout"),
        ("runs.csv", "seed", "8"),
        ("runs.csv", "final_target_range_m", "99"),
        ("runs.csv", "sample_count", "4"),
        ("runs.csv", "fuel_used_kg_estimate", "NaN"),
        ("summary.csv", "successes", "999"),
        ("summary.csv", "median_final_relative_speed_mps", "999"),
    ],
)
def test_current_analyze_run_is_crosschecked_against_both_saved_csv_summaries(
    saved_sweep, filename, key, value
):
    path = saved_sweep / filename
    rows = read_csv(path)
    rows[0][key] = value
    write_csv(path, rows)
    with pytest.raises(ValueError):
        build_comparison(saved_sweep, comparison_id="official")


def test_current_analyze_run_is_crosschecked_against_saved_aggregate_json(saved_sweep):
    path = saved_sweep / "summary.json"
    groups = read_json(path)
    groups[0]["median_fuel_used_kg_estimate"] = 999
    write_json(path, groups)
    with pytest.raises(ValueError, match="aggregate metrics"):
        build_comparison(saved_sweep, comparison_id="official")


def test_legacy_empty_row_summaries_require_matching_measured_aggregates(saved_sweep):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    fields = (
        "elapsed_sim_s",
        "final_target_range_m",
        "final_relative_speed_mps",
        "fuel_used_kg_estimate",
        "max_damage_j_estimate",
        "first_damage_after_s",
        "first_damage_by_s",
    )
    for row in rows:
        row["sample_count"] = "0"
        row.update(dict.fromkeys(fields, ""))
    write_csv(path, rows)
    before = snapshot(saved_sweep)
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["runs"][0]["source_sample_count"] == 5
    assert data["runs"][0]["final"]["fuel_kg"] == 1
    assert any(
        "original per-trial runs.csv is incomplete" in note
        for note in data["comparison"]["limitations"]
    )
    assert all(
        row["final_target_range_m"] == "" and row["sample_count"] == "0" for row in read_csv(path)
    )
    assert before == snapshot(saved_sweep)
    groups = read_json(saved_sweep / "summary.json")
    groups[0]["median_fuel_used_kg_estimate"] = 100
    write_json(saved_sweep / "summary.json", groups)
    with pytest.raises(ValueError, match="aggregate metrics"):
        build_comparison(saved_sweep, comparison_id="official")


def test_missing_csv_cells_stay_unavailable_while_genuine_trace_finals_export(saved_sweep):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    for key in (
        "elapsed_sim_s",
        "final_target_range_m",
        "final_relative_speed_mps",
        "fuel_used_kg_estimate",
        "max_damage_j_estimate",
        "first_damage_after_s",
        "first_damage_by_s",
    ):
        rows[0][key] = ""
    rows[0]["sample_count"] = "0"
    write_csv(path, rows)
    before = path.read_bytes()
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["runs"][0]["final"]["time_s"] == 4
    assert data["runs"][0]["final"]["range_m"] == pytest.approx(math.hypot(0, 8, -12))
    assert data["runs"][0]["source_sample_count"] == 5
    assert read_csv(path)[0]["final_target_range_m"] == ""
    assert read_csv(path)[0]["sample_count"] == "0"
    assert path.read_bytes() == before
    notes = " ".join(data["comparison"]["limitations"])
    assert "re-derived from saved trace.jsonl" in notes
    assert "not recovered from the missing table" in notes
    assert "checked against saved summary.csv" in notes


def test_partially_missing_numeric_csv_cells_do_not_hide_contradictory_populated_metrics(
    saved_sweep,
):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    rows[0]["sample_count"] = "0"
    rows[0]["elapsed_sim_s"] = ""
    rows[0]["final_target_range_m"] = "99"
    write_csv(path, rows)
    with pytest.raises(ValueError, match="final metrics"):
        build_comparison(saved_sweep, comparison_id="official")


def test_missing_single_numeric_csv_cell_is_not_filled_or_treated_as_zero(saved_sweep):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    rows[0]["fuel_used_kg_estimate"] = ""
    write_csv(path, rows)
    before = path.read_bytes()
    data = build_comparison(saved_sweep, comparison_id="official")
    assert data["runs"][0]["final"]["fuel_kg"] == 1
    assert read_csv(path)[0]["fuel_used_kg_estimate"] == ""
    assert path.read_bytes() == before


def test_incomplete_trial_table_still_requires_recomputed_groups_to_match_summary_csv(saved_sweep):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    rows[0]["sample_count"] = "0"
    rows[0]["final_target_range_m"] = ""
    write_csv(path, rows)
    groups_path = saved_sweep / "summary.csv"
    groups = read_csv(groups_path)
    groups[0]["median_final_target_range_m"] = "999"
    write_csv(groups_path, groups)
    with pytest.raises(ValueError, match="aggregate metrics"):
        build_comparison(saved_sweep, comparison_id="official")


def test_saved_summary_cannot_duplicate_another_trial_identity(saved_sweep):
    path = saved_sweep / "runs.csv"
    rows = read_csv(path)
    rows[1] = dict(rows[0])
    write_csv(path, rows)
    with pytest.raises(ValueError, match="duplicate"):
        build_comparison(saved_sweep, comparison_id="official")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(token=SECRET),
        lambda data: data.update(schema_version=True),
        lambda data: data["columns"].reverse(),
        lambda data: data["comparison"].update(source=SECRET),
        lambda data: data["comparison"].update(route_length_m=0),
        lambda data: data["comparison"].update(route_length_m=99),
        lambda data: data["comparison"]["basis"].update(normal_axis=[0, 1, 0]),
        lambda data: data["comparison"]["basis"].update(lateral_axis=[0, -1, 0]),
        lambda data: data["comparison"]["basis"].update(origin_reference_m=[1, 2]),
        lambda data: data["comparison"]["limitations"].append(SECRET),
        lambda data: data["runs"][0].update(camera=SECRET),
        lambda data: data["runs"][0].update(algorithm="ali_official_profile", owner="WSO"),
        lambda data: data["runs"][0].update(source_sample_count=True),
        lambda data: data["runs"][0]["samples"][1].pop(),
        lambda data: data["runs"][0]["samples"][1].__setitem__(4, -1),
        lambda data: data["runs"][0]["samples"][1].__setitem__(4, float("nan")),
        lambda data: data["runs"][0]["samples"][1].__setitem__(8, 99),
        lambda data: data["runs"][0]["samples"][1].__setitem__(0, 0),
        lambda data: data["runs"][0]["samples"][0].__setitem__(1, 1),
        lambda data: data["runs"][0]["samples"][1].__setitem__(1, None),
        lambda data: data["runs"][0]["final"].update(time_s=3),
        lambda data: data["runs"][1]["events"].pop(0),
        lambda data: data["runs"][0]["source_sha256"].update(trace="invalid"),
        lambda data: data["runs"][0]["source_sha256"].update(source=SECRET),
        lambda data: data["runs"][1].update(repetition=1),
        lambda data: data["runs"].pop(),
    ],
)
def test_public_validator_rejects_unknown_fields_malformed_shapes_and_invalid_values(
    saved_sweep, mutation
):
    data = build_comparison(saved_sweep, comparison_id="official")
    mutation(data)
    with pytest.raises(ValueError):
        validate_comparison(data)


def test_public_validator_detaches_results_without_rewriting_input(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    detached = validate_comparison(data)
    detached["runs"][0]["samples"][0][1] = 1
    assert data["runs"][0]["samples"][0][1] == 0


def test_public_run_and_sample_caps_and_four_mebibyte_encoding_are_enforced(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    data["runs"] = [deepcopy(data["runs"][0]) for _ in range(MAX_RUNS + 1)]
    with pytest.raises(ValueError, match="128 runs"):
        validate_comparison(data)
    data = build_comparison(saved_sweep, comparison_id="official")
    data["runs"][0]["samples"] = [data["runs"][0]["samples"][0]] * (MAX_SAMPLES + 1)
    with pytest.raises(ValueError, match="sample budget"):
        validate_comparison(data)
    with pytest.raises(ValueError, match="4 MiB"):
        publication._encode({"not_a_publication": "x" * MAX_BYTES})


def test_file_budget_thins_actual_observations_instead_of_rounding_or_synthetic_rows(
    tmp_path, monkeypatch
):
    directory = make_sweep(tmp_path, duration=80)
    full = build_comparison(directory, comparison_id="official")
    full_size = len(publication._encode(full))
    monkeypatch.setattr(publication, "MAX_BYTES", full_size * 3 // 4)
    smaller = build_comparison(directory, comparison_id="official")
    assert len(publication._encode(smaller)) <= full_size * 3 // 4
    for before, after in zip(full["runs"], smaller["runs"], strict=True):
        assert after["final"] == before["final"]
        assert after["events"] == before["events"]
        assert after["source_sample_count"] == before["source_sample_count"]
        assert all(row in before["samples"] for row in after["samples"])
    assert sum(len(run["samples"]) for run in smaller["runs"]) < sum(
        len(run["samples"]) for run in full["runs"]
    )


def test_required_evidence_over_file_budget_is_refused_before_writing(
    saved_sweep, tmp_path, monkeypatch
):
    monkeypatch.setattr(publication, "MAX_BYTES", 10)
    output = tmp_path / "public" / "official.json"
    with pytest.raises(ValueError, match="4 MiB"):
        write_comparison(saved_sweep, output, comparison_id="official")
    assert not output.parent.exists()


def test_deterministic_writes_manifest_hashes_and_upsert_preserve_other_entry(
    saved_sweep, tmp_path
):
    output = tmp_path / "public" / "official.json"
    manifest = output.with_name("manifest.json")
    before = snapshot(saved_sweep)
    first = write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    original = snapshot(output.parent)
    assert (
        write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest) == first
    )
    assert snapshot(output.parent) == original
    team = output.with_name("team.json")
    write_comparison(saved_sweep, team, comparison_id="team", manifest=manifest)
    records = read_json(manifest)
    assert set(records) == {"schema", "schema_version", "artifacts"}
    assert records["schema"] == "wsorb.path_comparison.artifacts"
    assert records["schema_version"] == 1
    assert [row["id"] for row in records["artifacts"]] == ["official", "official-trials", "team"]
    for row in records["artifacts"]:
        raw = (output.parent / row["path"]).read_bytes()
        path = "official-trials.csv" if row["id"] == "official-trials" else f"{row['id']}.json"
        assert row == {
            "id": row["id"],
            "path": path,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        assert len(raw) <= MAX_BYTES
        assert SECRET.encode() not in raw
        assert str(tmp_path).encode() not in raw
    assert output.read_bytes() == original["official.json"]
    assert before == snapshot(saved_sweep)


def test_corrected_official_trials_csv_is_allowlisted_and_matches_exported_finals(
    saved_sweep, tmp_path
):
    source_path = saved_sweep / "runs.csv"
    source_rows = read_csv(source_path)
    numeric_fields = (
        "elapsed_sim_s",
        "final_target_range_m",
        "final_relative_speed_mps",
        "fuel_used_kg_estimate",
        "max_damage_j_estimate",
        "first_damage_after_s",
        "first_damage_by_s",
    )
    for row in source_rows:
        row["sample_count"] = "0"
        row.update(dict.fromkeys(numeric_fields, ""))
    write_csv(source_path, source_rows)
    before = snapshot(saved_sweep)
    output = tmp_path / "public" / "official.json"
    data = write_comparison(
        saved_sweep, output, comparison_id="official", manifest=output.with_name("manifest.json")
    )
    corrected_path = output.with_name("official-trials.csv")
    rows = read_csv(corrected_path)
    assert (
        list(rows[0])
        == list(TRIAL_COLUMNS)
        == [
            "id",
            "algorithm",
            "owner",
            "label",
            "seed",
            "repetition",
            "outcome",
            "time_s",
            "range_m",
            "relative_speed_mps",
            "fuel_kg",
            "damage_j",
            "source_sample_count",
        ]
    )
    assert len(rows) == len(data["runs"])
    for exported, row in zip(data["runs"], rows, strict=True):
        for key in ("id", "algorithm", "owner", "label", "outcome"):
            assert row[key] == exported[key]
        for key in ("seed", "repetition", "source_sample_count"):
            assert int(row[key]) == exported[key]
        for key, value in exported["final"].items():
            if value is None:
                assert row[key] == ""
            else:
                assert float(row[key]) == value
    assert rows[0]["time_s"] == "4.0"
    assert rows[0]["fuel_kg"] == "1.0"
    assert rows[0]["source_sample_count"] == "5"
    assert SECRET.encode() not in corrected_path.read_bytes()
    assert str(tmp_path).encode() not in corrected_path.read_bytes()
    assert before == snapshot(saved_sweep)


def test_corrected_official_csv_keeps_missing_final_measurements_blank(saved_sweep, tmp_path):
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: rows[-1]["observation"]["mission"].update(fuel_used_kg=None, damage=None),
    )
    write_summaries(saved_sweep)
    output = tmp_path / "public" / "official.json"
    data = write_comparison(saved_sweep, output, comparison_id="official")
    row = read_csv(output.with_name("official-trials.csv"))[0]
    assert data["runs"][0]["final"]["fuel_kg"] is None
    assert data["runs"][0]["final"]["damage_j"] is None
    assert row["fuel_kg"] == ""
    assert row["damage_j"] == ""
    assert float(row["time_s"]) == 4


def test_team_export_does_not_create_or_replace_corrected_official_evidence(saved_sweep, tmp_path):
    output = tmp_path / "public" / "team.json"
    write_comparison(saved_sweep, output, comparison_id="team")
    assert not output.with_name("official-trials.csv").exists()


def test_unverified_corrected_official_csv_is_not_overwritten(saved_sweep, tmp_path):
    output = tmp_path / "public" / "official.json"
    output.parent.mkdir()
    corrected = output.with_name("official-trials.csv")
    corrected.write_text("owner work")
    with pytest.raises(FileExistsError):
        write_comparison(
            saved_sweep,
            output,
            comparison_id="official",
            manifest=output.with_name("manifest.json"),
        )
    assert corrected.read_text() == "owner work"
    assert not output.exists()
    assert not output.with_name("manifest.json").exists()


def test_manifest_hash_alone_cannot_claim_corrected_csv_that_disagrees_with_json(
    saved_sweep, tmp_path
):
    output = tmp_path / "public" / "official.json"
    manifest = output.with_name("manifest.json")
    write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    corrected = output.with_name("official-trials.csv")
    rows = read_csv(corrected)
    rows[0]["fuel_kg"] = "1234"
    write_csv(corrected, rows)
    raw = corrected.read_bytes()
    records = read_json(manifest)
    entry = next(row for row in records["artifacts"] if row["id"] == "official-trials")
    entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    write_json(manifest, records)
    before = snapshot(output.parent)
    with pytest.raises(ValueError, match="Corrected trial CSV disagrees"):
        write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    assert before == snapshot(output.parent)


def test_manifest_verified_regeneration_changes_only_generated_files(saved_sweep, tmp_path):
    output = tmp_path / "public" / "official.json"
    manifest = output.with_name("manifest.json")
    write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    (output.parent / "owner-notes.txt").write_text("preserve user work")
    mutate_trace(
        saved_sweep, 0, lambda rows: rows[-1]["observation"]["mission"].update(fuel_used_kg=2)
    )
    write_summaries(saved_sweep)
    data = write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    assert data["runs"][0]["final"]["fuel_kg"] == 2
    assert float(read_csv(output.with_name("official-trials.csv"))[0]["fuel_kg"]) == 2
    assert (output.parent / "owner-notes.txt").read_text() == "preserve user work"
    output.write_text("owner changed this file")
    before = snapshot(output.parent)
    with pytest.raises(ValueError, match="possible user work"):
        write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    assert before == snapshot(output.parent)


def test_existing_unverified_files_and_raw_nested_output_are_refused(saved_sweep, tmp_path):
    output = tmp_path / "owner.json"
    output.write_text("owner work")
    with pytest.raises(FileExistsError):
        write_comparison(saved_sweep, output, comparison_id="official")
    assert output.read_text() == "owner work"
    with pytest.raises(ValueError, match="outside raw evidence"):
        write_comparison(saved_sweep, saved_sweep / "official.json", comparison_id="official")


def test_historical_publications_are_explicitly_not_refresh_targets(saved_sweep):
    for name in ("official-comparison", "team-comparison"):
        with pytest.raises(ValueError, match="historical publications"):
            write_comparison(
                saved_sweep,
                ROOT / "docs" / "analytics" / name / "new.json",
                comparison_id="official",
            )


def test_output_source_and_manifest_symlinks_are_refused(saved_sweep, tmp_path):
    target = tmp_path / "owner.json"
    target.write_text("owner work")
    link = tmp_path / "official.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        write_comparison(saved_sweep, link, comparison_id="official")
    assert target.read_text() == "owner work"
    source = tmp_path / "source-link"
    source.symlink_to(saved_sweep, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        build_comparison(source, comparison_id="official")
    manifest = tmp_path / "manifest.json"
    manifest.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        write_comparison(
            saved_sweep,
            tmp_path / "new" / "official.json",
            comparison_id="official",
            manifest=manifest,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data.update(token=SECRET),
        lambda data: data["artifacts"][0].update(path="../owner.json"),
        lambda data: data["artifacts"][0].update(bytes=MAX_BYTES + 1),
        lambda data: data["artifacts"].append(dict(data["artifacts"][0])),
    ],
)
def test_manifest_shape_paths_counts_and_bounds_are_strict(saved_sweep, tmp_path, mutation):
    output = tmp_path / "public" / "official.json"
    manifest = output.with_name("manifest.json")
    write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    data = read_json(manifest)
    mutation(data)
    write_json(manifest, data)
    before = snapshot(output.parent)
    with pytest.raises(ValueError):
        write_comparison(saved_sweep, output, comparison_id="official", manifest=manifest)
    assert before == snapshot(output.parent)


def test_no_private_fields_or_unreviewed_strings_can_reach_curated_json(saved_sweep):
    data = build_comparison(saved_sweep, comparison_id="official")
    encoded = json.dumps(data, allow_nan=False)
    assert SECRET not in encoded
    forbidden = {
        "radar",
        "radar_backup",
        "camera",
        "cameras",
        "descriptor",
        "descriptors",
        "source",
        "package_sources",
        "exception",
        "error",
        "token",
        "debris_truth",
        "applied_physics",
        "observation",
        "settings",
    }

    def walk(value):
        if isinstance(value, dict):
            assert not forbidden & value.keys()
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    notes = " ".join(data["comparison"]["limitations"])
    assert "Camera, raw radar" in notes
    assert "hidden debris are absent" in notes


def test_cli_supports_explicit_id_and_manifest_and_redacts_source_error_strings(
    saved_sweep, tmp_path, capsys
):
    output = tmp_path / "public" / "official.json"
    manifest = output.with_name("manifest.json")
    assert (
        main(
            [
                "--input",
                str(saved_sweep),
                "--output",
                str(output),
                "--id",
                "official",
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )
    assert str(output) in capsys.readouterr().out
    mutate_trace(
        saved_sweep, 0, lambda rows: rows[1]["observation"]["mission"].update(damage=SECRET)
    )
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--input",
                str(saved_sweep),
                "--output",
                str(tmp_path / "refused.json"),
                "--id",
                "official",
            ]
        )
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "Path publication failed" in captured.err
    assert SECRET not in captured.err
    assert not (tmp_path / "refused.json").exists()


def test_historical_publication_evidence_identifies_distinct_worlds_and_preserves_alias():
    official = read_json(ROOT / "docs/analytics/official-comparison/manifest.json")
    team = read_json(ROOT / "docs/analytics/team-comparison/dataset.json")
    assert official["preset"] == "competition_reference"
    assert official["debris_distribution"] == {"kind": "official_cloud"}
    assert official["engine_build"]["sha256"] != team["comparison"]["engine_build"]["sha256"]
    assert "wso_official_profile" in {
        row["algorithm"]
        for row in read_csv(ROOT / "docs/analytics/official-comparison/summary.csv")
    }


def test_original_official_trial_csv_is_incomplete_not_a_zero_measurement_source():
    rows = read_csv(ROOT / "docs/analytics/official-comparison/runs.csv")
    assert len(rows) == 30
    assert all(row["sample_count"] == "0" for row in rows)
    assert all(
        row[key] == ""
        for row in rows
        for key in (
            "elapsed_sim_s",
            "final_target_range_m",
            "final_relative_speed_mps",
            "fuel_used_kg_estimate",
            "max_damage_j_estimate",
        )
    )
    groups = read_csv(ROOT / "docs/analytics/official-comparison/summary.csv")
    assert any(
        float(row["median_final_target_range_m"]) > 0
        for row in groups
        if row["median_final_target_range_m"] != ""
    )
