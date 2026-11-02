"""Pure saved-data publication fixtures. None of these are benchmark flights."""

import csv
import hashlib
import json
from copy import deepcopy
from xml.etree import ElementTree as ET

import pytest

from wsorb.flight_analytics import COLUMNS, DEFINITION_ID, EPISODES, validate_run_analytics
from wsorb.scoreboard import (
    build_bundle,
    main,
    publish_scoreboard,
    refresh_scoreboard,
    render_readme_section,
)
from wsorb.scoring import FORMULA, score_compatibility

SECRET = "PRIVATE_FIXTURE_TOKEN_https://private.invalid/do-not-publish"
PROFILES = ("basic_pd_radar", "bui_alpha_beta_mvp", "tom_kalman_velocity_mvp")
DEFINITIONS = {
    "schema_version": 1,
    "definition_id": DEFINITION_ID,
    "burn_force_threshold_n": 1e-6,
    "angular_torque_threshold_nm": 1e-6,
    "turn_speed_threshold_rad_s": 1e-3,
}
BUILD = {"bytes": 24, "sha256": hashlib.sha256(b"pure fixture, not WASM").hexdigest()}


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False))


def native(now, profile):
    turn = profile != PROFILES[0]
    result = {
        "schema_version": 1,
        "definition_id": DEFINITION_ID,
        "observed_sim_s": now,
        "total_turn_angle_rad": now * 0.2 if turn else 0,
        "peak_angular_speed_rad_s": 0.2 if now and turn else 0,
        "propellant_impulse_ns": now * 5,
        "angular_impulse_nms": now if turn else 0,
    }
    for name in EPISODES:
        active = bool(now) and (name == "burn" or turn)
        result.update(
            {
                f"{name}_count": int(active),
                f"{name}_duration_s": now if active else 0,
                f"longest_{name}_s": now if active else 0,
                f"current_{name}_s": now if active else 0,
                f"{name}_active": active,
            }
        )
    return result


def terms(score, success):
    # Explicit receipt fixture, not another implementation of the score formula.
    return {
        "formula": FORMULA,
        "progress_basis": "time_ratio" if success else "remaining_distance_ratio",
        "progress_points": 1000,
        "collision_penalty_points": 0,
        "fuel_penalty_points": 0,
        "offset_points": score - 1000,
        "total": score,
        "nominal_time_s": 600,
        "nominal_fuel_kg": 30,
        "impact_threshold_j": 5000,
    }


