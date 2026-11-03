"""Publish allowlisted teammate metrics from a saved sweep, without flying again.

Raw traces, exception strings and private source snapshots never leave the input
directory. Local pair means are not an official STP multi-seed leaderboard.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .controllers import catalog
from .flight_analytics import (
    COLUMNS,
    COUNTERS,
    DEFINITION_ID,
    FlightAnalyticsRecorder,
)
from .scoring import FORMULA, POINT_FIELDS, native_score_breakdown, score_compatibility

_PHYSICAL = {"success", "failed_damage", "timeout"}
_OUTCOMES = _PHYSICAL | {"policy_error", "engine_error", "interrupted"}
_EXECUTION = "owned_native_manual_step"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SOURCE_PATH = re.compile(
    r"(?:src/wsorb/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.py"
    r"|examples/(?:mvp|tom-mvp|bui/mvp|tom)/[A-Za-z0-9_]+\.py)\Z"
)
_DATA_PATHS = {
    "uv.lock",
    "simulation/scenarios/phobos_escape.json",
    "simulation/scenarios/flight_analytics.json",
    "src/wsorb/controllers/catalog.json",
}
_ENGINE_PATHS = {
    "module_sha256": "worker/public/sim/wsorb_bevy.js",
    "manifest_sha256": "worker/public/sim/manifest.json",
    "scenario_source_sha256": "simulation/scenarios/phobos_escape.json",
    "analytics_definition_sha256": "simulation/scenarios/flight_analytics.json",
    "driver_sha256": "worker/scripts/headless.mjs",
    "launcher_sha256": "worker/scripts/local.mjs",
    "bridge_sha256": "worker/web/local-api.js",
    "local_api_adapter_sha256": "src/wsorb/local_api.py",
}
_DEFINITION = {
    "schema_version": 1,
    "definition_id": DEFINITION_ID,
    "burn_force_threshold_n": 1e-6,
    "angular_torque_threshold_nm": 1e-6,
    "turn_speed_threshold_rad_s": 1e-3,
}
_ANALYTICS_METRICS = (
    *COUNTERS,
    "burn_duration_s",
    "angular_burn_duration_s",
    "turn_duration_s",
    "total_turn_angle_rad",
    "longest_burn_s",
    "longest_angular_burn_s",
    "longest_turn_s",
    "peak_angular_speed_rad_s",
    "propellant_impulse_ns",
    "angular_impulse_nms",
)
_MEASUREMENTS = ("time_s", "range_m", "relative_speed_mps", "damage_j", "fuel_used_kg")
_LIMITATIONS = [
    "These are local practice-model results, not an official STP leaderboard or SDK validation.",
    "Success means arrival-radius entry. Arrival speed and safe docking are separate checks.",
    "STP points are unavailable: physical SI fuel and local contact-energy estimates are "
    "not the official Stats/FuelUsed and Stats/Damage counters. No fuel conversion is assumed.",
    "Official Rules 2.18 require two consecutive unchanged-code runs in one continuous video. "
    "These saved headless attempts mirror pairing only, not the video, licensed SDK or an "
    "official submission. The multi-seed mean and ordering are local choices, not official "
    "tie-break or mixed-outcome precedence rules.",
    "Historical score and pair-mean fields preserve legacy local SI diagnostics only. "
    "Matching formula IDs and valid-pair seed coverage do not make them STP-comparable. "
    "Profiles are not ranked by those totals. Missing pairs are not assigned zero.",
    "Repeats at the same seed are not independent seed draws. Success counts are descriptive, "
    "not proof of robustness. No confidence interval is claimed.",
    "All attempted outcomes are retained. Timeouts and software errors have no terminal score. "
    "Timeout measurements are partial flight horizons, not successful completion times.",
    "Measurement medians include physical success, damage failure and timeout results only. "
    "Software-error partial measurements remain in trials but not physical medians.",
    "Native analytics medians use available receipts from that same physical cohort. "
    "Missing legacy counters are unavailable, never zero. Coverage counts are provided.",
    "Burns and turns are contiguous native physics-step episodes, not API polls, nozzle "
    "pulses or extra score terms. Direct angular control and its fuel model are unchanged.",
    "Series retain actual initial and final observations with bounded interior downsampling. "
    "Native totals remain exact; sampled series cannot locate every between-sample event.",
    "Fuel and damage are local estimates. Paired seeds share a build and settings, but this "
    "small seed set does not establish performance on other scenarios or engine versions.",
    "A failed attempt may have no engine receipt. Its engine hash stays unavailable. "
    "Source hashes identify saved evidence, not a complete external dependency lock.",
]
_UNRANKED = (
    "Unranked measured profiles. STP points are unavailable because local SI fuel and damage "
    "do not reproduce the official counters. Historical score fields are legacy diagnostics, "
    "not an official aggregate or a valid STP point comparison."
)


def _json(value: Any) -> str:
    return json.dumps(value, indent=2, allow_nan=False, ensure_ascii=True) + "\n"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any, *, signed: bool = False) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) and (signed or result >= 0) else None


def _median(values) -> float | None:
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    middle = len(present) // 2
    return present[middle] if len(present) % 2 else present[middle - 1] / 2 + present[middle] / 2


def _mean(values) -> float | None:
    present = list(values)
    return math.fsum(value / len(present) for value in present) if present else None


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("Missing or invalid saved source digest")
    return value


def _sources(evidence: Mapping) -> dict[str, str]:
    result = {}

    def add(path, digest):
        if not isinstance(path, str) or not (_SOURCE_PATH.fullmatch(path) or path in _DATA_PATHS):
            raise ValueError("Unreviewed source path in sweep provenance")
        digest = _digest(digest)
        if path in result and result[path] != digest:
            raise ValueError("Conflicting saved source digests")
        result[path] = digest

    packages = evidence.get("package_sources", {})
    if not isinstance(packages, Mapping):
        raise ValueError("Invalid private package-source evidence")
    for path, record in packages.items():
        if not isinstance(record, Mapping):
            raise ValueError("Invalid private package-source evidence")
        digest = record.get("sha256")
        source = record.get("source")
        if source is not None and (
            not isinstance(source, str) or hashlib.sha256(source.encode()).hexdigest() != digest
        ):
            raise ValueError("Private package snapshot does not match its saved digest")
        add(path, digest)
    for family in ("reviewed_example_hashes", "data_source_hashes"):
        hashes = evidence.get(family, {})
        if not isinstance(hashes, Mapping):
            raise ValueError("Invalid saved source hashes")
        for path, digest in hashes.items():
            add(path, digest)
    if evidence.get("uv_lock_sha256") is not None:
        add("uv.lock", evidence["uv_lock_sha256"])
    return dict(sorted(result.items()))


def _definition(evidence: Mapping) -> dict | None:
    value = evidence.get("analytics_definitions")
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value) != set(_DEFINITION)
        or type(value.get("schema_version")) is not int
        or value != _DEFINITION
        or any(type(value[key]) not in (int, float) for key in tuple(_DEFINITION)[2:])
    ):
        raise ValueError("Unsupported saved native analytics definitions")
    return deepcopy(_DEFINITION)


def _configuration(value: Any) -> dict:
    allowed = {
        "schema_version",
        "seeds",
        "debris_counts",
        "preset",
        "duration_s",
        "step_s",
        "settings",
        "algorithms",
        "repetitions",
    }
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or allowed - {"repetitions"} - set(value)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("preset") not in ("mars_facing_l1", "far_side_l2")
        or not isinstance(value.get("algorithms"), dict)
        or not value["algorithms"]
        or not isinstance(value.get("settings"), dict)
        or any(not isinstance(settings, dict) for settings in value["algorithms"].values())
        or _number(value.get("duration_s")) in (None, 0)
        or _number(value.get("step_s")) in (None, 0)
    ):
        raise ValueError("Invalid saved sweep configuration")
    for field in ("seeds", "debris_counts"):
        rows = value.get(field)
        if (
            not isinstance(rows, list)
            or not rows
            or any(type(row) is not int or not 0 <= row <= 0xFFFFFFFF for row in rows)
            or len(set(rows)) != len(rows)
        ):
            raise ValueError("Invalid saved cohort seeds or debris counts")
    if len(value["debris_counts"]) != 1:
        raise ValueError("Publish one matched debris-count cohort at a time")
    repetitions = value.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("Saved repetitions must be an integer between 1 and 10")
    value["repetitions"] = repetitions
    if any(not isinstance(value["settings"].get(key, {}), dict) for key in ("ship", "debris")):
        raise ValueError("Invalid saved common ship or debris settings")
    _canonical(value)
    return value


def _engine(value: Any) -> tuple[dict | None, float | None, dict, dict | None]:
    if value is None:
        return None, None, {}, None
    if not isinstance(value, Mapping):
        raise ValueError("Invalid saved engine receipt")
    build = value.get("engine_build")
    if (
        not isinstance(build, dict)
        or set(build) != {"bytes", "sha256"}
        or type(build.get("bytes")) is not int
        or build["bytes"] <= 0
        or value.get("execution_kind") != _EXECUTION
        or value.get("backend") != "native_headless"
        or value.get("control_mode") != "owned_native_API_manual_step"
    ):
        raise ValueError("Saved engine receipt is not the owned native manual-step backend")
    digest = _digest(build["sha256"])
    if value.get("wasm_sha256") != digest:
        raise ValueError("Saved WASM and engine-build digests disagree")
    scenario = value.get("scenario_source", {})
    integration = scenario.get("integration") if isinstance(scenario, Mapping) else None
    fixed = _number(integration.get("fixed_step_s")) if isinstance(integration, Mapping) else None
    if fixed in (None, 0):
        raise ValueError("Missing saved native physics-step schedule")
    hashes = {
        path: _digest(value[field]) for field, path in _ENGINE_PATHS.items() if field in value
    }
    definition = _definition({"analytics_definitions": value.get("analytics_definition")})
    return deepcopy(build), fixed, hashes, definition


def _observations(run: Mapping, *, fixed: float | None) -> list[dict]:
    observations = []
    context = None
    context_available = None
    previous = None
    previous_status = None
    for sample in run["samples"]:
        observation = sample.get("observation") if isinstance(sample, Mapping) else None
        if not isinstance(observation, dict):
            raise ValueError("Saved trace is missing an actual public observation")
        now = _number(observation.get("time_s"))
        if (
            now is None
            or (previous is None and now != 0)
            or (previous is not None and now <= previous)
        ):
            raise ValueError(
                "Saved trial clock rewound, repeated or omitted its initial observation"
            )
        tolerance = 1e-6 * max(1, now)
        if now > run["duration_s"] + tolerance:
            raise ValueError("Saved trial exceeded its configured horizon")
        if previous is not None and now - previous > run["step_s"] + tolerance:
            raise ValueError("Saved observations do not match the configured controller cadence")
        if previous_status in ("success", "failed_damage"):
            raise ValueError("Saved trace advanced after its native terminal result")
        mission = observation.get("mission", {})
        if (
            previous is not None
            and isinstance(mission, Mapping)
            and mission.get("status") == "running"
            and not math.isclose(now - previous, run["step_s"], abs_tol=tolerance)
        ):
            raise ValueError("Saved observations use a different controller cadence")
        if fixed is not None and not math.isclose(now / fixed, round(now / fixed), abs_tol=1e-5):
            raise ValueError("Saved observations do not match the native physics-step schedule")
        keys = ("seed", "trial_revision", "tuning_revision", "manual_interventions")
        actual = tuple(observation.get(key) for key in keys)
        if any(item is not None for item in actual):
            if (
                context_available is False
                or any(type(item) is not int or not 0 <= item <= 0xFFFFFFFF for item in actual)
                or actual[0] != run["seed"]
                or actual[3] != 0
                or (context is not None and actual != context)
            ):
                raise ValueError("Saved trial changed its actual public source context")
            context = actual
            context_available = True
        elif context_available is True:
            raise ValueError("Saved trial lost its actual public source context")
        else:
            context_available = False
        observations.append(observation)
        previous = now
        previous_status = mission.get("status") if isinstance(mission, Mapping) else None
    return observations


def _trial(path: Path, config: dict, metadata: dict) -> tuple:
    from .sweep import load_trial

    try:
        run = load_trial(path)
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError("Invalid saved raw trial structure") from error
    profile_id = run.get("algorithm")
    if (
        not isinstance(profile_id, str)
        or profile_id not in metadata
        or profile_id not in config["algorithms"]
    ):
        raise ValueError("Saved trial has an unreviewed or unexpected profile")
    count = config["debris_counts"][0]
    expected = {
        "schema_version": 1,
        "execution_kind": _EXECUTION,
        "preset": config["preset"],
        "debris_count": count,
        "duration_s": config["duration_s"],
        "step_s": config["step_s"],
        "algorithm_settings": config["algorithms"][profile_id],
        "settings": deepcopy(config["settings"]),
    }
    expected["settings"].setdefault("debris", {})["count"] = count
    repetition = run.get("repetition", 1)
    if (
        any(_canonical(run.get(key)) != _canonical(value) for key, value in expected.items())
        or type(run.get("seed")) is not int
        or run["seed"] not in config["seeds"]
        or not isinstance(run.get("outcome"), str)
        or run["outcome"] not in _OUTCOMES
        or type(repetition) is not int
        or not 1 <= repetition <= config["repetitions"]
    ):
        raise ValueError("Saved trials mix configuration, cadence, execution kind or outcomes")
    build, fixed, hashes, definition = _engine(run.get("engine"))
    if build is None and run["outcome"] in _PHYSICAL:
        raise ValueError("A physical result has no saved engine receipt")
    observations = _observations(run, fixed=fixed)
    mission = observations[-1].get("mission", {}) if observations else {}
    if not isinstance(mission, Mapping):
        raise ValueError("Invalid saved mission measurement")
    expected_status = {"success": "success", "failed_damage": "failed_damage", "timeout": "running"}
    if run["outcome"] in _PHYSICAL and mission.get("status") != expected_status[run["outcome"]]:
        raise ValueError("Saved outcome disagrees with the native mission status")
    if run["outcome"] == "timeout" and (
        not observations
        or not math.isclose(observations[-1]["time_s"], run["duration_s"], abs_tol=1e-5)
    ):
        raise ValueError("A timeout must retain its actual configured horizon")
    raw_terms = mission.get("score_breakdown")
    if isinstance(raw_terms, Mapping) and raw_terms.get("formula") not in (None, FORMULA):
        raise ValueError("Saved trials contain an unsupported or mixed native score formula")
    terminal = run["outcome"] in ("success", "failed_damage")
    score = _number(mission.get("score"), signed=True) if terminal else None
    terms = native_score_breakdown(raw_terms, score=score)
    recorder = FlightAnalyticsRecorder(192)
    for observation in observations:
        recorder.observe(observation)
    entry = metadata[profile_id]
    trial = {
        "id": path.name,
        "profile_id": profile_id,
        "owner": entry["owner"],
        "label": entry["label"],
        "seed": run["seed"],
        "repetition": repetition,
        "outcome": run["outcome"],
        "score": score,
        "score_breakdown": terms,
        "measurement": {
            "time_s": _number(observations[-1]["time_s"]) if observations else None,
            "range_m": _number(mission.get("target_distance_m")),
            "relative_speed_mps": _number(mission.get("relative_speed_mps")),
            "damage_j": _number(mission.get("damage")),
            "fuel_used_kg": _number(mission.get("fuel_used_kg")),
        },
        "analytics": recorder.report if build is not None else None,
        "engine_sha256": build["sha256"] if build is not None else None,
    }
    return trial, build, fixed, hashes, definition


def _aggregate(trials: list[dict], profile: dict, config: dict) -> dict:
    rows = [row for row in trials if row["profile_id"] == profile["id"]]
    physical = [row for row in rows if row["outcome"] in _PHYSICAL]
    analytics = [row["analytics"]["totals"] for row in physical if row["analytics"] is not None]
    scored = [row for row in rows if row["score"] is not None]
    pairs = []
    if config["repetitions"] == 2:
        for seed in config["seeds"]:
            pair = sorted(
                (row for row in rows if row["seed"] == seed),
                key=lambda row: row["repetition"],
            )
            valid = len(pair) == 2 and all(row["score"] is not None for row in pair)
            pairs.append(
                {
                    "seed": seed,
                    "run_ids": [row["id"] for row in pair],
                    "mean_score": _mean(row["score"] for row in pair) if valid else None,
                    "outcomes": [row["outcome"] for row in pair],
                }
            )
    valid_pairs = [row["mean_score"] for row in pairs if row["mean_score"] is not None]
    result = {
        "id": profile["id"],
        "owner": profile["owner"],
        "label": profile["label"],
        "attempts": len(rows),
        "successes": sum(row["outcome"] == "success" for row in rows),
        "scored_runs": len(scored),
        "unscored_runs": len(rows) - len(scored),
        "physical_runs": len(physical),
        "analytics_runs": len(analytics),
        "mean_score": _mean(valid_pairs),
        "scored_pairs": len(valid_pairs),
        "paired_scores": pairs,
        "outcomes": dict(sorted(Counter(row["outcome"] for row in rows).items())),
        "median_score": _median(row["score"] for row in scored),
        **{
            f"median_{key}": _median(row["measurement"][key] for row in physical)
            for key in _MEASUREMENTS
        },
        **{f"median_{key}": _median(row[key] for row in analytics) for key in _ANALYTICS_METRICS},
        "metric_runs": {
            key: sum(row["measurement"][key] is not None for row in physical)
            for key in _MEASUREMENTS
        },
    }
    return result


def build_bundle(directory: str | Path) -> dict:
    """Read one complete paired cohort. No simulation, writes or source imports."""
    directory = Path(directory)
    config = _configuration(json.loads((directory / "configuration.json").read_text()))
    evidence = json.loads((directory / "python.json").read_text())
    if not isinstance(evidence, Mapping):
        raise ValueError("Missing saved sweep provenance")
    metadata = {entry["id"]: entry for entry in catalog()}
    if set(config["algorithms"]) - set(metadata):
        raise ValueError("The publisher accepts reviewed profile IDs only")
    source_hashes = _sources(evidence)
    definitions = _definition(evidence)
    trials, builds, physics_steps = [], [], []
    for path in sorted(directory.glob("trial-*")):
        if not path.is_dir() or not re.fullmatch(r"trial-\d{5}", path.name):
            raise ValueError("Unexpected saved trial path")
        trial, build, fixed, hashes, definition = _trial(path, config, metadata)
        trials.append(trial)
        if build is not None:
            builds.append(build)
            physics_steps.append(fixed)
        if definition is not None:
            if definitions is not None and definition != definitions:
                raise ValueError("Saved trials mix native analytics definitions")
            # The actual engine receipt takes precedence over present-day files.
            definitions = definition
        for name, digest in hashes.items():
            if name in source_hashes and source_hashes[name] != digest:
                raise ValueError("Saved trials mix source revisions")
            source_hashes[name] = digest
    expected = [
        (profile, seed, repetition)
        for seed in config["seeds"]
        for profile in config["algorithms"]
        for repetition in range(1, config["repetitions"] + 1)
    ]
    actual = [(row["profile_id"], row["seed"], row["repetition"]) for row in trials]
    if actual != expected:
        raise ValueError(
            "Saved comparison must retain every paired attempt in consecutive repetition order"
        )
    if not builds or any(build != builds[0] for build in builds):
        raise ValueError("Saved comparison has no verified build or mixes engine builds")
    if any(fixed != physics_steps[0] for fixed in physics_steps):
        raise ValueError("Saved comparison mixes physics-step schedules")
    fixed = physics_steps[0]
    for value in (config["duration_s"], config["step_s"]):
        if not math.isclose(value / fixed, round(value / fixed), abs_tol=1e-6):
            raise ValueError("Controller cadence and duration must match the native physics step")
    if any(row["analytics"] is not None for row in trials) and definitions is None:
        raise ValueError("Native analytics require their saved versioned definitions")
    if len(source_hashes) > 128:
        raise ValueError("Public provenance is limited to 128 source hashes; none were dropped")
    scored = [row for row in trials if row["score"] is not None]
    formula = (
        FORMULA if scored and all(row["score_breakdown"] is not None for row in scored) else None
    )
    profiles = [_aggregate(trials, metadata[name], config) for name in sorted(config["algorithms"])]
    return {
        "schema": "wsorb.teammate_analytics",
        "schema_version": 2,
        "comparison": {
            "preset": config["preset"],
            "seeds": list(config["seeds"]),
            "repetitions": config["repetitions"],
            "debris_count": config["debris_counts"][0],
            "duration_limit_s": config["duration_s"],
            "controller_step_s": config["step_s"],
            "physics_step_s": fixed,
            "execution_kind": _EXECUTION,
            "score_formula_id": formula,
            "score_compatibility": score_compatibility(),
            "engine_build": builds[0],
            "definitions": definitions,
            "source_hashes": dict(sorted(source_hashes.items())),
            "ranking_method": _UNRANKED,
            "limitations": [
                *_LIMITATIONS,
                f"{len(config['seeds'])} matched seeds with {config['repetitions']} "
                "consecutive repeats are paired seed cases, not "
                f"{len(config['seeds']) * config['repetitions']} independent draws.",
            ],
        },
        "profiles": profiles,
        "trials": trials,
    }


_COLORS = ("#075985", "#9d174d", "#166534", "#92400e", "#5b21b6", "#155e75", "#374151")
_DASHES = ("", "8 3", "3 3", "10 3 2 3", "2 4")


def _format(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:,.4g}"


def _draw_svg(title: str, description: str, panels: list[dict], profiles: list[dict]) -> str:
    """Small static chart writer. Every position comes from a recorded value.

    Colors have dark strokes on white; category labels and per-path titles also
    identify profiles. Missing cells break traces rather than turning into zero.
    """
    legend_height = math.ceil(len(profiles) / 2) * 23
    start = 93 + legend_height
    height = start + math.ceil(len(panels) / 2) * 270 + 18
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1100 {height}" '
        'role="img" aria-labelledby="title" aria-describedby="description">',
        f'<title id="title">{html.escape(title)}</title>',
        f'<desc id="description">{html.escape(description)} '
        "Values are available in dataset.json and the CSV downloads. Thin traces are actual "
        "paired-seed trials, not a median trajectory. Missing values are not drawn.</desc>",
        '<rect width="1100" height="100%" fill="white"/>',
        "<style>text{font-family:system-ui,sans-serif;fill:#172033;font-size:16px}"
        ".heading{font-size:24px;font-weight:650}.panel{font-size:16px;font-weight:650}</style>",
        f'<text x="24" y="35" class="heading">{html.escape(title)}</text>',
        f'<text x="24" y="59">{html.escape(description)}</text>',
    ]

    def text(x, y, value, *, anchor="start", css=""):
        parts.append(
            f'<text x="{x:.2f}" y="{y:.2f}" text-anchor="{anchor}" class="{css}">'
            f"{html.escape(str(value))}</text>"
        )

    for index, profile in enumerate(profiles):
        x, y = 25 + index % 2 * 540, 86 + index // 2 * 23
        color = _COLORS[index % len(_COLORS)]
        parts.append(f'<rect x="{x}" y="{y - 10}" width="13" height="13" fill="{color}"/>')
        text(x + 21, y + 1, f"{index + 1}. {profile['owner']} / {profile['label']}")
    for index, panel in enumerate(panels):
        bx, by = 16 + index % 2 * 543, start + index // 2 * 270
        # Negative scientific-notation tick labels need room to the axis's left.
        left, right, top, bottom = bx + 112, bx + 510, by + 63, by + 200
        text(bx + 9, by + 16, panel["title"], css="panel")
        values = panel.get("values")
        series = panel.get("series", [])
        numbers = (
            [value for value in values if value is not None]
            if values is not None
            else [value for item in series for _, value in item["points"] if value is not None]
        )
        if not numbers:
            text(
                (left + right) / 2,
                (top + bottom) / 2,
                "Unavailable, no recorded values",
                anchor="middle",
            )
            continue
        scale = max(abs(value) for value in numbers) or 1
        low, high = min(0, min(numbers) / scale), max(0, max(numbers) / scale)
        if low == high:
            high = low + 1

        def py(value, *, bottom=bottom, top=top, low=low, high=high, scale=scale):
            return bottom - (value / scale - low) / (high - low) * (bottom - top)

        text(left, by + 37, panel["unit"])
        for tick in range(4):
            value = low + (high - low) * tick / 3
            y = py(value * scale)
            parts.append(f'<path d="M {left} {y:.2f} H {right}" stroke="#d8dee7"/>')
            text(left - 8, y + 4, _format(value * scale), anchor="end")
        if values is not None:
            width = (right - left) / max(1, len(profiles))
            for category, (profile, value) in enumerate(zip(profiles, values, strict=True)):
                x = left + width * (category + 0.5)
                text(x, bottom + 44, f"{category + 1}. {profile['owner']}", anchor="middle")
                if value is None:
                    text(x, (top + bottom) / 2, "unavailable", anchor="middle")
                    continue
                y, zero = py(value), py(0)
                color = _COLORS[category % len(_COLORS)]
                label = f"{profile['owner']} / {profile['label']}: {_format(value)} {panel['unit']}"
                parts.append(
                    f'<rect x="{x - width * 0.26:.2f}" y="{min(y, zero):.2f}" '
                    f'width="{width * 0.52:.2f}" height="{max(1, abs(y - zero)):.2f}" '
                    f'fill="{color}"><title>{html.escape(label)}</title></rect>'
                )
                annotation = (
                    panel.get("annotations", [])[category]
                    if panel.get("annotations")
                    else _format(value)
                )
                text(x, y - 5 if value >= 0 else y + 18, annotation, anchor="middle")
        else:
            end = max((time for item in series for time, _ in item["points"]), default=0) or 1
            for tick in range(5):
                time = end * tick / 4
                text(left + (right - left) * tick / 4, bottom + 21, _format(time), anchor="middle")
            text((left + right) / 2, bottom + 43, "Simulation time (s)", anchor="middle")
            for item in series:
                segments, current = [], []
                for time, value in item["points"]:
                    if value is None:
                        if current:
                            segments.append(current)
                        current = []
                    else:
                        current.append((left + time / end * (right - left), py(value)))
                if current:
                    segments.append(current)
                color = _COLORS[item["profile"] % len(_COLORS)]
                dash = _DASHES[item["seed"] % len(_DASHES)]
                for points in segments:
                    commands = " ".join(
                        f"{'M' if i == 0 else 'L'} {x:.2f} {y:.2f}"
                        for i, (x, y) in enumerate(points)
                    )
                    parts.append(
                        f'<path d="{commands}" fill="none" stroke="{color}" '
                        f'stroke-width="1.7" stroke-dasharray="{dash}">'
                        f"<title>{html.escape(item['label'])}</title></path>"
                    )
                    for x, y in (points[0], points[-1]) if len(points) > 1 else points:
                        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.2" fill="{color}"/>')
    return "\n".join([*parts, "</svg>", ""])


def _graphs(bundle: dict) -> dict[str, str]:
    profiles, trials = bundle["profiles"], bundle["trials"]

    def bars(title, unit, metric):
        return {"title": title, "unit": unit, "values": [row[metric] for row in profiles]}

    def trace(title, unit, column):
        series = []
        for row in trials:
            report = row["analytics"]
            if report is None:
                continue
            series.append(
                {
                    "profile": next(
                        i for i, p in enumerate(profiles) if p["id"] == row["profile_id"]
                    ),
                    "seed": bundle["comparison"]["seeds"].index(row["seed"]) * 2
                    + row["repetition"]
                    - 1,
                    "label": f"{row['owner']} / {row['label']}, seed {row['seed']}, "
                    f"run {row['repetition']}, {row['outcome']}",
                    "points": [(sample[0], sample[column]) for sample in report["samples"]],
                }
            )
        return {"title": title, "unit": unit, "series": series}

    components = [
        bars("Legacy SI pair mean, not STP", "legacy diagnostic units", "mean_score"),
        bars("Legacy SI median, not STP", "legacy diagnostic units", "median_score"),
    ]
    for field, title in (
        ("progress_points", "Native progress median"),
        ("collision_penalty_points", "Collision penalty median, subtracted"),
        ("fuel_penalty_points", "Fuel penalty median, subtracted"),
        ("offset_points", "Native offset median"),
    ):
        components.append(
            {
                "title": title,
                "unit": "legacy diagnostic units, not STP points",
                "values": [
                    _median(
                        row["score_breakdown"][field]
                        for row in trials
                        if row["profile_id"] == profile["id"] and row["score_breakdown"] is not None
                    )
                    for profile in profiles
                ],
            }
        )
    completion = {
        "title": "Arrival-radius entries / all attempts",
        "unit": "fraction of attempts",
        "values": [row["successes"] / row["attempts"] for row in profiles],
        "annotations": [f"{row['successes']}/{row['attempts']}" for row in profiles],
    }
    charts = {
        "score-components.svg": (
            "Historical SI diagnostics, not STP points",
            "Invalid for STP comparison: local physical fuel is not the official fuel counter. "
            "Preserved receipts only.",
            components,
        ),
        "fuel-burns.svg": (
            "Fuel and native propellant activity",
            "All saved trial traces. Bars use physical medians. Errors remain in the downloads.",
            [
                trace("Cumulative fuel used", "kg, local estimate", 1),
                trace("Cumulative propellant-on time", "native active duration, s", 2),
                bars("Native propellant episodes", "median episode count", "median_burn_count"),
                bars(
                    "Longest propellant episode",
                    "median longest duration, s",
                    "median_longest_burn_s",
                ),
            ],
        ),
        "turns.svg": (
            "Physical rotation and angular actuation",
            "Orientation arcs, physical turns and actuator burns are different measurements.",
            [
                trace("Cumulative physical rotation", "orientation arc, rad", 5),
                trace("Cumulative physical turn time", "native active duration, s", 4),
                trace("Cumulative angular-actuator time", "native active duration, s", 3),
                bars("Physical turn episodes", "median episode count", "median_turn_count"),
                bars(
                    "Angular-actuator episodes", "median episode count", "median_angular_burn_count"
                ),
                bars("Peak angular speed", "median peak, rad/s", "median_peak_angular_speed_rad_s"),
            ],
        ),
        "efficiency.svg": (
            "Completion, duration and resource comparison",
            "No weighted efficiency score. Timeout durations are partial flight horizons.",
            [
                completion,
                bars("Sampled flight duration", "physical-result median, s", "median_time_s"),
                bars("Fuel used", "physical-result median, kg", "median_fuel_used_kg"),
                bars("Propellant episodes", "physical-result median count", "median_burn_count"),
                bars("Physical turn episodes", "physical-result median count", "median_turn_count"),
            ],
        ),
    }
    return {
        name: _draw_svg(title, description, panels, profiles)
        for name, (title, description, panels) in charts.items()
    }


def _csv(rows: list[dict], fields: Sequence[str] | None = None) -> str:
    stream = io.StringIO(newline="")
    fields = (
        list(fields)
        if fields is not None
        else list(dict.fromkeys(key for row in rows for key in row))
    )
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(
        {
            key: _canonical(value) if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        }
        for row in rows
    )
    return stream.getvalue()


def publish_scoreboard(directory: str | Path, output: str | Path) -> dict:
    """Create a new public bundle. Existing output and raw archives are untouched."""
    if Path(output).is_symlink():
        raise FileExistsError("Scoreboard output must be a new directory, not a symlink")
    directory, output = Path(directory).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Scoreboard output must be a new directory")
    if output.is_relative_to(directory):
        raise ValueError("Public output must be outside the private raw sweep directory")
    bundle = build_bundle(directory)
    trials, samples = [], []
    for trial in bundle["trials"]:
        totals = trial["analytics"]["totals"] if trial["analytics"] is not None else {}
        terms = trial["score_breakdown"] or {}
        trials.append(
            {
                **{
                    key: trial[key]
                    for key in (
                        "id",
                        "profile_id",
                        "owner",
                        "label",
                        "seed",
                        "repetition",
                        "outcome",
                        "score",
                        "engine_sha256",
                    )
                },
                **trial["measurement"],
                **{key: totals.get(key) for key in _ANALYTICS_METRICS},
                **{f"score_{key}": terms.get(key) for key in POINT_FIELDS},
            }
        )
        if trial["analytics"] is not None:
            for row in trial["analytics"]["samples"]:
                samples.append(
                    {
                        "trial_id": trial["id"],
                        "profile_id": trial["profile_id"],
                        "seed": trial["seed"],
                        "repetition": trial["repetition"],
                        **dict(zip(COLUMNS, row, strict=True)),
                    }
                )
    files = {
        "dataset.json": _json(bundle),
        "trials.csv": _csv(trials),
        "aggregate.csv": _csv(bundle["profiles"]),
        "samples.csv": _csv(samples, ["trial_id", "profile_id", "seed", "repetition", *COLUMNS]),
        **_graphs(bundle),
    }
    encoded = {name: value.encode("utf-8") for name, value in files.items()}
    manifest = {
        "schema": "wsorb.teammate_analytics.artifacts",
        "schema_version": 1,
        "provenance_revision": hashlib.sha256(encoded["dataset.json"]).hexdigest(),
        "artifacts": [
            {"path": name, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
            for name, value in sorted(encoded.items())
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    for name, value in encoded.items():
        (output / name).write_bytes(value)
    (output / "manifest.json").write_text(_json(manifest), encoding="utf-8")
    return bundle


def refresh_scoreboard(directory: str | Path, output: str | Path) -> dict:
    """Replace only digest-verified generated artifacts. Keep raw evidence and other files."""
    output = Path(output)
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Refresh requires an existing generated publication directory")
    manifest_path = output / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Publication manifest must not be a symlink")
    manifest = json.loads(manifest_path.read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "wsorb.teammate_analytics.artifacts"
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise ValueError("Refresh requires a generated artifact manifest")
    expected = {
        "dataset.json",
        "trials.csv",
        "aggregate.csv",
        "samples.csv",
        "score-components.svg",
        "fuel-burns.svg",
        "turns.svg",
        "efficiency.svg",
    }
    artifacts = manifest["artifacts"]
    if (
        len(artifacts) != len(expected)
        or {row.get("path") for row in artifacts if isinstance(row, dict)} != expected
    ):
        raise ValueError("Refresh accepts only the known generated artifact set")
    for row in artifacts:
        path = output / row["path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Generated artifact is missing or a symlink")
        data = path.read_bytes()
        if len(data) != row.get("bytes") or hashlib.sha256(data).hexdigest() != row.get("sha256"):
            raise ValueError("Generated artifact changed; refusing to overwrite possible user work")
    with TemporaryDirectory(prefix="wsorb-scoreboard-", dir=output.parent) as temporary:
        staging = Path(temporary) / "publication"
        bundle = publish_scoreboard(directory, staging)
        for name in sorted(expected | {"manifest.json"}):
            (staging / name).replace(output / name)
    return bundle


def render_comparison_section(
    bundle: dict, *, asset_prefix: str = "analytics/team-comparison"
) -> str:
    """Render the flight guide's measured comparison and links to the full records."""
    if (
        not isinstance(asset_prefix, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*", asset_prefix)
        or any(part in (".", "..") for part in asset_prefix.split("/"))
    ):
        raise ValueError("asset_prefix must be a local relative asset path")
    comparison = bundle["comparison"]
    lines = [
        "### Recorded teammate flights",
        "",
        f"Local practice with {comparison['debris_count']} debris bodies, "
        f"{len(comparison['seeds'])} matched seeds and {comparison['repetitions']} fresh "
        "attempts per seed and profile. Not an official leaderboard.",
        "",
        "![Completion fractions, sampled durations, fuel and episode counts]"
        f"({asset_prefix}/efficiency.svg)",
        "",
        "| Profile | Arrivals / attempts | Median duration, s | Estimated fuel, kg |",
        "| --- | ---: | ---: | ---: |",
    ]
    for profile in bundle["profiles"]:
        cells = [
            f"{profile['owner']} / {profile['label']}",
            f"{profile['successes']}/{profile['attempts']}",
            _format(profile["median_time_s"]),
            _format(profile["median_fuel_used_kg"]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    outcome_names = {"failed_damage": "damage failure", "interrupted": "interrupted attempt"}
    for profile in bundle["profiles"]:
        other_outcomes = ", ".join(
            f"{count} {outcome_names.get(name, name.replace('_', ' '))}{'s' if count != 1 else ''}"
            for name, count in profile["outcomes"].items()
            if name != "success"
        )
        if other_outcomes:
            lines.extend([f"{profile['owner']} also recorded {other_outcomes}.", ""])
    lines.extend(
        [
            "Duration and fuel are medians of available physical results, including failed "
            "flights and partial timeouts. Errors count as attempts, not physical measurements. "
            "A timeout is not an arrival time. Missing readings stay missing.",
            "",
            f"{len(comparison['seeds'])} matched seeds with {comparison['repetitions']} "
            "consecutive repeats are not "
            f"{len(comparison['seeds']) * comparison['repetitions']} independent seed draws. "
            "Arrival counts are descriptive, not proof of robustness. "
            "Arrival means entry to the station region, not safe docking.",
            "",
            "STP points are unavailable because local fuel is not STP's fuel counter. "
            "The [scoring notes](scoring.md) preserve the wiki/code disagreement; "
            "these profiles are unranked.",
            "",
        ]
    )
    if comparison["definitions"] is not None:
        analytics_runs = sum(profile["analytics_runs"] for profile in bundle["profiles"])
        physical_runs = sum(profile["physical_runs"] for profile in bundle["profiles"])
        lines.extend(
            [
                f"Native burn and turn measurements cover {analytics_runs}/{physical_runs} "
                "physical results. Fuel and turning measurements describe the local model, "
                "not competition efficiency.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "No burn or turn history has been reconstructed where native measurements "
                "are missing.",
                "",
            ]
        )
    lines.extend(
        [
            f"[Fuel and burns]({asset_prefix}/fuel-burns.svg) · "
            f"[Turns and rotation]({asset_prefix}/turns.svg) · "
            "[How to read and repeat this comparison](#reading-the-comparison)",
            "",
            f"[Full dataset and build record]({asset_prefix}/dataset.json) · "
            f"[Trials CSV]({asset_prefix}/trials.csv) · "
            f"[Summary CSV]({asset_prefix}/aggregate.csv) · "
            f"[Samples CSV]({asset_prefix}/samples.csv) · "
            f"[Artifact checksums]({asset_prefix}/manifest.json)",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="Existing private run_sweep directory"
    )
    parser.add_argument("--output", type=Path, required=True, help="New public output directory")
    parser.add_argument(
        "--refresh", action="store_true", help="Replace only digest-verified generated artifacts"
    )
    args = parser.parse_args(argv)
    try:
        (refresh_scoreboard if args.refresh else publish_scoreboard)(args.input, args.output)
    except (OSError, ValueError) as error:
        # Keep raw JSON contents and exception messages out of public artifacts.
        parser.exit(
            2, f"Scoreboard publication failed: {type(error).__name__}. Check the saved cohort.\n"
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
