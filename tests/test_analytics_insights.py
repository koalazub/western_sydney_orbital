"""Source and pure derivation contracts, not DOM, browser, or publication proof."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "worker/web/analytics-insights.ts").read_text()


def section(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_public_types_are_structural_and_do_not_depend_on_the_overview():
    for declaration in (
        "export type InsightRun =",
        "label: string",
        "outcome?: string",
        "seed: number",
        "repetition: number | null",
        "time_s: number | null",
        "fuel_used_kg: number | null",
        "damage_j: number | null",
        "analytics: { totals: Record<string, number | string | boolean> } | null",
        "export type InsightProfile =",
        "attempts: number",
        "successes: number",
        "median_time_s?: unknown",
        "median_fuel_used_kg?: unknown",
        "median_damage_j?: unknown",
        "export type InsightGroup =",
        "owner: string",
        "label?: string",
        "first: InsightRun",
        "profile?: InsightProfile",
        "export type MetricInsight = { guidance: string; observation: string; scope: string }",
        "export function deriveMetricInsight(",
        'metric: "fuel" | "time" | "thrust" | "turn", groups: InsightGroup[]',
        "): MetricInsight",
    ):
        assert declaration in SOURCE
    assert "analytics-overview" not in SOURCE
    assert re.findall(r"export (?:type|function) (\w+)", SOURCE) == [
        "InsightRun",
        "InsightProfile",
        "InsightGroup",
        "MetricInsight",
        "deriveMetricInsight",
    ]


def test_derivation_has_no_data_control_dom_or_execution_capabilities():
    for forbidden in (
        "import ",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "document.",
        "window.",
        "HTMLElement",
        "localStorage",
        "sessionStorage",
        "console.",
        "api.control",
        "RoomEngine",
        "setTimeout(",
        "setInterval(",
        "requestAnimationFrame(",
        "Math.random(",
        "Date.",
        "performance.",
        "innerHTML",
        "eval(",
        "new Function(",
    ):
        assert forbidden not in SOURCE
    # Iteration reads the supplied groups; it cannot reorder or replace their runs.
    for mutation in (".sort(", ".splice(", ".push(", ".pop(", ".reverse("):
        assert mutation not in SOURCE
    assert re.search(r"\b(?:group|run|profile|first)\.\w+\s*=(?!=)", SOURCE) is None


def test_numeric_guards_preserve_zero_and_reject_missing_or_nonphysical_values():
    guard = section("function nonnegative(", "function integerCount(")
    assert 'typeof value === "number"' in guard
    assert "Number.isFinite(value)" in guard
    assert "value >= 0 ? value : null" in guard
    counts = section("function integerCount(", "function decimal(")
    assert "nonnegative(value)" in counts
    assert "number !== null && Number.isInteger(number)" in counts
    measured = section("function readings(", "function minimum(")
    assert "nonnegative(measure(group))" in measured
    assert "value === null ? [] : [{ group, value }]" in measured
    assert "nonnegative(run.analytics?.totals[key])" in SOURCE
    for invented_zero in ("?? 0", "|| 0", "filter(Boolean)", "Number(value)"):
        assert invented_zero not in SOURCE
    assert "if (winnerTime !== null && nextTime !== null)" in SOURCE
    assert "winnerFuel !== null" in SOURCE
    assert "turns === null" in SOURCE
    assert "angle === null" in SOURCE
    assert "angular === null" in SOURCE


def test_arrivals_require_an_explicit_success_outcome_not_short_duration_or_proximity():
    arrival = section("function arrived(", "function profileCounts(")
    assert 'run.outcome === "Station region reached" || run.outcome === "success"' in arrival
    assert arrival.count("===") == 2
    for inference in ("measurement", "analytics", "range_m", "relative_speed_mps"):
        assert inference not in arrival
    assert "groups.filter(group => arrived(group.first))" in SOURCE
    no_arrivals = section("function noArrivals(", "function quantity(")
    assert "No recorded arrivals in these selected runs" in no_arrivals
    assert "Reach the station before comparing efficiency" in no_arrivals
    assert "stopped, failed, and unknown-outcome runs stay unranked" in no_arrivals


def test_guidance_gives_a_useful_direction_even_without_observations():
    for direction in (
        "Compare local fuel use after arrival.",
        "Compare arrival time with fuel and damage.",
        "Compare engine-on time with fuel and damage after arrival.",
        "Compare physical turning with angular thrust. Avoidance can require turns.",
    ):
        assert direction in SOURCE
    derive = SOURCE.split("export function deriveMetricInsight(", 1)[1]
    assert "const guidance = GUIDANCE[metric]" in derive
    assert "if (!groups.length) return { guidance," in derive
    assert "No measured runs in this selection" in derive
    assert "Choose a seed or run a test to compare completed flights" in derive
    assert 'scope: "No selected runs."' in derive


def test_selected_run_scope_matches_both_seed_and_actual_repetition_before_comparing():
    scope = section("function selectedScope(", "function noArrivals(")
    for actual_scope in (
        "group.first.seed",
        "group.first.repetition",
        "integerCount(first.seed) !== null",
        "first.repetition === null || integerCount(first.repetition) !== null",
        "valid && seeds.size === 1 && repetitions.size === 1",
        "Mixed seeds · first selected runs are unranked",
        "Mixed repetitions · first selected runs are unranked",
        "Selected-run scope unavailable · unranked",
        "First selected runs · seed",
        "${completed.length}/${groups.length} recorded arrivals",
        "non-arrivals excluded",
    ):
        assert actual_scope in scope
    assert "profile" not in scope
    selected = section("function selectedObservation(", "function turnObservation(")
    assert selected.index("if (!comparable)") < selected.index("const winners = minimum(rows)")
    assert (
        "Different or unavailable seed/repetition scopes leave these readings unranked" in selected
    )
    assert "rows.slice(0, 2).map(row =>" in selected


def test_selected_fuel_and_thrust_use_actual_run_values_never_profile_medians():
    values = section("function selectedValue(", "function fuelContext(")
    assert "nonnegative(group.first.measurement.fuel_used_kg)" in values
    assert "nonnegative(group.first.measurement.time_s)" in values
    assert 'total(group.first, "burn_duration_s")' in values
    assert "profile" not in values
    selected = section("function selectedObservation(", "function turnObservation(")
    assert "if (!completed.length) return noArrivals()" in selected
    assert "readings(completed, group => selectedValue(metric, group))" in selected
    assert "completed flights with available data" in selected
    assert "the only completed flight" in selected
    assert "the only available ${metric} reading among completed flights" in selected
    assert "More completed-flight measurements are needed for a comparison" in selected
    assert "if (rows.length === 1)" in selected
    assert selected.index("if (rows.length === 1)") < selected.index(
        "const winners = minimum(rows)"
    )
    assert 'const tie = winners.length > 1 ? " each" : ""' in selected
    assert "if (!rows.length)" in selected
    assert "Record those measurements before comparing completed flights" in selected


def test_fuel_tradeoff_uses_measured_fuel_and_same_scope_arrival_time_difference():
    selected = section("function selectedObservation(", "function turnObservation(")
    assert "minimum(rows.filter(row => row.value > winners[0].value))[0]" in selected
    assert 'if (metric === "fuel" && next)' in selected
    assert "${name(next.group)} used ${decimal(next.value, 2)} kg" in selected
    context = section("function fuelContext(", "function thrustContext(")
    assert "winner.group.first.measurement.time_s" in context
    assert "next.group.first.measurement.time_s" in context
    assert "const difference = winnerTime - nextTime" in context
    assert "if (difference > 0)" in context
    assert "if (difference < 0)" in context
    assert "${decimal(difference)} s longer" in context
    assert "${decimal(-difference)} s sooner" in context
    assert "damageWarning(completed)" in context
    assert "Their recorded arrival times are equal" in context
    assert "profile" not in context


def test_thrust_is_engine_on_seconds_not_episode_count_or_fuel_efficiency():
    context = section("function thrustContext(", "function selectedObservation(")
    assert "readings(completed, group => group.first.measurement.fuel_used_kg)" in context
    assert "nonnegative(winners[0].group.first.measurement.fuel_used_kg)" in context
    assert "lowerFuel.value < winnerFuel" in context
    assert "used less estimated fuel" in context
    assert "${decimal(lowerFuel.value, 2)} kg" in context
    assert "${decimal(winnerFuel, 2)} kg" in context
    assert "engine-on time alone is not fuel efficiency" in context
    assert "Estimated fuel is unavailable for some completed flights" in context
    assert "damageWarning(completed)" in context
    assert "Engine-on time, not burn count, fuel use, or an official score" in SOURCE
    assert 'if (metric === "thrust") return `${decimal(value)} s of translation thrust`' in SOURCE


def test_time_cohorts_require_valid_positive_attempts_and_all_recorded_arrivals():
    counts = section("function profileCounts(", "function completedCohort(")
    assert "integerCount(profile.attempts)" in counts
    assert "integerCount(profile.successes)" in counts
    assert "attempts !== null && successes !== null && successes <= attempts" in counts
    complete = section("function completedCohort(", "function countText(")
    assert "profileCounts(profile)" in complete
    assert (
        "counts !== null && counts.attempts > 0 && counts.successes === counts.attempts" in complete
    )
    cohort = section("function cohortTime(", "function cohortValue(")
    assert "groups.filter(group => completedCohort(group.profile!))" in cohort
    assert "readings(completed, group => group.profile?.median_time_s)" in cohort
    assert "group.first" not in cohort
    assert "arrived(" not in cohort
    assert "only all-arrival cohort with an available median duration" in cohort
    assert "all-arrival cohorts with available duration data" in cohort
    assert "${countText(profileCounts(winners[0].group.profile!))}" in cohort
    assert (
        "Compare arrival times only for cohorts whose recorded attempts all reached the station"
        in cohort
    )
    assert "No all-arrival cohort has an available median duration" in cohort
    assert "More all-arrival cohorts with duration data are needed for a comparison" in cohort


def test_all_metrics_keep_full_cohort_and_recorded_run_scopes_separate():
    derive = SOURCE.split("export function deriveMetricInsight(", 1)[1]
    assert "groups.filter(group => group.profile !== undefined)" in derive
    assert "if (profiled.length)" in derive
    assert "cohortTime(profiled)" in derive
    assert "cohortTurn(profiled)" in derive
    assert "cohortFuelOrThrust(metric, profiled)" in derive
    assert "scope: cohortScope(profiled)" in derive
    assert "Unmatched selected-run scopes excluded" in derive
    assert derive.index("cohortTime(profiled)") < derive.index("selectedScope(groups)")
    assert "group.first" not in derive
    scope = section("function cohortScope(", "function cohortContext(")
    assert "Full-cohort medians, not the selected seed" in scope
    assert "countText(profileCounts(group.profile!))" in scope
    cohort = section("function cohortValue(", "export function deriveMetricInsight(")
    for median in (
        "median_fuel_used_kg",
        "median_time_s",
        "median_burn_duration_s",
        "median_turn_duration_s",
        "median_burn_count",
        "median_turn_count",
        "median_angular_burn_duration_s",
        "median_angular_burn_count",
    ):
        assert f"group.profile?.{median}" in cohort
    assert "group.first" not in cohort
    assert "arrived(" not in cohort
    assert "integerCount(" not in cohort  # Median counts can be fractional.
    assert "Missing cohort medians are not replaced with a selected run" in cohort
    assert "Duration and count are separate medians, not one recorded flight" in cohort
    assert "Local fuel counters are not verified as comparable with STP fuel mass" in derive
    assert "median(" not in SOURCE
    assert ".reduce(" not in SOURCE


def test_time_fuel_and_damage_context_use_only_matching_cohort_medians():
    context = section("function cohortContext(", "function cohortTime(")
    assert "Cohorts without all arrivals are excluded" in context
    assert "group.profile?.median_fuel_used_kg" in context
    assert "winners[0].group.profile?.median_fuel_used_kg" in context
    assert "damageWarning(completed, true)" in context
    assert "group.first" not in context
    assert "lower median fuel use" in context
    damage = section("function damageWarning(", "function selectedScope(")
    assert "cohort ? group.profile?.median_damage_j : group.first.measurement.damage_j" in damage
    assert "new Set(rows.map(row => row.value)).size > 1" in damage
    assert "rows.length < groups.length" in damage
    for unsupported_claim in (
        "all damage-free",
        "every flight was damage-free",
        "all flights had zero damage",
        "independent trials",
        "robustness proven",
    ):
        assert unsupported_claim not in SOURCE.lower()


def test_turning_uses_native_motion_angle_and_actuation_without_inventing_coast_time():
    turning = section("function turnObservation(", "function cohortScope(")
    for native_field in (
        'total(group.first, "turn_duration_s")',
        'total(entry.group.first, "angular_burn_duration_s")',
        'integerCount(total(row.group.first, "turn_count"))',
        'total(row.group.first, "total_turn_angle_rad")',
        'total(row.group.first, "angular_burn_duration_s")',
    ):
        assert native_field in turning
    assert "if (!completed.length) return noArrivals()" in turning
    assert "if (!rows.length)" in turning
    assert "Physical-turn totals are unavailable" in turning
    assert "entry.value > angular" in turning
    assert "row.value > angular" in turning
    assert "physical turning${count}${rotation}${actuation}" in turning
    assert "the totals do not locate those intervals" in turning
    assert "Physical turning measures motion; angular actuation measures thrust" in turning
    assert "their durations may overlap" in turning
    assert "unranked; physical turning can continue without angular thrust" in turning
    assert "the only completed flight" in turning
    assert "compare fuel and damage too" in turning
    for false_derivation in ("row.value - angular", "entry.value - angular", "minimum(", "penalty"):
        assert false_derivation not in turning
    assert "samples" not in turning


def test_formatting_uses_intl_with_fuel_angle_two_decimals_and_time_one_decimal():
    formatting = section("function decimal(", "function name(")
    assert "places = 1" in formatting
    assert 'new Intl.NumberFormat("en", {' in formatting
    assert "minimumFractionDigits: places, maximumFractionDigits: places" in formatting
    assert "${decimal(value, 2)} kg of estimated fuel" in SOURCE
    assert "${decimal(angle, 2)} rad" in SOURCE
    assert 'new Intl.PluralRules("en").select(turns)' in SOURCE
    assert 'new Intl.ListFormat("en", { type: "conjunction" })' in SOURCE
    assert "toFixed(" not in SOURCE


def test_no_score_algorithm_nominal_weights_or_hardcoded_observed_results():
    assert re.search(r"\.(?:score|mean_score|scored_pairs)\b", SOURCE) is None
    for forbidden in (
        "combineScoreMetrics",
        "fuel_penalty_points",
        "nominal_time",
        "nominal_fuel",
        "wikiFuel",
        "maxFuel",
        "weights",
        "STP points",
        "best controller",
        "winner overall",
        "Tom",
        "Bui",
        "Harry",
        "39.5",
        "78.0",
        "224.4",
        "222.7",
        "239.75",
        "83.2235",
        "84.99566",
        "237.1",
        "3.011",
    ):
        assert forbidden not in SOURCE
    # Numeric literals belong to validation, formatting, or readout limits only.
    without_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', SOURCE)
    assert not re.search(r"\b\d+\.\d+\b", without_strings)


def _derive(cases: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the pure TypeScript derivation contract")
    script = """
