"""Publish bounded, allowlisted trajectories from saved sweeps without flying again.

Only recorded public ownship measurements, public target navigation and two radar
reductions enter the publication. Source snapshots, sensor payloads and exception
messages are never copied. The source archive and older publications stay intact.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .route_frame import route_basis
from .sweep_analysis import analyze_run, summarize_runs

COLUMNS = (
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
)
TRIAL_COLUMNS = (
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
)
MAX_BYTES = 4 * 1024 * 1024
MAX_RUNS = 128
MAX_SAMPLES = 512
ANCHOR_TOLERANCE_M = 1e-6
AXIS_TOLERANCE = 1e-9

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_EXECUTION = "owned_native_manual_step"
_PRESETS = {"competition_reference", "far_side_l2", "mars_facing_l1"}
_DISTRIBUTIONS = {"official_cloud", "directed_stream", "transit_stream", "impact_ejecta"}
_PHYSICAL = {"success", "failed_damage", "timeout"}
_OUTCOMES = _PHYSICAL | {"policy_error", "engine_error", "interrupted"}
_PROFILES = {
    "wso_official_profile": ("Ali", "Ali official profile"),
    "ali_official_profile": ("Ali", "Ali official profile"),
    "official_basic_beeline": ("Published", "Published BasicController"),
    "baseline": ("WSO", "WSO checkpoint baseline"),
    "body-axis": ("WSO", "WSO checkpoint body-axis"),
    "basic_pd_radar": ("Harry", "Harry basic PD/radar"),
    "bui_alpha_beta_mvp": ("Bui", "Bui alpha-beta + sidestep"),
    "tom_kalman_velocity_mvp": ("Tom", "Tom Kalman + velocity governor"),
}
_LABELS = {
    "official": "Competition hop · official cloud",
    "team": "Historical L2 · directed-stream practice",
}
_ARTIFACT_PATHS = {
    "official": "official.json",
    "official-trials": "official-trials.csv",
    "team": "team.json",
}
_BASIS_KEYS = {
    "origin_reference_m",
    "target_initial_reference_m",
    "along_axis",
    "lateral_axis",
    "normal_axis",
}
_COMPARISON_KEYS = {
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
_RUN_KEYS = {
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
_FINAL_COLUMNS = {
    "time_s": 0,
    "range_m": 4,
    "relative_speed_mps": 5,
    "fuel_kg": 6,
    "damage_j": 7,
}
_ROW_METRICS = (
    "elapsed_sim_s",
    "final_target_range_m",
    "final_relative_speed_mps",
    "fuel_used_kg_estimate",
    "max_damage_j_estimate",
    "first_damage_after_s",
    "first_damage_by_s",
)
_GROUP_INTS = ("debris_count", "runs", "successes", "physical_runs", "runs_with_observed_damage")
_GROUP_METRICS = (
    "median_success_time_s",
    "median_damage_failure_time_s",
    "median_first_damage_by_s",
    "median_final_target_range_m",
    "median_final_relative_speed_mps",
    "median_fuel_used_kg_estimate",
)
_LIMITATIONS = [
    "Saved local practice-model evidence only, not an official STP result, score or docking claim. "
    "No simulation or reflight is performed by this publisher.",
    "Only allowlisted ownship measurements and derived metrics are published. Camera, raw radar, "
    "descriptors, source snapshots, exception strings, credentials and hidden debris are absent.",
    "The launch_route_reference frame is fixed at the first ownship position S and initial target "
    "G. Along is dot(p-S,e1), where e1=(G-S)/|G-S|. Lateral uses the normalized projection of the "
    "least-parallel Cartesian axis onto the plane perpendicular to e1, with X/Y/Z tie order. "
    "Normal uses e3=e1 cross e2. Lateral and normal are signed, not geographic or terrain normals.",
    "Every trial shares the published start, initial target and basis within 1e-6 m for anchors "
    "and 1e-9 for unit axes. The target may move; the route frame never follows it.",
    "Commands stored with observation row t were applied during the PREVIOUS interval, not the "
    "next interval. accel_mps2 and angular_accel_rad_s2 are requested command magnitudes; "
    "ax_mps2, ay_mps2 and az_mps2 are requested body-frame components, not route-frame or "
    "measured total accelerations. Initial commands are null because no previous interval exists.",
    "command_from_s is the actual preceding observation timestamp for the recorded command. "
    "The request was held from command_from_s until time_s. Thinning does not stretch it to "
    "the previous retained sample or interpolate omitted requests.",
    "fuel_kg means cumulative mission.fuel_used_kg, a local physical fuel-use estimate, not "
    "remaining stored fuel or the official STP fuel counter. damage_j is cumulative local damage.",
    "Range and relative speed use public mission measurements, or public ownship/target "
    "navigation when that measurement is absent. Missing measurements stay null, never zero.",
    "nearest_radar_m is the smallest range in the same observation's public radar rows. "
    "max_radar_age_s is the largest row age on the simulation clock. Negative noisy ranges and "
    "negative ages from future timestamps are retained as diagnostics, not discarded or clamped. "
    "Neither is hidden nearest debris truth, a collision margin, or proof that unobserved space "
    "is clear.",
    "terrain_clearance_m is radial centre clearance from public ownship position and the "
    "same-row reported terrain radius. It is null without both readings. No terrain model, "
    "enclosing-radius substitute, hull clearance or debris clearance is inferred.",
    "Samples are actual observations, not interpolated trajectories. Deterministic thinning "
    "retains first/last observations, every observed damage-increase bracket, and both sides "
    "of every null gap. Unsampled motion and contacts remain unknown.",
    "Damage events bracket increases between known damage readings; their value is cumulative "
    "damage, not an exact contact time or cause. A terminal marker denotes the last saved "
    "observation, including timeout and software-error partial horizons, not an inferred event.",
    "Same-seed, same-repetition runs are paired within this cohort only. Repetitions are not "
    "independent seeds. Different comparison files may use different worlds and engine builds.",
]
_MOVED_TARGET = (
    "Public target navigation moved more than 1e-6 m from its initial anchor in this cohort. "
    "The published target marker is the initial anchor; range remains relative to the observed "
    "target at each sample."
)
_INCOMPLETE_ROWS = (
    "The original per-trial runs.csv is incomplete: some numeric cells are missing or its zero "
    "sample counts are unavailable placeholders. CSV identities, outcomes and populated "
    "measurements were checked. Exported finals and sample counts were re-derived from saved "
    "trace.jsonl with current analyze_run, not recovered from the missing table. Recomputed "
    "group metrics were checked against saved summary.csv. The original CSV was not rewritten."
)
_SPLIT_DISTRIBUTION = (
    "This historical observation format records debris_distribution beside experiment_settings, "
    "not inside its debris settings. Both genuine fields are combined only to compare the saved "
    "world receipt. A missing or contradictory distribution is rejected, never inferred."
)


def _number(value: Any, *, signed: bool = False, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float):
        raise ValueError("A measurement must be a finite number or null")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("A measurement exceeds finite numeric bounds") from error
    if not math.isfinite(result) or (not signed and result < 0):
        raise ValueError("A measurement must be finite and have the declared sign")
    return result


def _integer(value: Any, *, minimum: int = 0, maximum: int = 0xFFFFFFFF) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("An integer is missing or outside its declared bounds")
    return value


def _choice(value: Any, choices: set | dict) -> bool:
    return isinstance(value, str) and value in choices


def _object(value: Any, *, optional: bool = False) -> dict:
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ValueError("A saved measurement or metadata object has an invalid shape")
    return value


def _vector(value: Any, *, optional: bool = True) -> list[float] | None:
    if value is None and optional:
        return None
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("A vector must contain exactly three finite numbers")
    return [_number(cell, signed=True, optional=False) for cell in value]


def _norm(vector: Sequence[float]) -> float:
    return _number(math.hypot(*vector), optional=False)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    try:
        result = math.fsum(a * b for a, b in zip(left, right, strict=True))
    except (OverflowError, ValueError) as error:
        raise ValueError("A derived coordinate exceeds finite numeric bounds") from error
    return _number(result, signed=True, optional=False)


def _difference(left: Sequence[float], right: Sequence[float]) -> list[float]:
    return [_number(a - b, signed=True, optional=False) for a, b in zip(left, right, strict=True)]


def _close(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-6)


def _basis(origin: list[float], target: list[float]) -> tuple[float, dict]:
    return route_basis(origin, target)


def _same_basis(left: dict, right: dict) -> bool:
    for key in _BASIS_KEYS:
        tolerance = ANCHOR_TOLERANCE_M if key.endswith("reference_m") else AXIS_TOLERANCE
        if any(abs(a - b) > tolerance for a, b in zip(left[key], right[key], strict=True)):
            return False
    return True


def _json_number(raw: str) -> float:
    return _number(float(raw), signed=True, optional=False)


def _json_integer(raw: str) -> int:
    value = int(raw)
    _number(value, signed=True, optional=False)
    return value


def _no_constant(_raw: str) -> None:
    raise ValueError("Saved JSON contains a nonfinite number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Saved JSON contains duplicate object fields")
        result[key] = value
    return result


def _loads(raw: bytes | str) -> Any:
    return json.loads(
        raw,
        parse_float=_json_number,
        parse_int=_json_integer,
        parse_constant=_no_constant,
        object_pairs_hook=_unique_object,
    )


def _plain_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Saved evidence must be a regular file, not a symlink")
    return path.read_bytes()


def _encode(data: Any) -> bytes:
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("A curated publication exceeds the 4 MiB file budget")
    return raw


def _configuration(directory: Path) -> dict:
    config = _object(_loads(_plain_file(directory / "configuration.json")))
    required = {
        "schema_version",
        "preset",
        "seeds",
        "debris_counts",
        "duration_s",
        "step_s",
        "settings",
        "algorithms",
    }
    if set(config) - (required | {"repetitions"}) or required - set(config):
        raise ValueError("Saved sweep configuration is partial or has unknown metadata")
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("Unsupported saved sweep schema")
    if not _choice(config["preset"], _PRESETS):
        raise ValueError("Unreviewed saved flight preset")
    for key in ("seeds", "debris_counts"):
        rows = config[key]
        if not isinstance(rows, list) or not rows:
            raise ValueError("Saved cohort seeds and debris counts must be nonempty lists")
        for value in rows:
            _integer(value)
        if len(set(rows)) != len(rows):
            raise ValueError("Saved cohort metadata contains duplicate seeds or counts")
    if len(config["debris_counts"]) != 1:
        raise ValueError("Publish one matched debris-count cohort at a time")
    config["repetitions"] = _integer(config.get("repetitions", 1), minimum=1, maximum=10)
    algorithms = _object(config["algorithms"])
    if not algorithms or set(algorithms) - set(_PROFILES):
        raise ValueError("Saved comparison contains an unreviewed algorithm ID")
    for settings in algorithms.values():
        _object(settings)
    if len(algorithms) * len(config["seeds"]) * config["repetitions"] > MAX_RUNS:
        raise ValueError("A curated comparison is limited to 128 runs")
    duration, step = (_number(config[key], optional=False) for key in ("duration_s", "step_s"))
    if duration <= 0 or step <= 0 or step > duration:
        raise ValueError("Saved duration and cadence must be positive and consistent")
    ratio = duration / step
    if not math.isfinite(ratio) or not math.isclose(ratio, round(ratio), abs_tol=1e-6):
        raise ValueError("Saved duration must be a multiple of the controller cadence")
    settings = _object(config["settings"])
    if set(settings) != {"ship", "debris"}:
        raise ValueError("Saved world settings must contain ship and debris metadata")
    ship = _object(settings["ship"])
    for key in ("mass_kg", "inertia_scale"):
        if _number(ship.get(key), optional=False) <= 0:
            raise ValueError("Saved ship mass and inertia scale must be positive")
    debris = _object(settings["debris"])
    _integer(debris.get("count"))
    if debris.get("count") != config["debris_counts"][0]:
        raise ValueError("Saved world debris count disagrees with its cohort")
    distribution = _object(debris.get("distribution"))
    if not _choice(distribution.get("kind"), _DISTRIBUTIONS):
        raise ValueError("Saved world has no reviewed explicit debris distribution")
    for key in (
        "radius_min_m",
        "radius_max_m",
        "speed_mps",
        "speed_spread_mps",
        "shell_inner_m",
        "shell_outer_m",
    ):
        if key in debris:
            _number(debris[key], optional=False)
    return config


def _engine(run: dict, config: dict) -> tuple[dict, float, float]:
    engine = _object(run.get("engine"))
    build = _object(engine.get("engine_build"))
    if (
        set(build) != {"bytes", "sha256"}
        or not isinstance(build.get("sha256"), str)
        or not _HASH.fullmatch(build["sha256"])
        or engine.get("wasm_sha256") != build["sha256"]
        or engine.get("backend") != "native_headless"
        or engine.get("execution_kind") != _EXECUTION
        or engine.get("control_mode") != "owned_native_API_manual_step"
    ):
        raise ValueError("Saved trial has no consistent owned-native engine identity")
    _integer(build["bytes"], minimum=1)
    scenario = _object(engine.get("scenario_source"))
    integration = _object(scenario.get("integration"))
    fixed = _number(integration.get("fixed_step_s"), optional=False)
    mission = _object(scenario.get("mission"))
    arrival = _number(mission.get("arrival_radius_m"), optional=False)
    if fixed <= 0 or arrival <= 0:
        raise ValueError("Saved engine physics step and arrival radius must be positive")
    for key in ("step_s", "duration_s"):
        ratio = config[key] / fixed
        if not math.isfinite(ratio) or not math.isclose(ratio, round(ratio), abs_tol=1e-6):
            raise ValueError("Saved controller schedule disagrees with its engine physics step")
    # Compare the entire saved world definition, not today's scenario or a source import.
    identity = {"engine_build": build, "scenario_source": scenario}
    for key, value in engine.items():
        if key.endswith("_sha256"):
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise ValueError("Saved engine provenance has an invalid digest")
            identity[key] = value
    return identity, fixed, arrival


def _radar(observation: dict, now: float) -> tuple[float | None, float | None]:
    rows = observation.get("radar")
    if rows is None:
        return None, None
    if not isinstance(rows, list):
        raise ValueError("Public radar rows have an invalid shape")
    ranges, ages = [], []
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("Public radar rows must be three-by-three arrays")
        parts = [_vector(part, optional=False) for part in row]
        distance = _number(parts[1][0], signed=True, optional=False)
        measured_at = _number(parts[2][0], signed=True, optional=False)
        age = _number(now - measured_at, signed=True, optional=False)
        ranges.append(distance)
        ages.append(age)
    return (min(ranges), max(ages)) if ranges else (None, None)


def _target_metric(mission: dict, key: str, ship: dict, target: dict) -> float | None:
    measured = _number(mission.get(key))
    if measured is not None:
        return measured
    field = "position" if key == "target_distance_m" else "velocity"
    left, right = (_vector(item.get(field)) for item in (ship, target))
    return _norm(_difference(left, right)) if left is not None and right is not None else None


def _sample(
    sample: Any, basis: dict, previous_time: float | None = None
) -> tuple[list, list[float] | None]:
    sample = _object(sample)
    observation = _object(sample.get("observation"))
    now = _number(observation.get("time_s"), optional=False)
    mission = _object(observation.get("mission"), optional=True)
    ship = _object(observation.get("shuttle"), optional=True)
    target = _object(observation.get("target"), optional=True)
    position = _vector(ship.get("position"))
    target_position = _vector(target.get("position"))
    # Validate navigation shapes even if a mission measurement is also present.
    _vector(ship.get("velocity"))
    _vector(target.get("velocity"))
    offset = _difference(position, basis["origin_reference_m"]) if position is not None else None
    coordinates = (
        [_dot(offset, basis[key]) for key in ("along_axis", "lateral_axis", "normal_axis")]
        if offset is not None
        else [None, None, None]
    )
    accel, angular = (_vector(sample.get(key)) for key in ("accel_command", "angular_command"))
    if now == 0 and (accel is not None or angular is not None):
        raise ValueError("Initial saved commands cannot claim a previous interval")
    nearest, age = _radar(observation, now)
    geometry = _object(observation.get("geometry"), optional=True)
    terrain_radius = _number(geometry.get("terrain_radius_at_shuttle_m"))
    clearance = (
        _number(_norm(position) - terrain_radius, signed=True)
        if position is not None and terrain_radius is not None
        else None
    )
    return [
        now,
        *coordinates,
        _target_metric(mission, "target_distance_m", ship, target),
        _target_metric(mission, "relative_speed_mps", ship, target),
        _number(mission.get("fuel_used_kg")),
        _number(mission.get("damage")),
        _norm(accel) if accel is not None else None,
        _norm(angular) if angular is not None else None,
        nearest,
        age,
        clearance,
        *(accel if accel is not None else [None, None, None]),
        previous_time if accel is not None or angular is not None else None,
    ], target_position


def _events(rows: list[list]) -> list[dict]:
    events = []
    previous = None
    for row in rows:
        damage = row[7]
        if damage is None:
            continue
        if previous is not None and damage < previous[7]:
            raise ValueError("Saved cumulative damage decreased")
        if (previous is None and damage > 0) or (previous is not None and damage > previous[7]):
            events.append(
                {
                    "kind": "damage",
                    "after_s": previous[0] if previous is not None else None,
                    "by_s": row[0],
                    "value": damage,
                }
            )
        previous = row
    if rows:
        events.append({"kind": "terminal", "after_s": None, "by_s": rows[-1][0], "value": None})
    return events


def _mandatory(rows: list[list]) -> set[int]:
    indices = {0, len(rows) - 1}
    previous_damage = None
    for index, row in enumerate(rows):
        if row[7] is not None:
            if previous_damage is None:
                if row[7] > 0:
                    indices.add(index)
            elif row[7] > rows[previous_damage][7]:
                indices.update((previous_damage, index))
            previous_damage = index
        if index and any(
            (a is None) != (b is None) for a, b in zip(rows[index - 1], row, strict=True)
        ):
            indices.update((index - 1, index))
    return indices


def _thin(rows: list[list], count: int, mandatory: set[int]) -> list[list]:
    if len(mandatory) > count:
        raise ValueError("Damage brackets and null gaps exceed the retained-sample budget")
    if len(rows) <= count:
        return deepcopy(rows)
    optional = [index for index in range(len(rows)) if index not in mandatory]
    slots = count - len(mandatory)
    # Integer midpoint quantiles select actual observations without random state.
    selected = {optional[(2 * index + 1) * len(optional) // (2 * slots)] for index in range(slots)}
    return [list(rows[index]) for index in sorted(mandatory | selected)]


@dataclass
class _Flight:
    public: dict
    rows: list[list]
    mandatory: set[int]
    analysis: dict
    identity: dict
    basis: dict
    route_length: float
    arrival_radius: float
    moved_target: bool
    split_distribution: bool


def _trial(path: Path, config: dict) -> _Flight:
    run_bytes = _plain_file(path / "run.json")
    run = _object(_loads(run_bytes))
    algorithm = run.get("algorithm")
    if not isinstance(algorithm, str) or algorithm not in config["algorithms"]:
        raise ValueError("Saved trial algorithm is not in its configured cohort")
    expected = {
        "schema_version": 1,
        "execution_kind": _EXECUTION,
        "preset": config["preset"],
        "debris_count": config["debris_counts"][0],
        "duration_s": config["duration_s"],
        "step_s": config["step_s"],
        "settings": config["settings"],
        "algorithm_settings": config["algorithms"][algorithm],
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError("Saved trials contain partial or mixed world/configuration metadata")
    _integer(run.get("schema_version"), minimum=1, maximum=1)
    _integer(run.get("debris_count"))
    _number(run.get("duration_s"), optional=False)
    _number(run.get("step_s"), optional=False)
    seed = _integer(run.get("seed"))
    repetition = _integer(run.get("repetition", 1), minimum=1, maximum=config["repetitions"])
    if seed not in config["seeds"] or not _choice(run.get("outcome"), _OUTCOMES):
        raise ValueError("Saved trial has an unexpected seed or outcome")
    identity, fixed, arrival = _engine(run, config)
    trace_path = path / "trace.jsonl"
    if trace_path.is_symlink() or not trace_path.is_file():
        raise ValueError("Saved trace must be a regular file, not a symlink")
    digest = hashlib.sha256()
    samples, rows = [], []
    basis = None
    context = None
    previous_status = None
    moved_target = False
    split_distribution = False
    previous_fuel = None
    maximum_count = round(config["duration_s"] / config["step_s"]) + 1
    with trace_path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            sample = _object(_loads(line))
            observation = _object(sample.get("observation"))
            if basis is None:
                ship = _object(observation.get("shuttle"))
                target = _object(observation.get("target"))
                route_length, basis = _basis(
                    _vector(ship.get("position"), optional=False),
                    _vector(target.get("position"), optional=False),
                )
            row, target_position = _sample(sample, basis, rows[-1][0] if rows else None)
            now = row[0]
            if (not rows and now != 0) or (rows and now <= rows[-1][0]):
                raise ValueError("Saved observation clock omitted its start, repeated or rewound")
            tolerance = 1e-7 * max(1, now)
            mission = _object(observation.get("mission"), optional=True)
            status = mission.get("status")
            if status is not None and not _choice(status, {"running", "success", "failed_damage"}):
                raise ValueError("Saved mission status is invalid")
            frame = observation.get("frame")
            if not isinstance(frame, str) or not frame:
                raise ValueError("Saved public navigation is missing its reference frame")
            if not rows:
                identity["observation_frame"] = frame
            elif frame != identity["observation_frame"]:
                raise ValueError("Saved public navigation changed its reference frame")
            if previous_status in {"success", "failed_damage"}:
                raise ValueError("Saved trace advanced after a native terminal outcome")
            if now > config["duration_s"] + tolerance or len(rows) >= maximum_count:
                raise ValueError("Saved trace exceeded its configured observation budget")
            if rows:
                elapsed = now - rows[-1][0]
                if elapsed > config["step_s"] + tolerance or (
                    status not in {"success", "failed_damage"}
                    and not math.isclose(elapsed, config["step_s"], abs_tol=tolerance)
                ):
                    raise ValueError("Saved observations do not match their configured cadence")
            ratio = now / fixed
            if not math.isfinite(ratio) or not math.isclose(ratio, round(ratio), abs_tol=1e-5):
                raise ValueError("Saved observations disagree with the saved native physics step")
            current_context = tuple(
                observation.get(key)
                for key in ("seed", "trial_revision", "tuning_revision", "manual_interventions")
            )
            for value in current_context:
                _integer(value)
            if (
                current_context[0] != seed
                or current_context[3] != 0
                or (context is not None and current_context != context)
            ):
                raise ValueError("Saved observation changed its public trial source context")
            context = current_context
            observed_settings = deepcopy(_object(observation.get("experiment_settings")))
            observed_debris = _object(observed_settings.get("debris"))
            distribution = observation.get("debris_distribution")
            if "distribution" not in observed_debris and distribution is not None:
                # Older genuine observations carry this receipt next to settings.
                # Compare the two explicit fields; never fill it from configuration.
                observed_debris["distribution"] = deepcopy(distribution)
                split_distribution = True
            if observation.get("flight_preset") != config["preset"] or (
                observed_settings != config["settings"]
                or distribution != config["settings"]["debris"]["distribution"]
            ):
                raise ValueError("Public observations contain partial or mixed world settings")
            geometry = _object(observation.get("geometry"), optional=True)
            if "arrival_radius_m" in geometry and not _close(
                _number(geometry["arrival_radius_m"], optional=False), arrival
            ):
                raise ValueError("Public arrival radius disagrees with the saved engine")
            if (
                target_position is not None
                and _norm(_difference(target_position, basis["target_initial_reference_m"]))
                > ANCHOR_TOLERANCE_M
            ):
                moved_target = True
            if row[6] is not None:
                if previous_fuel is not None and row[6] < previous_fuel:
                    raise ValueError("Saved cumulative fuel use decreased")
                previous_fuel = row[6]
            samples.append(sample)
            rows.append(row)
            previous_status = status
    if not rows:
        raise ValueError("A paired trajectory requires its initial saved observation")
    expected_status = {"success": "success", "failed_damage": "failed_damage", "timeout": "running"}
    outcome = run["outcome"]
    if outcome in _PHYSICAL and previous_status != expected_status[outcome]:
        raise ValueError("Saved outcome disagrees with the last observed mission status")
    if outcome not in {"success", "failed_damage"} and previous_status in {
        "success",
        "failed_damage",
    }:
        raise ValueError("Saved software outcome hides an observed native terminal outcome")
    if outcome == "timeout" and not _close(rows[-1][0], config["duration_s"]):
        raise ValueError("A timeout must retain its configured final observation")
    _events(rows)
    analysis = analyze_run({**run, "samples": samples})
    if not _close(analysis["elapsed_sim_s"], rows[-1][0]) or any(
        not _close(analysis[key], rows[-1][column])
        for key, column in (
            ("final_target_range_m", 4),
            ("final_relative_speed_mps", 5),
            ("fuel_used_kg_estimate", 6),
        )
    ):
        raise ValueError("Curated final measurements disagree with current analyze_run")
    owner, label = _PROFILES[algorithm]
    public = {
        "id": path.name,
        "algorithm": algorithm,
        "owner": owner,
        "label": label,
        "seed": seed,
        "repetition": repetition,
        "outcome": outcome,
        "source_sample_count": len(rows),
        "samples": [],
        "events": _events(rows),
        "final": {key: rows[-1][column] for key, column in _FINAL_COLUMNS.items()},
        "source_sha256": {
            "run": hashlib.sha256(run_bytes).hexdigest(),
            "trace": digest.hexdigest(),
        },
    }
    return _Flight(
        public,
        rows,
        _mandatory(rows),
        analysis,
        identity,
        basis,
        route_length,
        arrival,
        moved_target,
        split_distribution,
    )


def _csv_rows(path: Path, required: set[str]) -> list[dict]:
    text = _plain_file(path).decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = reader.fieldnames
    if not fields or len(set(fields)) != len(fields) or required - set(fields):
        raise ValueError("A saved summary has a partial or duplicate CSV header")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("A saved summary contains malformed CSV rows")
    return rows


def _csv_integer(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise ValueError("A saved summary integer is invalid")
    return _integer(int(value))


def _csv_number(value: str) -> float | None:
    return None if value == "" else _number(float(value), optional=False)


def _crosscheck_summaries(directory: Path, flights: list[_Flight]) -> bool:
    required = {
        "trial_directory",
        "algorithm",
        "seed",
        "repetition",
        "debris_count",
        "outcome",
        "sample_count",
        *_ROW_METRICS,
    }
    rows = _csv_rows(directory / "runs.csv", required)
    if len(rows) != len(flights):
        raise ValueError("Saved row summary omitted paired attempts")
    incomplete = False
    seen = set()
    by_id = {flight.public["id"]: flight for flight in flights}
    for row in rows:
        name = row["trial_directory"]
        if name not in by_id or name in seen:
            raise ValueError("Saved row summary contains duplicate or unexpected paired attempts")
        seen.add(name)
        summary = by_id[name].analysis
        for key in ("algorithm", "outcome"):
            if row[key] != summary[key]:
                raise ValueError(
                    "Saved row summary outcome or algorithm disagrees with analyze_run"
                )
        for key in ("seed", "repetition", "debris_count"):
            if _csv_integer(row[key]) != summary[key]:
                raise ValueError("Saved row summary pairing disagrees with analyze_run")
        if row["sample_count"] == "" or _csv_integer(row["sample_count"]) == 0:
            # Historical official CSVs recorded empty-observation analyses even
            # though the corresponding trace files contain genuine observations.
            incomplete = True
        elif _csv_integer(row["sample_count"]) != summary["sample_count"]:
            raise ValueError("Saved row summary sample count disagrees with current analyze_run")
        for key in _ROW_METRICS:
            reported = _csv_number(row[key])
            if reported is None:
                incomplete |= summary[key] is not None
            elif not _close(reported, summary[key]):
                raise ValueError(
                    "Saved row summary final metrics disagree with current analyze_run"
                )
    expected = {
        row["algorithm"]: row for row in summarize_runs(flight.analysis for flight in flights)
    }
    json_path = directory / "summary.json"
    if json_path.exists() or json_path.is_symlink():
        groups = _loads(_plain_file(json_path))
        if not isinstance(groups, list):
            raise ValueError("Saved aggregate summary must be a complete group list")
        _crosscheck_groups(groups, expected)
    csv_path = directory / "summary.csv"
    csv_groups = _csv_rows(csv_path, {"algorithm", "outcomes", *_GROUP_INTS, *_GROUP_METRICS})
    decoded = []
    for row in csv_groups:
        try:
            outcomes = ast.literal_eval(row["outcomes"])
        except (ValueError, SyntaxError) as error:
            raise ValueError("Saved aggregate outcome summary is malformed") from error
        decoded.append(
            {
                "algorithm": row["algorithm"],
                "outcomes": outcomes,
                **{key: _csv_integer(row[key]) for key in _GROUP_INTS},
                **{key: _csv_number(row[key]) for key in _GROUP_METRICS},
            }
        )
    _crosscheck_groups(decoded, expected)
    return incomplete


def _crosscheck_groups(groups: list, expected: dict[str, dict]) -> None:
    seen = set()
    for raw in groups:
        row = _object(raw)
        algorithm = row.get("algorithm")
        if not isinstance(algorithm, str) or algorithm not in expected or algorithm in seen:
            raise ValueError("Saved aggregate summary contains duplicate or unexpected groups")
        seen.add(algorithm)
        actual = expected[algorithm]
        outcomes = _object(row.get("outcomes"))
        if set(outcomes) - _OUTCOMES:
            raise ValueError("Saved aggregate summary has an unreviewed outcome")
        for value in outcomes.values():
            _integer(value, minimum=1, maximum=MAX_RUNS)
        if (
            outcomes != actual["outcomes"]
            or any(_integer(row.get(key)) != actual[key] for key in _GROUP_INTS)
            or any(
                key not in row or not _close(_number(row[key]), actual[key])
                for key in _GROUP_METRICS
            )
        ):
            raise ValueError(
                "Saved aggregate metrics or outcomes disagree with current analyze_run"
            )
    if seen != set(expected):
        raise ValueError("Saved aggregate summary omitted configured groups")


def build_comparison(
    directory: str | Path, *, comparison_id: str, max_samples: int = MAX_SAMPLES
) -> dict:
    """Read a complete saved cohort and return its detached public DTO. No writes.

    Raises ValueError on malformed, partial, mixed, inconsistent or over-budget
    evidence. Missing row-summary cells remain unavailable; observed finals come
    from the saved trace and require matching measured summary.csv aggregates.
    """
    if not _choice(comparison_id, _LABELS):
        raise ValueError("comparison_id must be official or team")
    _integer(max_samples, minimum=2, maximum=MAX_SAMPLES)
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Saved sweep input must be a directory, not a symlink")
    config = _configuration(directory)
    paths = sorted(directory.glob("trial-*"))
    if not 1 <= len(paths) <= MAX_RUNS or any(
        path.is_symlink() or not path.is_dir() or path.name != f"trial-{index:05d}"
        for index, path in enumerate(paths)
    ):
        raise ValueError("Saved cohort must contain consecutive regular trial directories")
    flights = [_trial(path, config) for path in paths]
    expected = [
        (algorithm, seed, repetition)
        for seed in config["seeds"]
        for algorithm in config["algorithms"]
        for repetition in range(1, config["repetitions"] + 1)
    ]
    actual = [
        (row.public["algorithm"], row.public["seed"], row.public["repetition"]) for row in flights
    ]
    if actual != expected:
        raise ValueError(
            "Saved cohort must retain every paired attempt in consecutive repetition order"
        )
    first = flights[0]
    if any(flight.identity != first.identity for flight in flights):
        raise ValueError("Saved cohort mixes engine identity or world definitions")
    if any(not _same_basis(flight.basis, first.basis) for flight in flights):
        raise ValueError(
            "Saved paired trials do not share the same launch/target anchors and basis"
        )
    incomplete_rows = _crosscheck_summaries(directory, flights)
    limitations = list(_LIMITATIONS)
    if any(flight.moved_target for flight in flights):
        limitations.append(_MOVED_TARGET)
    if incomplete_rows:
        limitations.append(_INCOMPLETE_ROWS)
    if any(flight.split_distribution for flight in flights):
        limitations.append(_SPLIT_DISTRIBUTION)
    data = {
        "schema": "wsorb.path_comparison",
        "schema_version": 1,
        "id": comparison_id,
        "label": _LABELS[comparison_id],
        "comparison": {
            "preset": config["preset"],
            "distribution": config["settings"]["debris"]["distribution"]["kind"],
            "debris_count": config["debris_counts"][0],
            "seeds": list(config["seeds"]),
            "repetitions": config["repetitions"],
            "duration_s": config["duration_s"],
            "step_s": config["step_s"],
            "engine_sha256": first.identity["engine_build"]["sha256"],
            "arrival_radius_m": first.arrival_radius,
            "route_length_m": first.route_length,
            "basis": deepcopy(first.basis),
            "frame": "launch_route_reference",
            "limitations": limitations,
        },
        "columns": list(COLUMNS),
        "runs": [flight.public for flight in flights],
    }
    counts = [min(max_samples, len(flight.rows)) for flight in flights]
    while True:
        for flight, count in zip(flights, counts, strict=True):
            flight.public["samples"] = _thin(flight.rows, count, flight.mandatory)
        try:
            _encode(data)
            break
        except ValueError:
            reduced = [
                max(len(flight.mandatory), count * 3 // 4)
                for flight, count in zip(flights, counts, strict=True)
            ]
            if counts == reduced:
                raise ValueError(
                    "Required trajectory evidence exceeds the 4 MiB file budget"
                ) from None
            counts = reduced
    return validate_comparison(data)


def validate_comparison(data: Any) -> dict:
    """Return a detached strict public DTO, or raise ValueError. Unknown fields fail.

    This validates the publication's declared evidence. Only build_comparison can
    also compare it with source files, engine receipts and saved summaries.
    """
    data = _object(data)
    if set(data) != {"schema", "schema_version", "id", "label", "comparison", "columns", "runs"}:
        raise ValueError("A trajectory publication has unknown or missing top-level fields")
    if (
        data["schema"] != "wsorb.path_comparison"
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
    ):
        raise ValueError("Unsupported trajectory publication schema")
    if (
        not isinstance(data["id"], str)
        or data["id"] not in _LABELS
        or data["label"] != _LABELS[data["id"]]
    ):
        raise ValueError("A trajectory publication has an unreviewed ID or label")
    if type(data["columns"]) is not list or data["columns"] != list(COLUMNS):
        raise ValueError("Trajectory columns differ from the versioned column contract")
    comparison = _object(data["comparison"])
    if set(comparison) != _COMPARISON_KEYS or comparison["frame"] != "launch_route_reference":
        raise ValueError("A trajectory comparison has unknown or missing metadata")
    if not _choice(comparison["preset"], _PRESETS) or not _choice(
        comparison["distribution"], _DISTRIBUTIONS
    ):
        raise ValueError("A trajectory comparison has an unreviewed world label")
    _integer(comparison["debris_count"])
    repetitions = _integer(comparison["repetitions"], minimum=1, maximum=10)
    seeds = comparison["seeds"]
    if not isinstance(seeds, list) or not 1 <= len(seeds) <= MAX_RUNS:
        raise ValueError("A trajectory comparison requires bounded seed coverage")
    for seed in seeds:
        _integer(seed)
    if len(set(seeds)) != len(seeds):
        raise ValueError("A trajectory comparison contains duplicate seeds")
    duration, step, arrival, length = (
        _number(comparison[key], optional=False)
        for key in ("duration_s", "step_s", "arrival_radius_m", "route_length_m")
    )
    if min(duration, step, arrival, length) <= 0 or step > duration:
        raise ValueError(
            "Trajectory duration, cadence, arrival radius and route length must be positive"
        )
    ratio = duration / step
    if not math.isfinite(ratio) or not math.isclose(ratio, round(ratio), abs_tol=1e-6):
        raise ValueError("Trajectory duration does not match the declared cadence")
    digest = comparison["engine_sha256"]
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError("A trajectory comparison has an invalid engine digest")
    basis = _object(comparison["basis"])
    if set(basis) != _BASIS_KEYS:
        raise ValueError("A trajectory comparison has an incomplete or unknown basis")
    vectors = {key: _vector(value, optional=False) for key, value in basis.items()}
    expected_length, expected_basis = _basis(
        vectors["origin_reference_m"], vectors["target_initial_reference_m"]
    )
    if not _close(length, expected_length) or not _same_basis(vectors, expected_basis):
        raise ValueError("Trajectory route length or basis disagrees with the published anchors")
    limitations = comparison["limitations"]
    if not isinstance(limitations, list) or limitations[: len(_LIMITATIONS)] != _LIMITATIONS:
        raise ValueError(
            "A trajectory publication must retain its reviewed measurement limitations"
        )
    extras = limitations[len(_LIMITATIONS) :]
    order = (_MOVED_TARGET, _INCOMPLETE_ROWS, _SPLIT_DISTRIBUTION)
    if extras != [note for note in order if note in extras]:
        raise ValueError("A trajectory publication contains unreviewed limitation text")
    runs = data["runs"]
    if not isinstance(runs, list) or not 1 <= len(runs) <= MAX_RUNS:
        raise ValueError("A trajectory comparison is limited to 128 runs")
    seen = set()
    algorithms = set()
    for index, run in enumerate(runs):
        _validate_run(run, index, seeds, repetitions, duration, step)
        key = run["algorithm"], run["seed"], run["repetition"]
        if key in seen:
            raise ValueError("A trajectory comparison contains duplicate paired attempts")
        seen.add(key)
        algorithms.add(run["algorithm"])
    if seen != {
        (algorithm, seed, repetition)
        for algorithm in algorithms
        for seed in seeds
        for repetition in range(1, repetitions + 1)
    }:
        raise ValueError("A trajectory comparison omitted paired attempts")
    _encode(data)
    return deepcopy(data)


def _validate_run(
    run: Any, index: int, seeds: list[int], repetitions: int, duration: float, step: float
) -> None:
    run = _object(run)
    if set(run) != _RUN_KEYS or run["id"] != f"trial-{index:05d}":
        raise ValueError("A trajectory run has unknown fields or a nonconsecutive source ID")
    algorithm = run["algorithm"]
    if (
        not isinstance(algorithm, str)
        or algorithm not in _PROFILES
        or (run["owner"], run["label"]) != _PROFILES[algorithm]
    ):
        raise ValueError("A trajectory run changed a reviewed algorithm identity or owner label")
    _integer(run["seed"])
    _integer(run["repetition"], minimum=1, maximum=repetitions)
    if run["seed"] not in seeds or not _choice(run["outcome"], _OUTCOMES):
        raise ValueError("A trajectory run is outside its declared cohort")
    count = _integer(run["source_sample_count"], minimum=1)
    samples = run["samples"]
    if (
        not isinstance(samples, list)
        or not 1 <= len(samples) <= min(MAX_SAMPLES, count)
        or count > round(duration / step) + 1
    ):
        raise ValueError("A trajectory run exceeds its source or retained-sample budget")
    previous = None
    cumulative = {}
    for row in samples:
        if not isinstance(row, list) or len(row) != len(COLUMNS):
            raise ValueError("A trajectory sample differs from the declared column shape")
        for column, value in enumerate(row):
            _number(value, signed=column in {1, 2, 3, 10, 11, 12, 13, 14, 15}, optional=column != 0)
        now = row[0]
        if now > duration + 1e-6 or (previous is not None and now <= previous):
            raise ValueError("A trajectory sample clock is outside its horizon or rewound")
        if any(value is None for value in row[1:4]) and row[1:4] != [None, None, None]:
            raise ValueError("Trajectory position components must share a null gap")
        command = row[13:16]
        if any(value is None for value in command):
            if command != [None, None, None] or row[8] is not None:
                raise ValueError("Trajectory command components have an inconsistent null gap")
        elif row[8] is None or not _close(row[8], _norm(command)):
            raise ValueError("Trajectory command magnitude disagrees with its body components")
        command_present = row[8] is not None or row[9] is not None
        if command_present:
            if row[16] is None or not 0 <= row[16] < now or now - row[16] > step + 1e-6:
                raise ValueError("Trajectory command omitted its actual preceding interval")
        elif row[16] is not None:
            raise ValueError("An unavailable command cannot claim a preceding interval")
        for column in (6, 7):
            if row[column] is not None:
                if column in cumulative and row[column] < cumulative[column]:
                    raise ValueError("A trajectory cumulative measurement decreased")
                cumulative[column] = row[column]
        previous = now
    if samples[0][0] != 0 or any(
        value is None or abs(value) > ANCHOR_TOLERANCE_M for value in samples[0][1:4]
    ):
        raise ValueError("A trajectory omitted its actual launch anchor")
    if samples[0][8:10] != [None, None] or samples[0][13:16] != [None, None, None]:
        raise ValueError("A trajectory initial command cannot claim a previous interval")
    intervals = samples[-1][0] / step
    expected_count = math.ceil(intervals - 1e-7) + 1
    if count != expected_count or (count > 1 and len(samples) < 2):
        raise ValueError("A trajectory source count disagrees with its saved observation horizon")
    if run["outcome"] == "timeout" and not _close(samples[-1][0], duration):
        raise ValueError("A trajectory timeout omitted its configured horizon")
    final = _object(run["final"])
    if set(final) != set(_FINAL_COLUMNS) or any(
        not _close(_number(final[key], optional=key != "time_s"), samples[-1][column])
        for key, column in _FINAL_COLUMNS.items()
    ):
        raise ValueError("Trajectory final measurements differ from the retained final observation")
    events = run["events"]
    if not isinstance(events, list) or len(events) > MAX_SAMPLES + 1:
        raise ValueError("Trajectory events exceed their bounded shape")
    for event in events:
        event = _object(event)
        if set(event) != {"kind", "after_s", "by_s", "value"} or not _choice(
            event["kind"], {"damage", "terminal"}
        ):
            raise ValueError("A trajectory event has unknown fields or kind")
        _number(event["after_s"])
        _number(event["by_s"], optional=False)
        _number(event["value"])
    if events != _events(samples):
        raise ValueError(
            "Trajectory events do not retain their observed damage brackets and endpoint"
        )
    hashes = _object(run["source_sha256"])
    if set(hashes) != {"run", "trace"} or any(
        not isinstance(value, str) or not _HASH.fullmatch(value) for value in hashes.values()
    ):
        raise ValueError("A trajectory run has incomplete or invalid source digests")


def _trials_csv(data: dict) -> bytes:
    """Render only exported final evidence, never the incomplete source table."""
    data = validate_comparison(data)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=TRIAL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for run in data["runs"]:
        writer.writerow(
            {
                **{key: run[key] for key in TRIAL_COLUMNS if key not in _FINAL_COLUMNS},
                **run["final"],
            }
        )
    encoded = stream.getvalue().encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise ValueError("A curated publication exceeds the 4 MiB file budget")
    return encoded


def _manifest(data: Any) -> dict:
    data = _object(data)
    if (
        set(data) != {"schema", "schema_version", "artifacts"}
        or data["schema"] != "wsorb.path_comparison.artifacts"
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
    ):
        raise ValueError("A path-comparison manifest has an unknown schema")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > len(_ARTIFACT_PATHS):
        raise ValueError("A path-comparison manifest exceeds its curated artifact set")
    seen = set()
    for entry in artifacts:
        entry = _object(entry)
        if (
            set(entry) != {"id", "path", "bytes", "sha256"}
            or not isinstance(entry["id"], str)
            or entry["id"] not in _ARTIFACT_PATHS
            or entry["id"] in seen
            or entry["path"] != _ARTIFACT_PATHS[entry["id"]]
        ):
            raise ValueError("A path-comparison manifest has an unexpected or duplicate artifact")
        _integer(entry["bytes"], minimum=1, maximum=MAX_BYTES)
        if not isinstance(entry["sha256"], str) or not _HASH.fullmatch(entry["sha256"]):
            raise ValueError("A path-comparison manifest artifact digest is invalid")
        seen.add(entry["id"])
    if "official-trials" in seen and "official" not in seen:
        raise ValueError("Corrected official trial evidence requires its comparison JSON")
    _encode(data)
    return deepcopy(data)


def _output_path(path: Path, directory: Path) -> Path:
    if path.is_symlink():
        raise ValueError("Curated output must not be a symlink")
    resolved = path.resolve()
    root = Path(__file__).resolve().parents[2]
    if resolved.is_relative_to(directory) or any(
        resolved.is_relative_to(root / "docs" / "analytics" / name)
        for name in ("official-comparison", "team-comparison")
    ):
        raise ValueError("Curated output must be outside raw evidence and historical publications")
    return resolved


def write_comparison(
    directory: str | Path,
    output: str | Path,
    *,
    comparison_id: str,
    max_samples: int = MAX_SAMPLES,
    manifest: str | Path | None = None,
) -> dict:
    """Write a curated JSON and optionally upsert its digest manifest entries.

    Existing files may change only when a supplied manifest verifies the previous
    generated bytes. Identical bytes are safe to reuse. Unknown files are refused.
    Official exports also write sibling official-trials.csv from exported finals.
    JSON uses compact sorted keys. Every public file is limited to 4 MiB.
    """
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Saved input must not be a symlink")
    directory = directory.resolve()
    output = _output_path(Path(output), directory)
    manifest_path = _output_path(Path(manifest), directory) if manifest is not None else None
    if manifest_path is not None and (
        manifest_path.parent != output.parent
        or manifest_path == output
        or manifest_path.name != "manifest.json"
        or output.name != f"{comparison_id}.json"
    ):
        raise ValueError("The manifest and named comparison JSON must be sibling curated files")
    old_manifest = (
        _plain_file(manifest_path) if manifest_path is not None and manifest_path.exists() else None
    )
    records = (
        _manifest(_loads(old_manifest))
        if old_manifest is not None
        else {
            "schema": "wsorb.path_comparison.artifacts",
            "schema_version": 1,
            "artifacts": [],
        }
    )
    verified = {}
    previous_dtos = {}
    if manifest_path is not None:
        for entry in records["artifacts"]:
            path = _output_path(manifest_path.parent / entry["path"], directory)
            raw = _plain_file(path)
            if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError(
                    "A curated artifact changed; refusing to overwrite possible user work"
                )
            if entry["id"] in _LABELS:
                dto = validate_comparison(_loads(raw))
                if dto["id"] != entry["id"]:
                    raise ValueError("A curated artifact disagrees with its manifest identity")
                previous_dtos[entry["id"]] = dto
            verified[path] = raw
        if any(entry["id"] == "official-trials" for entry in records["artifacts"]):
            csv_path = manifest_path.parent / "official-trials.csv"
            if verified[csv_path] != _trials_csv(previous_dtos["official"]):
                raise ValueError("Corrected trial CSV disagrees with its exported final evidence")
    bundle = build_comparison(directory, comparison_id=comparison_id, max_samples=max_samples)
    payloads = {output: _encode(bundle)}
    entries = {comparison_id: output}
    if comparison_id == "official":
        csv_path = _output_path(output.with_name("official-trials.csv"), directory)
        if csv_path in (output, manifest_path):
            raise ValueError("Corrected trial CSV and comparison JSON must be separate files")
        payloads[csv_path] = _trials_csv(bundle)
        entries["official-trials"] = csv_path
    previous_bytes = {}
    for path, encoded in payloads.items():
        previous = _plain_file(path) if path.exists() else None
        previous_bytes[path] = previous
        if previous is not None and previous != encoded and path not in verified:
            raise FileExistsError("Refusing to overwrite an unverified existing output file")
    if manifest_path is not None:
        updates = [
            {
                "id": identity,
                "path": path.name,
                "bytes": len(payloads[path]),
                "sha256": hashlib.sha256(payloads[path]).hexdigest(),
            }
            for identity, path in entries.items()
        ]
        records["artifacts"] = sorted(
            [row for row in records["artifacts"] if row["id"] not in entries] + updates,
            key=lambda row: row["id"],
        )
        payloads[manifest_path] = _encode(_manifest(records))
        previous_bytes[manifest_path] = old_manifest
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".wsorb-path-", dir=output.parent) as temporary:
        staged = Path(temporary)
        staging = {}
        for index, (path, encoded) in enumerate(payloads.items()):
            staging[path] = staged / str(index)
            staging[path].write_bytes(encoded)
        # Re-read before replacement so a changed generated file is not discarded.
        for path, previous in (verified | previous_bytes).items():
            if path.is_symlink() or (path.read_bytes() if path.exists() else None) != previous:
                raise ValueError("A curated output changed during publication")
        for path, encoded in payloads.items():
            previous = previous_bytes[path]
            if previous == encoded:
                continue
            if previous is None:
                with path.open("xb") as stream:
                    stream.write(encoded)
            else:
                staging[path].replace(path)
    return bundle


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Existing private saved sweep")
    parser.add_argument("--output", type=Path, required=True, help="Curated comparison JSON file")
    parser.add_argument("--id", dest="comparison_id", choices=tuple(_LABELS), required=True)
    parser.add_argument("--manifest", type=Path, help="Sibling manifest.json to upsert")
    parser.add_argument("--diagnose", action="store_true", help="Show local validation traceback")
    args = parser.parse_args(argv)
    try:
        write_comparison(
            args.input, args.output, comparison_id=args.comparison_id, manifest=args.manifest
        )
    except (OSError, ValueError, TypeError, OverflowError) as error:
        if args.diagnose:
            raise
        parser.exit(
            2, f"Path publication failed: {type(error).__name__}. Check the saved cohort.\n"
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