@pytest.fixture
def saved_sweep(tmp_path):
    directory = tmp_path / "private-run"
    directory.mkdir()
    config = {
        "schema_version": 1,
        "preset": "far_side_l2",
        "seeds": [100, 101],
        "debris_counts": [500],
        "duration_s": 2,
        "step_s": 0.5,
        "settings": {"ship": {"mass_kg": 1000}, "debris": {"count": 500}},
        "algorithms": {name: {} for name in PROFILES},
    }
    write_json(directory / "configuration.json", config)
    write_json(
        directory / "python.json",
        {
            "analytics_definitions": DEFINITIONS,
            "uv_lock_sha256": "c" * 64,
            "reviewed_example_hashes": {
                "examples/bui/mvp/guidance.py": "a" * 64,
                "examples/tom/avoidance.py": "b" * 64,
            },
            "package_sources": {
                "src/wsorb/flight/control.py": {
                    "sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
                    "source": SECRET,
                }
            },
        },
    )
    write_json(directory / "policies.json", {"private_factory_source": SECRET})
    for seed in config["seeds"]:
        for index, profile in enumerate(PROFILES):
            path = directory / f"trial-{len(list(directory.glob('trial-*'))):05d}"
            path.mkdir()
            outcome, score = (
                "success",
                {PROFILES[0]: 50 if seed == 100 else 100, PROFILES[1]: 200, PROFILES[2]: None}[
                    profile
                ],
            )
            if profile == PROFILES[1] and seed == 101:
                outcome, score = "failed_damage", -20
            elif profile == PROFILES[2]:
                outcome = "timeout" if seed == 100 else "policy_error"
            run = {
                "schema_version": 1,
                "execution_kind": "owned_native_manual_step",
                "algorithm": profile,
                "algorithm_settings": {},
                "seed": seed,
                "repetition": 1,
                "debris_count": 500,
                "preset": config["preset"],
                "duration_s": 2,
                "step_s": 0.5,
                "settings": deepcopy(config["settings"]),
                "outcome": outcome,
                "error": SECRET if "error" in outcome else None,
                "engine": {
                    "engine_build": BUILD,
                    "wasm_sha256": BUILD["sha256"],
                    "backend": "native_headless",
                    "control_mode": "owned_native_API_manual_step",
                    "execution_kind": "owned_native_manual_step",
                    "scenario_source": {"integration": {"fixed_step_s": 0.05}},
                    "scenario_source_sha256": "d" * 64,
                },
            }
            write_json(path / "run.json", run)
            trace = []
            times = [0, 0.5] if outcome == "policy_error" else [0, 0.5, 1, 1.5, 2]
            for now in times:
                final = now == 2 and outcome in ("success", "failed_damage")
                mission = {
                    "status": outcome if final else "running",
                    "target_distance_m": 1000 / (now + 1),
                    "relative_speed_mps": 1 + index,
                    "fuel_used_kg": now * (index + 1) / 10,
                    "damage": 6000 if final and outcome == "failed_damage" else 0,
                    "flight_analytics": native(now, profile),
                    "score": score if final else None,
                    "score_breakdown": terms(score, outcome == "success") if final else None,
                }
                trace.append(
                    {
                        "observation": {
                            "time_s": now,
                            "seed": seed,
                            "trial_revision": 1,
                            "tuning_revision": 0,
                            "manual_interventions": 0,
                            "mission": mission,
                            "radar": SECRET,
                            "debris_truth": SECRET,
                            "camera": SECRET,
                            "payload": SECRET,
                        },
                        "accel_command": [0, 0, 0],
                        "private": SECRET,
                    }
                )
            (path / "trace.jsonl").write_text("".join(json.dumps(row) + "\n" for row in trace))
    return directory


@pytest.fixture
def paired_sweep(saved_sweep):
    # Expand only saved-data fixtures. This does not start or emulate a physics engine.
    originals = [snapshot(path) for path in sorted(saved_sweep.glob("trial-*"))]
    config_path = saved_sweep / "configuration.json"
    config = json.loads(config_path.read_text())
    config["repetitions"] = 2
    write_json(config_path, config)
    for index, original in enumerate(originals):
        for repetition in (1, 2):
            path = saved_sweep / f"trial-{index * 2 + repetition - 1:05d}"
            path.mkdir(exist_ok=True)
            for name, value in original.items():
                (path / name).write_bytes(value)
            mutate_run(
                saved_sweep,
                index * 2 + repetition - 1,
                lambda run, repetition=repetition: run.update(repetition=repetition),
            )
            if repetition == 2:

                def second_score(rows):
                    mission = rows[-1]["observation"]["mission"]
                    score = mission["score"]
                    if score is not None:
                        score = {50: 150, 100: 110, 200: 220, -20: -10}[score]
                        mission["score"] = score
                        mission["score_breakdown"] = terms(score, mission["status"] == "success")

                mutate_trace(saved_sweep, index * 2 + repetition - 1, second_score)
    return saved_sweep


def mutate_run(directory, index, mutate):
    path = directory / f"trial-{index:05d}" / "run.json"
    value = json.loads(path.read_text())
    mutate(value)
    write_json(path, value)


