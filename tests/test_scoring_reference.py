"""Offline documentation fixtures, not observed runs or another production scorer."""

from __future__ import annotations

import json
import math
from datetime import date
from importlib.resources import files
from pathlib import Path

import pytest

from wsorb.scoring import FORMULA, POINT_FIELDS, REFERENCE_FIELDS, native_score_breakdown

ROOT = Path(__file__).resolve().parents[1]
INPUT_FIELDS = (
    "elapsed_time_s",
    "damage_j",
    "fuel_counter",
    "distance_m",
    "initial_distance_m",
)
# Hand-worked values also documented in docs/scoring.md. No SDK, engine or scorer
# is substituted here. Native mission tests own execution-level formula checks.
EXAMPLES = [
    (
        "nominal_success",
        (120.0, 0.0, 1.15, 50.0, 10000.0),
        "success",
        "time_ratio",
        (100.0, 0.0, 0.0, 50.0, 150.0),
    ),
    (
        "wiki_example_under_code",
        (96.0, 10460.0, 1.035, 50.0, 10000.0),
        "success",
        "time_ratio",
        (125.0, 10.0, 0.0, 50.0, 165.0),
    ),
    (
        "negative_success",
        (120.0, 0.0, 9.2, 50.0, 10000.0),
        "success",
        "time_ratio",
        (100.0, 0.0, 700.0, 50.0, -550.0),
    ),
    (
        "damage_at_threshold",
        (120.0, 209200.0, 1.15, 50.0, 10000.0),
        "success",
        "time_ratio",
        (100.0, 200.0, 0.0, 50.0, -50.0),
    ),
    (
        "damage_failure",
        (120.0, 313800.0, 1.15, 5000.0, 10000.0),
        "failed_damage",
        "remaining_distance_ratio",
        (-50.0, 300.0, 0.0, 50.0, -300.0),
    ),
    (
        "damage_preempts_arrival",
        (60.0, 313800.0, 1.15, 50.0, 10000.0),
        "failed_damage",
        "remaining_distance_ratio",
        (-0.5, 300.0, 0.0, 50.0, -250.5),
    ),
    (
        "zero_time_success",
        (0.0, 0.0, 1.15, 50.0, 10000.0),
        "success",
        "time_ratio",
        (0.0, 0.0, 0.0, 50.0, 50.0),
    ),
    (
        "running_unscored",
        (120.0, 0.0, 1.15, 5000.0, 10000.0),
        "running",
        None,
        (None, None, None, None, None),
    ),
]


@pytest.fixture(scope="module")
def reference():
    return json.loads(files("wsorb").joinpath("scoring_reference.json").read_text(encoding="utf-8"))


def test_reference_has_a_closed_versioned_scope_and_no_nonfinite_values(reference):
    assert set(reference) == {
        "schema",
        "schema_version",
        "reference_version",
        "reviewed_on",
        "scope",
        "formula_id",
        "local_receipt_formula_id",
        "sources",
        "constants",
        "algebra",
        "terminal_conditions",
        "counter_semantics",
        "ranking",
        "known_conflicts",
        "local_comparison",
        "examples_kind",
        "worked_examples",
    }
    assert reference["schema"] == "wsorb.scoring_reference"
    assert type(reference["schema_version"]) is int and reference["schema_version"] == 1
    assert reference["reference_version"] == "2026-10-29.1"
    assert date.fromisoformat(reference["reviewed_on"]) == date(2026, 10, 29)
    assert reference["scope"] == (
        "published_per_run_algebra_not_runtime_or_leaderboard_certification"
    )
    assert reference["formula_id"] == "stu4_sim_scoring_c1e3fc8_v1"
    assert reference["local_receipt_formula_id"] == FORMULA
    assert reference["examples_kind"] == "worked_algebra_not_observed_runs"
    # The standard serializer rejects non-finite numbers anywhere in this data.
    json.dumps(reference, allow_nan=False)


def test_reference_constants_match_the_existing_versioned_scenario(reference):
    scenario = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())
    expected = {
        "nominal_time_s": 120.0,
        "nominal_fuel_kg": 1.15,
        "impact_threshold_j": 209200.0,
        "arrival_radius_m": 50.0,
        "score_offset": 0.5,
        "damage_multiplier": 2.0,
        "points_scale": 100.0,
        "zero_time_bonus": 0.0,
    }
    assert reference["constants"] == expected
    assert all(type(value) is float for value in reference["constants"].values())
    for name in (*REFERENCE_FIELDS, "arrival_radius_m", "score_offset"):
        assert reference["constants"][name] == scenario["mission"][name]
    fuel = reference["counter_semantics"]["fuel"]
    for name in ("specific_impulse_s", "standard_gravity_mps2"):
        assert fuel[name] == scenario["lander"][name]


