"""Pure DTO/collector fixtures, not synthetic evidence of physical flights."""

import json
from copy import deepcopy

import pytest

from wsorb.flight_analytics import (
    COLUMNS,
    DEFINITION_ID,
    EPISODES,
    FlightAnalyticsRecorder,
    validate_native_analytics,
    validate_run_analytics,
)


def native(now=0):
    result = {
        "schema_version": 1,
        "definition_id": DEFINITION_ID,
        "observed_sim_s": now,
        "total_turn_angle_rad": now / 5,
        "peak_angular_speed_rad_s": 0.2 if now else 0,
        "propellant_impulse_ns": now * 10,
        "angular_impulse_nms": now * 2,
    }
    for name in EPISODES:
        result.update(
            {
                f"{name}_count": int(now > 0),
                f"{name}_duration_s": now,
                f"longest_{name}_s": now,
                f"current_{name}_s": now,
                f"{name}_active": now > 0,
            }
        )
    return result


def observation(now=0):
    return {
        "time_s": now,
        "seed": 100,
        "trial_revision": 3,
        "tuning_revision": 0,
        "manual_interventions": 0,
        "mission": {
            "flight_analytics": native(now),
            "fuel_used_kg": now / 10,
            "relative_speed_mps": 1,
            "target_distance_m": 1000 / (now + 1),
            "damage": 0,
        },
        "radar": "private irrelevant fixture sentinel",
    }


def report():
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation())
    recorder.observe(observation(1))
    return recorder.report


def test_exact_native_dto_is_detached_and_json_serializable():
    raw = native(2)
    parsed = validate_native_analytics(raw)
    assert len(parsed) == 22
    assert parsed == raw
    parsed["burn_count"] = 300
    assert raw["burn_count"] == 1
    assert json.loads(json.dumps(raw, allow_nan=False)) == raw
    assert validate_native_analytics(native()) is not None


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("definition_id", "https://private.invalid"),
        ("burn_count", True),
        ("burn_count", 1.0),
        ("turn_count", -1),
        ("turn_count", 10**1000),
        ("turn_count", 0),
        ("turn_duration_s", 5),
        ("longest_turn_s", 5),
        ("current_turn_s", 5),
        ("turn_active", 1),
        ("turn_active", False),
        ("propellant_impulse_ns", float("inf")),
        ("angular_impulse_nms", float("nan")),
        ("observed_sim_s", -0.1),
        ("peak_angular_speed_rad_s", 10**1000),
    ],
)
def test_invalid_native_fields_are_unavailable(field, value):
    raw = native(2)
    raw[field] = value
    assert validate_native_analytics(raw) is None


def test_extra_missing_or_legacy_native_data_never_becomes_zero():
    assert validate_native_analytics(None) is None
    assert validate_native_analytics({}) is None
    raw = native()
    raw["payload"] = "private"
    assert validate_native_analytics(raw) is None
    del raw["payload"], raw["burn_count"]
    assert validate_native_analytics(raw) is None
    raw = native()
    raw["propellant_impulse_ns"] = 1
    assert validate_native_analytics(raw) is None


def test_run_report_is_exact_allowlisted_detached_and_finite():
    raw = report()
    parsed = validate_run_analytics(raw)
    assert parsed == raw
    assert parsed["columns"] == list(COLUMNS)
    assert len(parsed["samples"][0]) == 9
    assert "private" not in json.dumps(parsed, allow_nan=False)
    parsed["samples"][0][1] = 1000
    parsed["totals"]["burn_count"] = 999
    assert raw["samples"][0][1] == 0
    assert raw["totals"]["burn_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(payload="private"),
        lambda r: r.update(schema_version=True),
        lambda r: r.update(source_kind="polls"),
        lambda r: r.update(definition_id="unknown"),
        lambda r: r.update(sample_count=True),
        lambda r: r.update(sample_count=7),
        lambda r: r.update(samples=[]),
        lambda r: r.update(columns=["other"]),
        lambda r: r["samples"][0].append(0),
        lambda r: r["samples"][0].__setitem__(0, None),
        lambda r: r["samples"][0].__setitem__(0, 0.5),
        lambda r: r["samples"][1].__setitem__(0, 0),
        lambda r: r["samples"][1].__setitem__(0, 2),
        lambda r: r["samples"][1].__setitem__(1, -1),
        lambda r: r["samples"][1].__setitem__(8, float("nan")),
        lambda r: r["samples"][1].__setitem__(2, 2),
        lambda r: r["samples"][1].__setitem__(2, 0.5),
        lambda r: r["samples"][0].__setitem__(1, 20),
    ],
)
def test_run_validator_rejects_invalid_shape_clock_intervals_and_counters(mutation):
    raw = report()
    mutation(raw)
    assert validate_run_analytics(raw) is None


def test_missing_optional_measurements_are_null_not_inferred():
    recorder = FlightAnalyticsRecorder()
    raw = observation()
    raw["mission"] = {"flight_analytics": native()}
    recorder.observe(raw)
    result = recorder.report
    assert result["samples"] == [[0, None, 0, 0, 0, 0, None, None, None]]
    assert validate_run_analytics(result) == result


def test_held_clock_does_not_make_extra_samples_or_episode_counts():
    recorder = FlightAnalyticsRecorder()
    assert recorder.report is None
    for _ in range(10):
        recorder.observe(observation())
    for _ in range(10):
        recorder.observe(observation(1))
    assert recorder.report["sample_count"] == 2
    assert recorder.report["totals"]["burn_count"] == 1
    assert recorder.report["totals"]["burn_duration_s"] == 1