def mutate_trace(directory, index, mutate):
    path = directory / f"trial-{index:05d}" / "trace.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    mutate(rows)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def snapshot(directory):
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_bundle_has_fixed_public_schema_and_retains_every_outcome(saved_sweep):
    before = snapshot(saved_sweep)
    bundle = build_bundle(saved_sweep)
    assert set(bundle) == {"schema", "schema_version", "comparison", "profiles", "trials"}
    assert bundle["schema"] == "wsorb.teammate_analytics"
    assert bundle["schema_version"] == 2
    comparison = bundle["comparison"]
    assert set(comparison) == {
        "preset",
        "seeds",
        "debris_count",
        "duration_limit_s",
        "controller_step_s",
        "physics_step_s",
        "execution_kind",
        "score_formula_id",
        "score_compatibility",
        "engine_build",
        "definitions",
        "source_hashes",
        "ranking_method",
        "limitations",
        "repetitions",
    }
    assert comparison["engine_build"] == BUILD
    assert comparison["definitions"] == DEFINITIONS
    assert comparison["score_formula_id"] == FORMULA
    assert comparison["score_compatibility"] == score_compatibility()
    assert comparison["score_compatibility"]["stp_points_available"] is False
    assert len(bundle["trials"]) == 6
    assert {row["outcome"] for row in bundle["trials"]} == {
        "success",
        "failed_damage",
        "timeout",
        "policy_error",
    }
    assert SECRET not in json.dumps(bundle, allow_nan=False)
    for row in bundle["trials"]:
        assert set(row) == {
            "id",
            "profile_id",
            "owner",
            "label",
            "seed",
            "outcome",
            "score",
            "score_breakdown",
            "measurement",
            "analytics",
            "engine_sha256",
            "repetition",
        }
        assert validate_run_analytics(row["analytics"]) == row["analytics"]
        assert row["analytics"]["sample_count"] <= 192
        assert set(row["measurement"]) == {
            "time_s",
            "range_m",
            "relative_speed_mps",
            "damage_j",
            "fuel_used_kg",
        }
    assert before == snapshot(saved_sweep)


def test_medians_rank_recorded_scores_and_do_not_mix_error_partial_metrics(saved_sweep):
    bundle = build_bundle(saved_sweep)
    assert [row["id"] for row in bundle["profiles"]] == list(PROFILES)
    harry, bui, tom = bundle["profiles"]
    assert bui["median_score"] == 90
    assert bui["successes"] == 1
    assert bui["median_damage_j"] == 3000
    assert harry["median_score"] == 75
    assert harry["successes"] == harry["scored_runs"] == 2
    assert tom["attempts"] == 2 and tom["physical_runs"] == 1
    assert tom["median_score"] is None
    assert tom["unscored_runs"] == 2
    assert tom["median_time_s"] == 2
    assert tom["median_fuel_used_kg"] == pytest.approx(0.6)
    assert tom["analytics_runs"] == 1
    assert tom["metric_runs"]["time_s"] == 1
    assert tom["outcomes"] == {"policy_error": 1, "timeout": 1}
    assert harry["median_turn_count"] == 0
    assert bui["median_turn_count"] == 1
    assert harry["mean_score"] is None and harry["scored_pairs"] == 0
    assert "Unranked" in bundle["comparison"]["ranking_method"]


def test_primary_mean_uses_only_complete_two_run_pairs_and_preserves_pair_outcomes(paired_sweep):
    bundle = build_bundle(paired_sweep)
    harry, bui, tom = bundle["profiles"]
    assert bundle["comparison"]["repetitions"] == 2
    assert len(bundle["trials"]) == 12
    assert harry["attempts"] == 4 and harry["successes"] == 4
    assert harry["scored_pairs"] == 2 and harry["mean_score"] == 102.5
    assert harry["median_score"] == 105
    assert harry["paired_scores"][0] == {
        "seed": 100,
        "run_ids": ["trial-00000", "trial-00001"],
        "mean_score": 100,
        "outcomes": ["success", "success"],
    }
    assert bui["mean_score"] == 97.5
    assert bui["paired_scores"][1]["outcomes"] == ["failed_damage", "failed_damage"]
    assert bui["paired_scores"][1]["mean_score"] == -15
    assert tom["scored_pairs"] == 0 and tom["mean_score"] is None
    assert tom["unscored_runs"] == 4
    assert all(pair["mean_score"] is None for pair in tom["paired_scores"])
    assert "Unranked" in bundle["comparison"]["ranking_method"]
    section = render_readme_section(bundle)
    assert "Harry / Basic PD + radar | 4/4 | 2 |" in section
    assert "Mean two-run score" not in section
    assert "STP points are unavailable" in section
    assert "2 matched seeds with 2 consecutive repeats are not 4 independent" in section
    assert "these profiles are unranked" in section
    assert "docs/flight-lab.md#reading-the-comparison" in section