def test_source_pins_and_content_hashes_identify_the_reviewed_first_party_files(reference):
    sources = reference["sources"]
    assert set(sources) == {"rules", "scoring_wiki", "simulation", "clarifications"}
    assert sources["rules"] == {
        "url": "https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218",
        "revision": None,
        "mutable": True,
        "authority_quote": "If there is conflicting info about the competition somewhere else, "
        "this page is the authoritative source.",
    }
    wiki = sources["scoring_wiki"]
    assert set(wiki) == {"url", "pinned_url", "wiki_commit_sha1", "revision_date"}
    assert wiki["url"] == (
        "https://github.com/SimDynamX/STU4-Solution/wiki/2.-Mission-Success,-Failure,-and-Scoring"
    )
    assert wiki["wiki_commit_sha1"] == "4ddae12f7d577b09c27dbfc9cab886ef98e693c5"
    assert wiki["pinned_url"] == f"{wiki['url']}/{wiki['wiki_commit_sha1']}"
    assert date.fromisoformat(wiki["revision_date"]) <= date.fromisoformat(reference["reviewed_on"])

    simulation = sources["simulation"]
    assert set(simulation) == {
        "repository_url",
        "pinned_commit",
        "observed_main_commit",
        "observed_main_commit_date",
        "content_hash_kind",
        "files",
    }
    assert simulation["repository_url"] == "https://github.com/SimDynamX/STU4-Sim"
    assert simulation["pinned_commit"] == "c1e3fc8f9ee3b189bba64d8bd87818a8003639cc"
    assert simulation["observed_main_commit"] == "3d7c342cd28817345bcd237279c44c95f9a990d1"
    assert simulation["observed_main_commit_date"] == "2026-10-26"
    assert simulation["content_hash_kind"] == "git_blob_sha1"
    expected_files = {
        "Systems/Scoring.py": ("388558e7fac38a1b7bfb73fd37c0a303f58fa0fb", 6862),
        "Systems/JetSelectionActuator.py": ("2d7f0f1ddad2e6c275e32af387196381dac9e6f5", 11815),
        "Sims/COMPETITION_SIM.simconf": ("4a228831ea71c21e9aad304914abbf95d11bf5ed", 822061),
    }
    assert len(simulation["files"]) == len(expected_files)
    assert {entry["path"] for entry in simulation["files"]} == set(expected_files)
    for entry in simulation["files"]:
        assert set(entry) == {
            "path",
            "pinned_url",
            "raw_url",
            "git_blob_sha1",
            "bytes",
            "identical_blob_at_observed_main",
        }
        assert (entry["git_blob_sha1"], entry["bytes"]) == expected_files[entry["path"]]
        assert type(entry["bytes"]) is int
        assert entry["identical_blob_at_observed_main"] is True
        suffix = f"{simulation['pinned_commit']}/{entry['path']}"
        assert entry["pinned_url"] == f"{simulation['repository_url']}/blob/{suffix}"
        assert entry["raw_url"] == f"https://raw.githubusercontent.com/SimDynamX/STU4-Sim/{suffix}"

    assert len(sources["clarifications"]) == 3
    expected_comments = {
        "230#discussioncomment-19180829": ("AlexSein98", "collaborator", "2026-10-25"),
        "232#discussioncomment-19226860": ("AlexSein98", "collaborator", "2026-10-27"),
        "227#discussioncomment-19116956": ("connorjak", "maintainer", "2026-10-22"),
    }
    prefix = "https://github.com/SimDynamX/SpaceTeamsPRO/discussions/"
    assert {entry["url"] for entry in sources["clarifications"]} == {
        f"{prefix}{suffix}" for suffix in expected_comments
    }
    for entry in sources["clarifications"]:
        assert set(entry) == {"topic", "url", "author", "author_role", "published_on", "mutable"}
        assert entry["url"].startswith(prefix)
        assert (entry["author"], entry["author_role"], entry["published_on"]) == expected_comments[
            entry["url"].removeprefix(prefix)
        ]
        assert entry["mutable"] is True


def test_documented_algebra_and_terminal_contract_are_explicit(reference):
    assert reference["algebra"] == {
        "time_bonus": "T_nom / T if T != 0 else 0",
        "damage_penalty": "2 * D / D_max",
        "fuel_penalty": "max(F / F_nom - 1, 0)",
        "success_score": "100 * (time_bonus - damage_penalty - fuel_penalty + 0.5)",
        "failure_score": "100 * (-r / r0 - damage_penalty - fuel_penalty + 0.5)",
        "clamped": False,
    }
    assert reference["terminal_conditions"] == {
        "priority": ["failed_damage", "success", "running"],
        "damage_failure": "D > D_max",
        "success": "r <= arrival_radius_m after damage check",
        "arrival_speed_gate_mps": None,
        "arrival_dwell_gate_s": None,
        "hard_timeout_s": None,
        "hard_timeout_status": "not_specified_in_reviewed_sources",
        "running_score": None,
    }