import { deriveMetricInsight } from './worker/web/analytics-insights.ts';
const cases = JSON.parse(process.argv[1]);
const before = JSON.stringify(cases);
const results = cases.map(({metric, groups}) => deriveMetricInsight(metric, groups));
if (JSON.stringify(cases) !== before) throw new Error('Derivation mutated its inputs');
console.log(JSON.stringify(results));
"""
    process = subprocess.run(
        [node, "--input-type=module", "-e", script, json.dumps(cases)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(process.stdout)


def _group(owner: str = "Pilot", **profile_overrides) -> dict:
    return {
        "owner": owner,
        "first": {
            "label": owner,
            "outcome": "success",
            "seed": 7,
            "repetition": 1,
            "measurement": {"time_s": 911.25, "fuel_used_kg": 922.75, "damage_j": 933.5},
            "analytics": {
                "totals": {
                    "burn_duration_s": 944.25,
                    "burn_count": 955,
                    "turn_duration_s": 966.75,
                    "turn_count": 977,
                    "angular_burn_duration_s": 988.5,
                    "angular_burn_count": 999,
                    "total_turn_angle_rad": 900.125,
                }
            },
        },
        "profile": {
            "attempts": 10,
            "successes": 10,
            "median_time_s": 51.25,
            "median_fuel_used_kg": 12.75,
            "median_damage_j": 0,
            "median_burn_duration_s": 21.25,
            "median_burn_count": 4.5,
            "median_turn_duration_s": 61.75,
            "median_turn_count": 5.5,
            "median_angular_burn_duration_s": 8.5,
            "median_angular_burn_count": 6.5,
            **profile_overrides,
        },
    }


def test_cpu_all_cohort_insights_ignore_contradictory_selected_run_values():
    metrics = ("fuel", "time", "thrust", "turn")
    group = _group()
    changed = _group()
    changed["first"] = {
        **changed["first"],
        "outcome": "timeout",
        "measurement": {"time_s": 1, "fuel_used_kg": 2, "damage_j": 3},
        "analytics": {"totals": {"burn_duration_s": 4, "turn_duration_s": 5}},
    }
    results = _derive(
        [{"metric": metric, "groups": [entry]} for entry in (group, changed) for metric in metrics]
    )
    assert results[:4] == results[4:]
    for result in results:
        assert "Pilot 10/10 arrivals" in result["scope"]
        for run_value in ("911.2", "922.75", "933.5", "944.2", "966.8", "988.5", "999"):
            assert run_value not in result["observation"]
    assert "12.75 kg" in results[0]["observation"]
    assert "51.3 s" in results[1]["observation"]
    assert "21.3 s" in results[2]["observation"]
    assert "4.5 median translation episodes" in results[2]["observation"]
    assert "61.8 s" in results[3]["observation"]
    assert "5.5 median physical turns" in results[3]["observation"]
    assert "8.5 s median angular thrust" in results[3]["observation"]
    assert "6.5 median angular episodes" in results[3]["observation"]


def test_cpu_missing_cohort_medians_never_substitute_selected_run_values():
    missing = _group(
        median_time_s=None,
        median_fuel_used_kg=None,
        median_burn_duration_s=None,
        median_turn_duration_s=None,
    )
    results = _derive(
        [{"metric": metric, "groups": [missing]} for metric in ("fuel", "time", "thrust", "turn")]
    )
    for result in results:
        assert (
            "unavailable" in result["observation"]
            or "No all-arrival cohort has an available" in result["observation"]
        )
        assert "911" not in result["observation"]
        assert "922" not in result["observation"]
        assert "944" not in result["observation"]
        assert "966" not in result["observation"]


def test_cpu_nonarrival_cohorts_never_win_even_with_lower_medians():
    complete = _group("Arrived")
    incomplete = _group(
        "Incomplete",
        successes=9,
        median_time_s=0,
        median_fuel_used_kg=0,
        median_burn_duration_s=0,
        median_turn_duration_s=0,
    )
    invalid = _group("Invalid", attempts=0, successes=0, median_time_s=0, median_fuel_used_kg=0)
    results = _derive(
        [
            {"metric": metric, "groups": [incomplete, invalid, complete]}
            for metric in ("fuel", "time", "thrust")
        ]
    )
    for result in results:
        assert result["observation"].startswith("Arrived is the only all-arrival cohort")
        assert "Incomplete 9/10 arrivals" in result["scope"]
        assert "Invalid 0/0 arrivals" in result["scope"]
        assert "lowest" not in result["observation"]
        assert "shortest" not in result["observation"]


def test_cpu_recorded_run_mode_keeps_two_repetitions_and_nonarrival_rules():
    first = _group()
    first.pop("profile")
    second = json.loads(json.dumps(first))
    second["first"]["repetition"] = 2
    second["first"]["measurement"].update(time_s=42.5, fuel_used_kg=14.25)
    second["first"]["analytics"]["totals"].update(burn_duration_s=19.75, turn_duration_s=23.5)
    stopped = json.loads(json.dumps(second))
    stopped["first"]["outcome"] = "stopped"
    results = _derive(
        [
            {"metric": metric, "groups": [group]}
            for group in (first, second, stopped)
            for metric in ("fuel", "time", "thrust", "turn")
        ]
    )
    for result in results[:4]:
        assert "rep 1" in result["scope"]
        assert "cohort" not in result["scope"].lower()
    for result in results[4:8]:
        assert "rep 2" in result["scope"]
    assert "922.75 kg" in results[0]["observation"]
    assert "14.25 kg" in results[4]["observation"]
    assert "42.5 s" in results[5]["observation"]
    assert "19.8 s" in results[6]["observation"]
    assert "23.5 s" in results[7]["observation"]
    for result in results[8:]:
        assert "No recorded arrivals" in result["observation"]
        assert "stay unranked" in result["observation"]


def test_cpu_cohort_zero_medians_are_measured_not_missing_and_mixed_scopes_are_excluded():
    zero = _group(
        median_time_s=0,
        median_fuel_used_kg=0,
        median_burn_duration_s=0,
        median_burn_count=0,
        median_turn_duration_s=0,
        median_turn_count=0,
        median_angular_burn_duration_s=0,
        median_angular_burn_count=0,
    )
    unmatched = _group("Unmatched")
    unmatched.pop("profile")
    results = _derive(
        [
            {"metric": metric, "groups": [zero, unmatched]}
            for metric in ("fuel", "time", "thrust", "turn")
        ]
    )
    for result in results:
        assert "Unmatched selected-run scopes excluded" in result["scope"]
        assert "Unmatched" not in result["observation"]
        assert "unavailable" not in result["observation"]
    assert "0.00 kg" in results[0]["observation"]
    assert "0.0 s" in results[1]["observation"]
    assert "0.0 median translation episodes" in results[2]["observation"]
    assert "0.0 median physical turns" in results[3]["observation"]