def test_paired_publication_matches_ui_profile_and_retained_sample_contract(paired_sweep):
    bundle = build_bundle(paired_sweep)
    required = {
        "id",
        "owner",
        "label",
        "attempts",
        "successes",
        "scored_runs",
        "scored_pairs",
        "mean_score",
        "median_time_s",
        "median_fuel_used_kg",
        "median_damage_j",
        "median_burn_count",
        "median_burn_duration_s",
        "median_angular_burn_count",
        "median_angular_burn_duration_s",
        "median_turn_count",
        "median_turn_duration_s",
        "median_total_turn_angle_rad",
    }
    optional = {
        "median_score",
        "min_score",
        "max_score",
        "score_stddev",
        "success_rate",
        "median_range_m",
        "median_relative_speed_mps",
        "median_longest_burn_s",
        "median_longest_angular_burn_s",
        "median_longest_turn_s",
        "median_peak_angular_speed_rad_s",
        "median_propellant_impulse_ns",
        "median_angular_impulse_nms",
        "unscored_runs",
        "physical_runs",
        "analytics_runs",
        "paired_scores",
        "outcomes",
        "metric_runs",
    }
    assert bundle["comparison"]["repetitions"] == 2
    assert len(bundle["comparison"]["source_hashes"]) <= 128
    for profile in bundle["profiles"]:
        assert required <= set(profile) <= required | optional
        assert set(profile["metric_runs"]) == {
            "time_s",
            "range_m",
            "relative_speed_mps",
            "damage_j",
            "fuel_used_kg",
        }
        assert len(profile["paired_scores"]) == len(bundle["comparison"]["seeds"])
        for pair in profile["paired_scores"]:
            assert set(pair) == {"seed", "run_ids", "mean_score", "outcomes"}
            assert len(pair["run_ids"]) == len(pair["outcomes"]) == 2
    for trial in bundle["trials"]:
        if trial["analytics"] is not None:
            report = trial["analytics"]
            assert report["sample_count"] == len(report["samples"]) <= 192


def test_publication_refuses_excess_source_hashes_instead_of_dropping_provenance(saved_sweep):
    path = saved_sweep / "python.json"
    evidence = json.loads(path.read_text())
    evidence["reviewed_example_hashes"] = {
        f"examples/bui/mvp/fixture_{index}.py": "a" * 64 for index in range(129)
    }
    write_json(path, evidence)
    with pytest.raises(ValueError, match="128 source hashes"):
        build_bundle(saved_sweep)


def test_one_valid_score_does_not_fill_a_partial_pair(paired_sweep):
    mutate_run(paired_sweep, 1, lambda run: run.update(outcome="policy_error"))
    mutate_trace(
        paired_sweep,
        1,
        lambda rows: rows[-1]["observation"]["mission"].update(score=None, score_breakdown=None),
    )
    bundle = build_bundle(paired_sweep)
    harry = next(row for row in bundle["profiles"] if row["id"] == PROFILES[0])
    assert harry["scored_pairs"] == 1
    assert harry["paired_scores"][0]["mean_score"] is None
    assert harry["mean_score"] == 105
    assert harry["scored_runs"] == 3 and harry["unscored_runs"] == 1