def test_counter_reference_does_not_certify_local_si_or_clock_parity(reference):
    counters = reference["counter_semantics"]
    assert set(counters) == {"fuel", "damage", "time"}
    assert counters["fuel"] == {
        "parameter_path": ["Stats", "FuelUsed"],
        "scorer_unit_label": "kg",
        "increment_per_iteration": "sum_i(norm(abs(onTimes[i]) * A[i, 0:3]) / (80 * 9.8))",
        "activity_vector_linear_components": "jet_force_divided_by_vehicle_mass",
        "timestep_multiplier_applied": False,
        "force_conversion_mass_multiplier_applied": False,
        "configured_loop_hz": 60.0,
        "specific_impulse_s": 80.0,
        "standard_gravity_mps2": 9.8,
        "local_si_fuel_equivalence_certified": False,
    }
    assert counters["damage"] == {
        "parameter_path": ["Stats", "Damage"],
        "scorer_unit_label": "J",
        "official_description": "collision energy extracted from STP physics engine hit outputs",
        "full_accumulator_source_in_reviewed_repositories": False,
        "local_damage_equivalence_certified": False,
    }
    assert counters["time"] == {
        "origin": "scoring_system_initialization",
        "assessment_sample": "preceding_iteration_elapsed_accumulator",
        "configured_loop_hz": 10.0,
        "scoring_loop_stops_physics_on_terminal": False,
        "local_clock_equivalence_certified": False,
    }


def test_two_run_submission_mean_is_not_a_local_seed_or_school_ranking(reference):
    ranking = reference["ranking"]
    assert set(ranking) == {
        "official_final_submission",
        "wiki_category_order",
        "category_order_enforced_by_numeric_formula",
        "mixed_outcome_category_aggregation",
        "tie_break",
        "school_aggregation",
        "prescribed_seed_list",
        "unfinished_run_score",
        "null_meaning",
    }
    assert ranking["official_final_submission"] == {
        "source": "rules",
        "run_count": 2,
        "aggregate": "arithmetic_mean_of_run_scores",
        "consecutive": True,
        "same_solution_code": True,
        "continuous_video": True,
        "top_claims_validated_by_organizers": True,
        "discard_scope": "software_startup_issues_between_runs_while_recording_continues",
        "worked_example": {"run_scores": [150.0, 50.0], "mean_score": 100.0},
    }
    assert ranking["wiki_category_order"] == ["arrival", "non_arrival"]
    assert ranking["category_order_enforced_by_numeric_formula"] is False
    for name in (
        "mixed_outcome_category_aggregation",
        "tie_break",
        "school_aggregation",
        "prescribed_seed_list",
        "unfinished_run_score",
    ):
        assert ranking[name] is None
    local = reference["local_comparison"]
    assert set(local) == {
        "official_leaderboard",
        "summaries",
        "cohort",
        "missing_terminal_score",
        "outcome_policy",
        "extra_efficiency_metrics_are_score_inputs",
    }
    assert local["official_leaderboard"] is False
    assert local["summaries"] == ["median_native_score", "mean_native_score", "success_count"]
    assert local["missing_terminal_score"] == "unavailable_not_zero"
    assert local["extra_efficiency_metrics_are_score_inputs"] is False
    assert {entry["id"] for entry in reference["known_conflicts"]} == {
        "fuel_penalty",
        "damage_boundary",
        "arrival_category",
        "completion_clock",
    }
    for entry in reference["known_conflicts"]:
        assert set(entry) == {"id", "wiki", "published_code", "resolved_by_rules"}
        assert entry["resolved_by_rules"] is False


@pytest.mark.parametrize(("example_id", "inputs", "status", "basis", "points"), EXAMPLES)
def test_worked_examples_match_explicit_fixtures_and_the_existing_receipt_contract(
    reference, example_id, inputs, status, basis, points
):
    examples = {entry["id"]: entry for entry in reference["worked_examples"]}
    assert len(examples) == len(reference["worked_examples"]) == len(EXAMPLES)
    assert set(examples) == {entry[0] for entry in EXAMPLES}
    example = examples[example_id]
    assert set(example) == {"id", "inputs", "expected"}
    assert example["inputs"] == dict(zip(INPUT_FIELDS, inputs, strict=True))
    assert all(
        type(value) is float and math.isfinite(value) for value in example["inputs"].values()
    )
    expected = example["expected"]
    assert set(expected) == {"status", "progress_basis", *POINT_FIELDS}
    assert expected == {
        "status": status,
        "progress_basis": basis,
        **dict(zip(POINT_FIELDS, points, strict=True)),
    }
    assert f"`{example_id}`" in (ROOT / "docs/scoring.md").read_text()
    if status == "running":
        assert native_score_breakdown(None, score=expected["total"]) is None
        return
    assert all(
        type(expected[name]) is float and math.isfinite(expected[name]) for name in POINT_FIELDS
    )
    # Check the already documented point receipt, not a reimplementation of how
    # time, damage, fuel or mission outcomes are calculated by the native engine.
    assert expected["total"] == pytest.approx(
        expected["progress_points"]
        - expected["collision_penalty_points"]
        - expected["fuel_penalty_points"]
        + expected["offset_points"]
    )
    receipt = {
        "formula": reference["local_receipt_formula_id"],
        "progress_basis": expected["progress_basis"],
        **{name: expected[name] for name in POINT_FIELDS},
        **{name: reference["constants"][name] for name in REFERENCE_FIELDS},
    }
    assert native_score_breakdown(receipt, score=expected["total"]) == receipt