def test_queued_commands_and_private_payloads_do_not_count_as_physics_intervals():
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation())
    held = observation()
    held["thrust_input"] = [1, 0, 0]
    held["angular_accel_input"] = [0, 0, 1]
    held["camera"] = "private payload"
    recorder.observe(held)
    assert recorder.report["sample_count"] == 1
    assert recorder.report["totals"]["burn_count"] == 0
    assert recorder.report["totals"]["angular_burn_active"] is False


def test_missing_public_readings_do_not_hide_a_later_cumulative_rewind():
    recorder = FlightAnalyticsRecorder(2)
    recorder.observe(observation())
    recorder.observe(observation(1))
    missing = observation(2)
    del missing["mission"]["fuel_used_kg"]
    recorder.observe(missing)
    rewound = observation(3)
    rewound["mission"]["fuel_used_kg"] = 0
    recorder.observe(rewound)
    assert recorder.report is None


def test_a_new_episode_requires_new_native_active_duration():
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation())
    recorder.observe(observation(1))
    bad = observation(2)
    bad["mission"]["flight_analytics"].update(
        burn_count=2, burn_duration_s=1, longest_burn_s=1, current_burn_s=0.5
    )
    assert validate_native_analytics(bad["mission"]["flight_analytics"]) is not None
    recorder.observe(bad)
    assert recorder.report is None


@pytest.mark.parametrize(
    "field", ["seed", "trial_revision", "tuning_revision", "manual_interventions"]
)
def test_source_changes_permanently_invalidate(field):
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation())
    changed = observation(1)
    changed[field] += 1
    recorder.observe(changed)
    assert recorder.report is None
    recorder.observe(observation())
    recorder.observe(observation(2))
    assert recorder.report is None


@pytest.mark.parametrize("kind", ["rewind", "held_mutation", "counter", "clock", "missing", "fuel"])
def test_invalid_history_never_silently_restarts(kind):
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation())
    recorder.observe(observation(1))
    bad = observation(2)
    if kind == "rewind":
        bad = observation(0.5)
    elif kind == "held_mutation":
        bad["time_s"] = 1
        bad["mission"]["flight_analytics"]["observed_sim_s"] = 1
    elif kind == "counter":
        bad["mission"]["flight_analytics"]["peak_angular_speed_rad_s"] = 0
    elif kind == "clock":
        bad["time_s"] = 3
    elif kind == "missing":
        del bad["mission"]["flight_analytics"]
    elif kind == "fuel":
        bad["mission"]["fuel_used_kg"] = 0
    recorder.observe(bad)
    assert recorder.report is None
    recorder.observe(observation(3))
    assert recorder.report is None


def test_missing_initial_native_history_is_not_reconstructed():
    recorder = FlightAnalyticsRecorder()
    recorder.observe(observation(1))
    assert recorder.report is None
    recorder = FlightAnalyticsRecorder()
    old = observation()
    del old["mission"]["flight_analytics"]
    recorder.observe(old)
    recorder.observe(observation(1))
    assert recorder.report is None


@pytest.mark.parametrize("maximum", [2, 3, 64, 192, 256])
def test_compaction_preserves_actual_endpoints_and_exact_native_totals(maximum):
    recorder = FlightAnalyticsRecorder(maximum)
    actual = {}
    for now in range(1000):
        raw = observation(now)
        actual[now] = deepcopy(raw)
        recorder.observe(raw)
    result = recorder.report
    assert 2 <= result["sample_count"] <= maximum
    assert result["sample_count"] == len(result["samples"])
    assert result["samples"][0][0] == 0
    assert result["samples"][-1][0] == 999
    assert result["totals"] == native(999)
    assert [row[0] for row in result["samples"]] == sorted({row[0] for row in result["samples"]})
    for row in result["samples"]:
        assert row[1] == actual[row[0]]["mission"]["fuel_used_kg"]
    result["samples"].clear()
    result["totals"]["burn_count"] = 777
    assert recorder.report["totals"]["burn_count"] == 1
    assert recorder.report["sample_count"] >= 2


@pytest.mark.parametrize("maximum", [True, 1, 257, 2.0, None])
def test_sample_limit_is_hard_bounded(maximum):
    with pytest.raises(ValueError, match="between 2 and 256"):
        FlightAnalyticsRecorder(maximum)


def test_validator_accepts_256_actual_rows_but_not_more():
    recorder = FlightAnalyticsRecorder(256)
    for now in range(256):
        recorder.observe(observation(now))
    raw = recorder.report
    assert validate_run_analytics(raw)["sample_count"] == 256
    raw["samples"].append(raw["samples"][-1])
    raw["sample_count"] += 1
    assert validate_run_analytics(raw) is None


def test_saved_sweep_summary_uses_native_receipts_not_sample_or_command_counts():
    from wsorb.sweep_analysis import analyze_run

    run = {
        "algorithm": "pure_fixture",
        "seed": 100,
        "debris_count": 500,
        "outcome": "timeout",
        "step_s": 1,
        "repetition": 2,
        "samples": [
            {"observation": observation(now), "accel_command": [1, 1, 1]} for now in (0, 1, 1, 2)
        ],
    }
    result = analyze_run(run)
    assert result["analytics_available"] is True
    assert result["analytics_definition_id"] == DEFINITION_ID
    assert result["burn_count"] == 1
    assert result["burn_duration_s"] == 2
    assert result["turn_count"] == 1
    assert result["repetition"] == 2
    assert result["final_score"] is None
    run["samples"][1]["observation"]["mission"]["flight_analytics"] = None
    legacy = analyze_run(run)
    assert legacy["analytics_available"] is False
    assert legacy["burn_count"] is None
    assert legacy["turn_duration_s"] is None