def test_complete_matched_pair_coverage_cannot_make_si_counters_stp_comparable(paired_sweep):
    for index in (4, 5, 10, 11):
        mutate_run(paired_sweep, index, lambda run: run.update(outcome="failed_damage"))
        mutate_trace(
            paired_sweep,
            index,
            lambda rows: rows[-1]["observation"]["mission"].update(
                status="failed_damage", score=-30, score_breakdown=terms(-30, False)
            ),
        )
    bundle = build_bundle(paired_sweep)
    assert "Unranked" in bundle["comparison"]["ranking_method"]
    assert "descending" not in bundle["comparison"]["ranking_method"]
    assert bundle["comparison"]["score_compatibility"]["stp_points_available"] is False
    assert "not an official aggregate" in bundle["comparison"]["ranking_method"]
    assert [row["id"] for row in bundle["profiles"]] == list(PROFILES)
    assert bundle["profiles"][-1]["mean_score"] == -30


def test_nonconsecutive_or_duplicated_pair_repetitions_are_refused(paired_sweep):
    mutate_run(paired_sweep, 1, lambda run: run.update(repetition=1))
    with pytest.raises(ValueError, match="consecutive repetition"):
        build_bundle(paired_sweep)


def test_actual_engine_definition_and_hash_are_used_without_current_file_dependency(saved_sweep):
    evidence_path = saved_sweep / "python.json"
    evidence = json.loads(evidence_path.read_text())
    evidence.pop("analytics_definitions")
    write_json(evidence_path, evidence)
    for index in range(6):
        mutate_run(
            saved_sweep,
            index,
            lambda run: run["engine"].update(
                analytics_definition=DEFINITIONS, analytics_definition_sha256="f" * 64
            ),
        )
    bundle = build_bundle(saved_sweep)
    assert bundle["comparison"]["definitions"] == DEFINITIONS
    assert (
        bundle["comparison"]["source_hashes"]["simulation/scenarios/flight_analytics.json"]
        == "f" * 64
    )
    assert "examples/bui/mvp/guidance.py" in bundle["comparison"]["source_hashes"]
    assert "examples/tom/avoidance.py" in bundle["comparison"]["source_hashes"]


def test_new_output_is_deterministic_private_safe_and_has_actual_artifact_digests(
    saved_sweep, tmp_path
):
    first, second = tmp_path / "public-a", tmp_path / "public-b"
    before = snapshot(saved_sweep)
    a = publish_scoreboard(saved_sweep, first)
    assert publish_scoreboard(saved_sweep, second) == a
    assert snapshot(first) == snapshot(second)
    assert before == snapshot(saved_sweep)
    assert set(snapshot(first)) == {
        "dataset.json",
        "trials.csv",
        "aggregate.csv",
        "samples.csv",
        "score-components.svg",
        "fuel-burns.svg",
        "turns.svg",
        "efficiency.svg",
        "manifest.json",
    }
    for value in snapshot(first).values():
        assert SECRET.encode() not in value
        assert str(tmp_path).encode() not in value
        assert b"debris_truth" not in value
    manifest = json.loads((first / "manifest.json").read_text())
    assert (
        manifest["provenance_revision"]
        == hashlib.sha256((first / "dataset.json").read_bytes()).hexdigest()
    )
    for entry in manifest["artifacts"]:
        value = (first / entry["path"]).read_bytes()
        assert entry["bytes"] == len(value)
        assert entry["sha256"] == hashlib.sha256(value).hexdigest()
    with (first / "trials.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 6
    assert any(row["outcome"] == "failed_damage" and row["score"] == "-20.0" for row in rows)
    assert all(row["score"] == "" for row in rows if row["outcome"] in ("timeout", "policy_error"))
    with (first / "samples.csv").open() as stream:
        assert next(csv.reader(stream)) == [
            "trial_id",
            "profile_id",
            "seed",
            "repetition",
            *COLUMNS,
        ]


def test_svg_titles_descriptions_values_and_no_script_or_external_resources(saved_sweep, tmp_path):
    output = tmp_path / "public"
    publish_scoreboard(saved_sweep, output)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    for path in output.glob("*.svg"):
        text = path.read_text()
        root = ET.fromstring(text)
        assert root.attrib["role"] == "img"
        assert root.find("svg:title", ns).text
        assert root.find("svg:desc", ns).text
        assert "PRIVATE" not in text
        assert "<script" not in text and "href=" not in text
        assert "Harry" in text and "Bui" in text and "Tom" in text
        assert "viewBox" in root.attrib
    legacy = (output / "score-components.svg").read_text()
    assert "90 legacy diagnostic units" in legacy
    assert "not STP points" in legacy
    assert "local physical fuel is not the official fuel counter" in legacy
    assert "1/2" in (output / "efficiency.svg").read_text()


def test_svg_missing_measurements_break_paths_and_text_is_escaped():
    from wsorb.scoreboard import _draw_svg

    svg = _draw_svg(
        "Pure <plot> fixture",
        "Missing is not zero.",
        [
            {
                "title": "Recorded fixture",
                "unit": "kg",
                "series": [
                    {
                        "profile": 0,
                        "seed": 0,
                        "label": "A < B & C",
                        "points": [(0, 0), (1, None), (2, 2)],
                    }
                ],
            }
        ],
        [{"owner": "Fixture", "label": "No physics claimed"}],
    )
    root = ET.fromstring(svg)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    paths = [path for path in root.findall("svg:path", ns) if "stroke-dasharray" in path.attrib]
    assert len(paths) == 2
    assert all("L" not in path.attrib["d"] for path in paths)
    assert "Pure &lt;plot&gt; fixture" in svg
    assert "A &lt; B &amp; C" in svg


def test_finite_extreme_plot_values_do_not_overflow_svg_coordinates():
    from wsorb.scoreboard import _draw_svg

    svg = _draw_svg(
        "Pure numeric bounds fixture",
        "Not a physical measurement.",
        [{"title": "Signed bounds", "unit": "points", "values": [-1.79e308, 1.79e308]}],
        [{"owner": "A", "label": "Fixture"}, {"owner": "B", "label": "Fixture"}],
    )
    root = ET.fromstring(svg)
    for element in root.iter():
        for key in ("x", "y", "cx", "cy", "width", "height"):
            raw = element.attrib.get(key)
            if raw is not None and not raw.endswith("%"):
                assert abs(float(raw)) < 2000
    ticks = [
        element
        for element in root.iter()
        if element.attrib.get("text-anchor") == "end" and (element.text or "").startswith("-")
    ]
    assert ticks
    assert all(float(element.attrib["x"]) >= 120 for element in ticks)


def test_chart_text_and_profile_strokes_have_measured_contrast_on_white():
    from wsorb.scoreboard import _COLORS

    def contrast_on_white(color):
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        luminance = sum(
            weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear, strict=True)
        )
        return 1.05 / (luminance + 0.05)

    assert contrast_on_white("#172033") >= 4.5
    assert all(contrast_on_white(color) >= 3 for color in _COLORS)


def test_readme_renderer_includes_real_table_downloads_and_method_links(saved_sweep):
    bundle = build_bundle(saved_sweep)
    before = deepcopy(bundle)
    section = render_readme_section(bundle)
    assert "Bui / Alpha-beta + sidestep | 1/2 | 2 | 0.4 |" in section
    assert "Mean two-run score" not in section
    assert "score-components.svg" not in section
    assert "| Profile | Arrivals / attempts | Median duration, s | Estimated fuel, kg |" in section
    assert section.count("![") == 1
    assert "1 damage failure" in section and "1 policy error" in section
    assert "1 timeout" in section
    assert "partial timeout" in section
    assert "official leaderboard" in section
    assert "Native burn and turn measurements cover 5/5 physical results" in section
    assert "docs/flight-lab.md#reading-the-comparison" in section
    assert "docs/scoring.md" in section
    assert "docs/analytics/team-comparison/turns.svg" in section
    assert "docs/analytics/team-comparison/fuel-burns.svg" in section
    assert "aggregate.csv" in section
    assert "dataset.json" in section and "manifest.json" in section
    assert BUILD["sha256"] not in section
    assert bundle == before
    custom = render_readme_section(bundle, asset_prefix="assets/measured")
    assert "assets/measured/turns.svg" in custom
    with pytest.raises(ValueError, match="local relative"):
        render_readme_section(bundle, asset_prefix="https://private.invalid")


def test_legacy_native_analytics_and_score_terms_stay_unavailable(saved_sweep, tmp_path):
    for index in range(6):

        def remove(rows):
            for row in rows:
                del row["observation"]["mission"]["flight_analytics"]
                row["observation"]["mission"].pop("score_breakdown", None)

        mutate_trace(saved_sweep, index, remove)
    evidence_path = saved_sweep / "python.json"
    evidence = json.loads(evidence_path.read_text())
    evidence.pop("analytics_definitions")
    write_json(evidence_path, evidence)
    bundle = publish_scoreboard(saved_sweep, tmp_path / "legacy-public")
    assert bundle["comparison"]["definitions"] is None
    assert bundle["comparison"]["score_formula_id"] is None
    assert "Unranked" in bundle["comparison"]["ranking_method"]
    assert all(row["analytics"] is None for row in bundle["trials"])
    assert all(row["median_burn_count"] is None for row in bundle["profiles"])
    assert all(row["score_breakdown"] is None for row in bundle["trials"])
    assert any(row["score"] == -20 for row in bundle["trials"])
    assert "Unavailable, no recorded values" in (tmp_path / "legacy-public/turns.svg").read_text()
    assert "No burn or turn history" in render_readme_section(bundle)


def test_mixed_legacy_score_evidence_cannot_claim_formula_matched_ranking(saved_sweep):
    mutate_trace(
        saved_sweep, 0, lambda rows: rows[-1]["observation"]["mission"].pop("score_breakdown")
    )
    bundle = build_bundle(saved_sweep)
    assert bundle["comparison"]["score_formula_id"] is None
    assert "Unranked" in bundle["comparison"]["ranking_method"]
    assert any(row["score"] == 50 and row["score_breakdown"] is None for row in bundle["trials"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda run: run.update(step_s=0.25),
        lambda run: run.update(duration_s=3),
        lambda run: run.update(preset="mars_facing_l1"),
        lambda run: run.update(debris_count=250),
        lambda run: run.update(seed=999),
        lambda run: run.update(execution_kind="other_transport"),
        lambda run: run.update(algorithm_settings={"different": 1}),
        lambda run: run["settings"]["ship"].update(mass_kg=5000),
        lambda run: run["engine"]["engine_build"].update(bytes=25),
        lambda run: run["engine"].update(wasm_sha256="f" * 64),
        lambda run: run["engine"]["scenario_source"]["integration"].update(fixed_step_s=0.025),
        lambda run: run["engine"].update(scenario_source_sha256="e" * 64),
    ],
)
def test_mixed_cohorts_are_rejected_before_any_output(saved_sweep, tmp_path, mutation):
    mutate_run(saved_sweep, 0, mutation)
    output = tmp_path / "refused"
    with pytest.raises(ValueError):
        publish_scoreboard(saved_sweep, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "field", ["seed", "trial_revision", "tuning_revision", "manual_interventions"]
)
def test_changed_actual_source_is_not_published_as_matched_flight(saved_sweep, field):
    mutate_trace(saved_sweep, 0, lambda rows: rows[-1]["observation"].update({field: 2}))
    with pytest.raises(ValueError, match="source context"):
        build_bundle(saved_sweep)


def test_rewinds_mixed_formula_and_missing_attempts_are_refused(saved_sweep):
    original = snapshot(saved_sweep)
    mutate_trace(saved_sweep, 0, lambda rows: rows[-1]["observation"].update(time_s=0.25))
    with pytest.raises(ValueError, match="clock"):
        build_bundle(saved_sweep)
    (saved_sweep / "trial-00000/trace.jsonl").write_bytes(original["trial-00000/trace.jsonl"])
    mutate_trace(
        saved_sweep,
        0,
        lambda rows: rows[-1]["observation"]["mission"]["score_breakdown"].update(
            formula="other_formula"
        ),
    )
    with pytest.raises(ValueError, match="formula"):
        build_bundle(saved_sweep)
    (saved_sweep / "trial-00000/trace.jsonl").write_bytes(original["trial-00000/trace.jsonl"])
    config_path = saved_sweep / "configuration.json"
    config = json.loads(config_path.read_text())
    config["seeds"].append(102)
    write_json(config_path, config)
    with pytest.raises(ValueError, match="every paired"):
        build_bundle(saved_sweep)


def test_no_raw_or_existing_output_writes(saved_sweep, tmp_path):
    before = snapshot(saved_sweep)
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("user work")
    with pytest.raises(FileExistsError):
        publish_scoreboard(saved_sweep, existing)
    assert (existing / "keep.txt").read_text() == "user work"
    with pytest.raises(ValueError, match="outside"):
        publish_scoreboard(saved_sweep, saved_sweep / "nested-public")
    assert before == snapshot(saved_sweep)


def test_errors_without_engine_receipt_are_retained_with_unknown_engine(saved_sweep):
    mutate_run(saved_sweep, 5, lambda run: run.update(engine=None, outcome="engine_error"))
    trace = saved_sweep / "trial-00005/trace.jsonl"
    trace.write_text("")
    bundle = build_bundle(saved_sweep)
    error = bundle["trials"][-1]
    assert error["outcome"] == "engine_error"
    assert error["analytics"] is error["score"] is error["engine_sha256"] is None
    assert all(value is None for value in error["measurement"].values())


def test_partial_error_without_engine_receipt_does_not_claim_native_analytics(saved_sweep):
    mutate_run(saved_sweep, 5, lambda run: run.update(engine=None))
    bundle = build_bundle(saved_sweep)
    error = bundle["trials"][-1]
    assert error["outcome"] == "policy_error"
    assert error["measurement"]["time_s"] == 0.5
    assert error["analytics"] is error["engine_sha256"] is None


def test_publisher_cli_creates_only_the_new_bundle(saved_sweep, tmp_path, capsys):
    output = tmp_path / "cli-public"
    assert main(["--input", str(saved_sweep), "--output", str(output)]) == 0
    assert str(output) in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(["--input", str(saved_sweep), "--output", str(output)])
    assert error.value.code == 2


def test_explicit_refresh_replaces_only_verified_generated_files(saved_sweep, tmp_path):
    output = tmp_path / "public"
    publish_scoreboard(saved_sweep, output)
    (output / "owner-notes.txt").write_text("preserve user work")
    before = snapshot(saved_sweep)
    assert refresh_scoreboard(saved_sweep, output) == build_bundle(saved_sweep)
    assert (output / "owner-notes.txt").read_text() == "preserve user work"
    assert snapshot(saved_sweep) == before
    (output / "dataset.json").write_text("user-edited file")
    unchanged = snapshot(output)
    with pytest.raises(ValueError, match="refusing to overwrite"):
        refresh_scoreboard(saved_sweep, output)
    assert snapshot(output) == unchanged


def test_refresh_rejects_symlinks_and_unexpected_manifest_paths(saved_sweep, tmp_path):
    output = tmp_path / "public"
    publish_scoreboard(saved_sweep, output)
    link = tmp_path / "link"
    link.symlink_to(output, target_is_directory=True)
    with pytest.raises(ValueError, match="existing generated"):
        refresh_scoreboard(saved_sweep, link)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["artifacts"][0]["path"] = "../owner.txt"
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="known generated"):
        refresh_scoreboard(saved_sweep, output)
