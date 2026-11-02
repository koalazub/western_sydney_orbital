"""Pure summaries of seeded local flight sweeps, with no simulator dependency.

Debris count is a density proxy only while the region, sizes and distribution
are fixed. Radar ranges are noisy, incomplete observations, not collision truth.
Damage changes locate an interval to inspect; they do not identify the collider
or establish why the controller failed. Fuel and damage are local estimates.
"""

from __future__ import annotations

import csv
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .flight_analytics import COUNTERS, NATIVE_NUMBERS, FlightAnalyticsRecorder
from .route_frame import route_basis


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _distance(left: Any, right: Any = (0, 0, 0)) -> float | None:
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        return None
    if len(left) != 3 or len(right) != 3:
        return None
    pairs = [(_number(a), _number(b)) for a, b in zip(left, right, strict=True)]
    if any(a is None or b is None for a, b in pairs):
        return None
    return _number(math.hypot(*(a - b for a, b in pairs)))


def _minimum(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _maximum(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _median(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return statistics.median(present) if present else None


def _target_metric(observation: Mapping, metric: str) -> float | None:
    mission = _mapping(observation.get("mission"))
    value = _number(mission.get(metric))
    if value is not None:
        return value
    ship, target = (_mapping(observation.get(name)) for name in ("shuttle", "target"))
    field = "position" if metric == "target_distance_m" else "velocity"
    return _distance(ship.get(field), target.get(field))


def _clearance(observation: Mapping) -> float | None:
    radius = _number(_mapping(observation.get("geometry")).get("terrain_radius_at_shuttle_m"))
    distance = _distance(_mapping(observation.get("shuttle")).get("position"))
    return _number(distance - radius) if distance is not None and radius is not None else None


def _radar(observation: Mapping) -> tuple[list[float], list[float]]:
    ranges, ages = [], []
    now = _number(observation.get("time_s"))
    rows = observation.get("radar", [])
    if not isinstance(rows, (list, tuple)):
        return ranges, ages
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            continue
        if any(not isinstance(part, (list, tuple)) or len(part) != 3 for part in row):
            continue
        distance, measured_at = _number(row[1][0]), _number(row[2][0])
        if distance is not None:
            ranges.append(distance)
        if now is not None and measured_at is not None:
            age = _number(now - measured_at)
            if age is not None:
                ages.append(age)
    return ranges, ages


def analyze_run(run: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize sampled evidence, preserving missing values as None.

    Damage times are observation intervals, not exact contact times. Minima are
    over saved samples, not all internal physics steps. Negative radar ages are
    retained through ``future_radar_rows`` rather than silently called fresh.
    """
    samples = run.get("samples", [])
    observations = [_mapping(sample.get("observation")) for sample in samples]
    first, last = (observations[0], observations[-1]) if observations else ({}, {})
    first_time, last_time = _number(first.get("time_s")), _number(last.get("time_s"))
    elapsed = (
        _number(last_time - first_time)
        if last_time is not None and first_time is not None
        else None
    )
    first_damage_after = first_damage_by = first_damage_phase = None
    previous_damage = previous_time = None
    radar_ranges, radar_ages = [], []
    for observation in observations:
        mission = _mapping(observation.get("mission"))
        damage = _number(mission.get("damage"))
        now = _number(observation.get("time_s"))
        if first_damage_by is None and damage is not None and damage > 0:
            first_damage_by = now
            first_damage_after = previous_time if previous_damage == 0 else None
            first_damage_phase = mission.get("phase")
        if damage is not None:
            previous_damage, previous_time = damage, now
        ranges, ages = _radar(observation)
        radar_ranges.extend(ranges)
        radar_ages.extend(ages)
    policy_times = [_number(sample.get("policy_wall_s")) for sample in samples]
    step_s = _number(run.get("step_s"))
    mission = _mapping(last.get("mission"))
    from .scoring import native_score_breakdown

    recorded_score = _number(mission.get("score"))
    terms = native_score_breakdown(mission.get("score_breakdown"), score=recorded_score) or {}
    recorder = FlightAnalyticsRecorder()
    for observation in observations:
        recorder.observe(observation)
    analytics = recorder.report
    totals = analytics["totals"] if analytics is not None else {}
    return {
        "algorithm": run["algorithm"],
        "seed": run["seed"],
        "repetition": run.get("repetition", 1),
        "debris_count": run["debris_count"],
        "outcome": run["outcome"],
        "error": run.get("error"),
        "sample_count": len(samples),
        "elapsed_sim_s": elapsed,
        "final_phase": mission.get("phase"),
        "final_score": recorded_score,
        "score_formula": terms.get("formula"),
        "score_progress_points": terms.get("progress_points"),
        "score_collision_penalty_points": terms.get("collision_penalty_points"),
        "score_fuel_penalty_points": terms.get("fuel_penalty_points"),
        "score_offset_points": terms.get("offset_points"),
        "analytics_available": analytics is not None,
        "analytics_definition_id": analytics["definition_id"] if analytics else None,
        **{key: totals.get(key) for key in (*COUNTERS, *NATIVE_NUMBERS)},
        "final_target_range_m": _target_metric(last, "target_distance_m"),
        "final_relative_speed_mps": _target_metric(last, "relative_speed_mps"),
        "closest_sampled_target_range_m": _minimum(
            _target_metric(observation, "target_distance_m") for observation in observations
        ),
        "min_sampled_terrain_center_clearance_m": _minimum(map(_clearance, observations)),
        "fuel_used_kg_estimate": _number(mission.get("fuel_used_kg")),
        "max_damage_j_estimate": _maximum(
            _number(_mapping(observation.get("mission")).get("damage"))
            for observation in observations
        ),
        "first_damage_after_s": first_damage_after,
        "first_damage_by_s": first_damage_by,
        "first_damage_phase": first_damage_phase,
        "min_observed_radar_range_m": _minimum(radar_ranges),
        "max_radar_age_s": _maximum(radar_ages),
        "future_radar_rows": sum(age < -1.0e-12 for age in radar_ages),
        "median_policy_wall_s": _median(policy_times),
        "max_policy_wall_s": _maximum(policy_times),
        "policy_calls_over_step_budget": sum(
            value is not None and value > step_s for value in policy_times
        )
        if step_s is not None
        else None,
    }


def _wilson(successes: int, total: int) -> tuple[float, float]:
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
    return max(0.0, center - half / denominator), min(1.0, center + half / denominator)


def summarize_runs(runs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group one sweep by algorithm/count; never pool errors with physical failures.

    ``success_rate`` includes every attempted run. ``physical_success_rate``
    excludes policy/engine errors and interruptions, and is null when no physical
    result exists. Accepts raw runs or previously computed ``analyze_run`` rows.
    Wilson intervals require unique seeds. Repeats do not add independent draws,
    so their bounds are omitted. Call separately for different scenarios/schedules.
    """
    groups = defaultdict(list)
    for run in runs:
        summary = run if "sample_count" in run and "samples" not in run else analyze_run(run)
        groups[(summary["algorithm"], summary["debris_count"])].append(summary)
    result = []
    for (algorithm, count), rows in sorted(groups.items()):
        outcomes = Counter(row["outcome"] for row in rows)
        successes = outcomes["success"]
        physical = sum(outcomes[name] for name in ("success", "failed_damage", "timeout"))
        physical_rows = [
            row for row in rows if row["outcome"] in ("success", "failed_damage", "timeout")
        ]
        repeated_seeds = len({row["seed"] for row in rows}) != len(rows)
        low, high = (None, None) if repeated_seeds else _wilson(successes, len(rows))
        result.append(
            {
                "algorithm": algorithm,
                "debris_count": count,
                "runs": len(rows),
                "successes": successes,
                "success_rate": successes / len(rows),
                "success_rate_wilson95_low": low,
                "success_rate_wilson95_high": high,
                "success_rate_wilson95_unavailable_reason": (
                    "Repeated seeds are not independent draws; no Wilson interval is reported."
                    if repeated_seeds
                    else None
                ),
                "physical_runs": physical,
                "physical_success_rate": successes / physical if physical else None,
                "outcomes": dict(sorted(outcomes.items())),
                "median_success_time_s": _median(
                    row["elapsed_sim_s"] for row in rows if row["outcome"] == "success"
                ),
                "median_damage_failure_time_s": _median(
                    row["elapsed_sim_s"] for row in rows if row["outcome"] == "failed_damage"
                ),
                "runs_with_observed_damage": sum(
                    row["max_damage_j_estimate"] is not None and row["max_damage_j_estimate"] > 0
                    for row in rows
                ),
                "median_first_damage_by_s": _median(row["first_damage_by_s"] for row in rows),
                "median_final_target_range_m": _median(
                    row["final_target_range_m"] for row in physical_rows
                ),
                "median_final_relative_speed_mps": _median(
                    row["final_relative_speed_mps"] for row in physical_rows
                ),
                "median_fuel_used_kg_estimate": _median(
                    row["fuel_used_kg_estimate"] for row in physical_rows
                ),
                "first_damage_phase_counts": dict(
                    sorted(
                        Counter(
                            row["first_damage_phase"]
                            for row in rows
                            if row["first_damage_phase"] is not None
                        ).items()
                    )
                ),
                "final_phase_counts": dict(
                    sorted(
                        Counter(
                            row["final_phase"] for row in rows if row["final_phase"] is not None
                        ).items()
                    )
                ),
            }
        )
    return result


_TRACE_METRICS = (
    ("target_distance_m", "Target range (m)"),
    ("relative_speed_mps", "Relative speed (m/s)"),
    ("fuel_used_kg_estimate", "Local fuel used (kg)"),
    ("requested_accel_mps2", "Command norm (m/s²)"),
    ("command_along_mps2", "Along-route command (m/s²)"),
    ("nearest_radar_range_m", "Observed radar range (m)"),
    ("oldest_radar_age_s", "Oldest radar row age (s)"),
    ("youngest_radar_age_s", "Youngest radar row age (s)"),
    ("damage_j_estimate", "Local damage (J)"),
    ("terrain_center_clearance_m", "Terrain centre clearance (m)"),
)


def _vector(value: Any) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    result = tuple(_number(component) for component in value)
    return result if all(component is not None for component in result) else None


def _route_frame(observation: Mapping) -> dict | None:
    """A fixed plotting frame, not a planned or certified flight corridor."""
    origin = _vector(_mapping(observation.get("shuttle")).get("position"))
    target = _vector(_mapping(observation.get("target")).get("position"))
    if origin is None or target is None:
        return None
    try:
        length, basis = route_basis(origin, target)
    except ValueError:
        return None
    return {
        "origin": origin,
        "along": basis["along_axis"],
        "lateral": basis["lateral_axis"],
        "normal": basis["normal_axis"],
        "length_m": length,
        "frame": observation.get("frame"),
    }


def _project(position: Any, frame: dict | None) -> dict:
    position = _vector(position)
    if position is None or frame is None:
        return {f"{axis}_m": None for axis in ("along", "lateral", "normal")}
    offset = tuple(a - b for a, b in zip(position, frame["origin"], strict=True))
    return {
        f"{axis}_m": _number(sum(a * b for a, b in zip(offset, frame[axis], strict=True)))
        for axis in ("along", "lateral", "normal")
    }


def _pairing_key(metadata: Mapping) -> dict | None:
    """Require actual trial receipts; shared missing fields are not a match.

    Algorithm settings intentionally differ across policies. They are reported
    per series, while the environment, schedule, build and repetition must match.
    """
    required = (
        "seed",
        "debris_count",
        "repetition",
        "preset",
        "settings",
        "step_s",
        "duration_s",
        "execution_kind",
    )
    if (
        any(metadata.get(key) is None for key in required)
        or type(metadata.get("repetition")) is not int
        or metadata["repetition"] < 1
    ):
        return None
    engine = _mapping(metadata.get("engine"))
    build = _mapping(engine.get("engine_build"))
    if not build.get("sha256") or not isinstance(metadata.get("settings"), Mapping):
        return None
    return {
        **{key: metadata[key] for key in required},
        "engine_build": dict(build),
        "engine_hashes": {key: value for key, value in engine.items() if key.endswith("sha256")},
    }


def _report_run_rows(directory: Path, archived: list[dict]) -> list[dict]:
    """Re-read each saved trace independently; never repair the source CSV in place.

    Some archived sweeps wrote metadata-only runs.csv rows before their separate
    trace summaries. Blank finals and a zero sample_count there are not measured
    zeros. analyze_run only copies recorded score receipts, never STP re-scoring.
    """
    from .sweep import load_trial

    rows = []
    for original in archived:
        row = dict(original)
        path = (directory / row.get("trial_directory", "")).resolve()
        if (
            path.parent != directory
            or not (path / "run.json").is_file()
            or not (path / "trace.jsonl").is_file()
        ):
            row["report_metric_source"] = "archived CSV; saved trace unavailable"
            row["archived_table_incomplete"] = False
            rows.append(row)
            continue
        loaded = load_trial(path)
        if not loaded.get("samples"):
            row["report_metric_source"] = "archived CSV; saved trace empty"
            row["archived_table_incomplete"] = False
            rows.append(row)
            continue
        # Very old run.json files can omit identity fields that their CSV kept.
        # This fallback does not supply missing physical readings or pair receipts.
        saved = {
            key: row.get(key)
            for key in ("algorithm", "seed", "debris_count", "outcome", "repetition", "step_s")
        }
        saved.update(loaded)
        derived = analyze_run(saved)
        incomplete = row.get("sample_count") != derived["sample_count"] or any(
            row.get(key) != derived[key]
            for key in ("final_target_range_m", "final_relative_speed_mps", "elapsed_sim_s")
            if derived[key] is not None
        )
        rows.append(
            {
                **row,
                **derived,
                "trial_directory": original["trial_directory"],
                "report_metric_source": "analyze_run(load_trial(saved trace))",
                "archived_sample_count": original.get("sample_count"),
                "archived_final_score": original.get("final_score"),
                "archived_table_incomplete": incomplete,
            }
        )
    return rows


def _report_series(directory: Path, runs: list[dict], trial: str | None):
    candidates = []
    for row in runs:
        name = row.get("trial_directory", "")
        path = (directory / name).resolve()
        if not name or path.parent != directory or not (path / "trace.jsonl").is_file():
            continue
        with (path / "trace.jsonl").open() as stream:
            if not any(line.strip() for line in stream):
                continue
        metadata_path = path / "run.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
        candidates.append({"row": row, "metadata": _mapping(metadata), "path": path})
    selected = next(
        (
            item
            for item in candidates
            if trial is not None and item["row"].get("trial_directory") == trial
        ),
        None,
    )
    if trial is None:
        selected = next((item for item in candidates if item["row"]["outcome"] != "success"), None)
    if selected is None:
        if trial is not None:
            raise ValueError("Selected trial has no saved observations")
        return None, [], []
    selected_key = _pairing_key(selected["metadata"])
    matched, excluded = [], []
    for item in candidates:
        row, metadata = item["row"], item["metadata"]
        if item is not selected and (row["seed"], row["debris_count"]) != (
            selected["row"]["seed"],
            selected["row"]["debris_count"],
        ):
            continue
        if item is not selected and (
            selected_key is None or _pairing_key(metadata) != selected_key
        ):
            excluded.append(row["trial_directory"])
            continue
        item["samples"] = [
            json.loads(line)
            for line in (item["path"] / "trace.jsonl").read_text().splitlines()
            if line.strip()
        ]
        matched.append(item)
    return selected, matched, excluded


def _trace_series(item: dict, frame: dict | None) -> tuple[list[dict], dict]:
    metadata, summary, samples = item["metadata"], item["row"], item["samples"]
    algorithm = metadata.get("algorithm", summary["algorithm"])
    repetition = metadata.get("repetition", summary.get("repetition"))
    series_id = summary["trial_directory"]
    label = f"{algorithm} / rep {repetition if repetition is not None else 'unknown'} / {series_id}"
    rows = []
    previous = {}
    previous_damage = previous_damage_row = None
    first_damage = None
    for index, sample in enumerate(samples):
        observation = _mapping(sample.get("observation"))
        mission, ship = (_mapping(observation.get(key)) for key in ("mission", "shuttle"))
        now, before = _number(observation.get("time_s")), _number(previous.get("time_s"))
        ranges, ages = _radar(observation)
        command_start = before if now is not None and before is not None and now > before else None
        same_frame = frame is not None and observation.get("frame") == frame["frame"]
        command_along = None
        if (
            command_start is not None
            and frame is not None
            and previous.get("frame") == frame["frame"]
        ):
            from .control_contract import body_to_reference

            try:
                command_world = body_to_reference(
                    sample.get("accel_command"),
                    _mapping(previous.get("shuttle")).get("rotation"),
                    previous.get("rotation_convention"),
                )
                command_along = _number(
                    float(sum(a * b for a, b in zip(command_world, frame["along"], strict=True)))
                )
            except (TypeError, ValueError, OverflowError):
                command_along = None
        row = {
            "algorithm": algorithm,
            "series_id": series_id,
            "series_label": label,
            "repetition": repetition,
            "sample_index": index,
            "time_s": now,
            "phase": mission.get("phase"),
            "outcome": summary["outcome"],
            "target_distance_m": _target_metric(observation, "target_distance_m"),
            "relative_speed_mps": _target_metric(observation, "relative_speed_mps"),
            "fuel_used_kg_estimate": _number(mission.get("fuel_used_kg")),
            "damage_j_estimate": _number(mission.get("damage")),
            "terrain_center_clearance_m": _clearance(observation),
            "nearest_radar_range_m": _minimum(ranges),
            "oldest_radar_age_s": _maximum(ages),
            "youngest_radar_age_s": _minimum(ages),
            "future_radar_rows": sum(age < -1.0e-12 for age in ages),
            "requested_accel_mps2": _distance(sample.get("accel_command")),
            "command_body_mps2": str(sample.get("accel_command")),
            "command_along_mps2": command_along,
            "command_start_s": command_start,
            "command_end_s": now if command_start is not None else None,
            "terminal": index == len(samples) - 1,
            "damage_bound": None,
            **_project(ship.get("position"), frame if same_frame else None),
        }
        damage = row["damage_j_estimate"]
        if first_damage is None and damage is not None and damage > 0:
            row["damage_bound"] = "by"
            after = previous_damage_row if previous_damage == 0 else None
            if after is not None:
                after["damage_bound"] = "after"
            first_damage = {
                "algorithm": algorithm,
                "series_id": series_id,
                "series_label": label,
                "first_damage_after_s": after["time_s"] if after is not None else None,
                "first_damage_by_s": now,
                "first_damage_phase": mission.get("phase"),
            }
        if damage is not None:
            previous_damage, previous_damage_row = damage, row
        rows.append(row)
        previous = observation
    # Explicit segments guard against Vega filtering nulls before drawing lines.
    # Keep the null rows in the dataset and the tables, not just in a caption.
    metrics = [metric for metric, _ in _TRACE_METRICS] + ["lateral_m", "normal_m"]
    for metric in metrics:
        segment, last_time = -1, None
        for row in rows:
            valid = row["time_s"] is not None and row[metric] is not None
            if metric in ("lateral_m", "normal_m"):
                valid = valid and row["along_m"] is not None
            if not valid:
                row[f"{metric}_segment"] = None
                last_time = None
                continue
            if last_time is None or row["time_s"] <= last_time:
                segment += 1
            row[f"{metric}_segment"] = f"{series_id}:{metric}:{segment}"
            last_time = row["time_s"]
    event = first_damage or {
        "algorithm": algorithm,
        "series_id": series_id,
        "series_label": label,
        "first_damage_after_s": None,
        "first_damage_by_s": None,
        "first_damage_phase": None,
    }
    event.update(
        {
            "terminal_time_s": rows[-1]["time_s"],
            "outcome": summary["outcome"],
            "terminal_range_m": rows[-1]["target_distance_m"],
            "terminal_speed_mps": rows[-1]["relative_speed_mps"],
        }
    )
    return rows, event


def _report_table(rows: Iterable[Mapping], columns: tuple[str, ...], caption: str) -> str:
    def cell(value):
        return html.escape("missing" if value is None else str(value))

    return (
        '<div class="table-scroll" tabindex="0" role="region" aria-label="'
        + html.escape(caption, quote=True)
        + '"><table><caption>'
        + html.escape(caption)
        + "</caption><thead><tr>"
        + "".join(f'<th scope="col">{html.escape(key)}</th>' for key in columns)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{cell(row.get(key))}</td>" for key in columns) + "</tr>"
            for row in rows
        )
        + "</tbody></table></div>"
    )


def write_report(
    directory: str | Path, *, trial: str | None = None, report_file: str | Path | None = None
) -> Path:
    """Write saved evidence, never run a simulator or recompute recorded scores.

    The default detail remains the first non-success with observations. Only
    matching environment/schedule/build receipts and the same explicit repetition
    join its comparison. Each trial keeps its own line detail and missing-value
    segments. Altair is the existing optional ``lab`` dependency.
    """
    import altair as alt

    directory = Path(directory).resolve()
    with (directory / "runs.csv").open(newline="") as stream:
        runs = list(csv.DictReader(stream))
    groups = json.loads((directory / "summary.json").read_text())
    numeric = (
        "seed",
        "repetition",
        "debris_count",
        "sample_count",
        "elapsed_sim_s",
        "first_damage_after_s",
        "first_damage_by_s",
        "final_target_range_m",
        "final_relative_speed_mps",
        "final_score",
        "score_progress_points",
        "score_collision_penalty_points",
        "score_fuel_penalty_points",
        "score_offset_points",
    )
    for row in runs:
        for key in numeric:
            value = row.get(key)
            try:
                row[key] = _number(float(value)) if value else None
            except ValueError:
                row[key] = None
    runs = _report_run_rows(directory, runs)
    selected, series, excluded = _report_series(directory, runs, trial)
    frame = None
    if selected is not None:
        frame = _route_frame(_mapping(selected["samples"][0].get("observation")))
    trace_rows, events = [], []
    evidence = []
    for item in series:
        rows, event = _trace_series(item, frame)
        trace_rows.extend(rows)
        events.append(event)
        metadata = item["metadata"]
        evidence.append(
            {
                "series_label": rows[0]["series_label"],
                "algorithm_settings": metadata.get("algorithm_settings"),
                "preset": metadata.get("preset"),
                "step_s": metadata.get("step_s"),
                "duration_s": metadata.get("duration_s"),
                "engine_build": _mapping(metadata.get("engine")).get("engine_build"),
                "settings": metadata.get("settings"),
            }
        )
    algorithms = sorted({row["algorithm"] for row in [*runs, *groups, *trace_rows]})
    palette = ("#0072b2", "#b34e00", "#007b5e", "#96477e", "#805f00", "#3b5ca8", "#555555")
    shapes = (
        "circle",
        "square",
        "triangle-up",
        "diamond",
        "cross",
        "triangle-down",
        "triangle-right",
    )
    dashes = ([1, 0], [7, 3], [2, 3], [9, 3, 2, 3], [5, 2], [1, 3], [12, 4])
    # Darker versions of the hosted view's identity colours work on white.
    # Explicit patterns and symbols keep identity available without colour.
    identities = {
        "official_basic_beeline": ("#555555", "circle", [3, 4]),
        "ali_official_profile": ("#805f00", "diamond", [1, 0]),
        "wso_official_profile": ("#805f00", "diamond", [1, 0]),
        "baseline": ("#765298", "square", [12, 4]),
        "body-axis": ("#765298", "triangle-up", [12, 3, 2, 3]),
        "basic_pd_radar": ("#b34e00", "cross", [7, 3]),
        "bui_alpha_beta_mvp": ("#007b5e", "triangle-down", [2, 3]),
        "tom_kalman_velocity_mvp": ("#3b5ca8", "triangle-right", [9, 3, 2, 3]),
    }

    def category(channel):
        values = {"color": palette, "shape": shapes, "strokeDash": dashes}[channel]
        component = {"color": 0, "shape": 1, "strokeDash": 2}[channel]
        return {
            "field": "algorithm",
            "type": "nominal",
            "scale": {
                "domain": algorithms,
                "range": [
                    identities[algorithm][component]
                    if algorithm in identities
                    else values[index % len(values)]
                    for index, algorithm in enumerate(algorithms)
                ],
            },
            "legend": {"title": "Policy", "orient": "bottom", "columns": 1, "labelLimit": 160},
        }

    def field(name, title=None, *, quantitative=True):
        result = {"field": name, "type": "quantitative" if quantitative else "nominal"}
        if title is not None:
            result["title"] = title
        return result

    def tips(*names):
        quantitative = {
            *numeric,
            *dict(_TRACE_METRICS),
            "time_s",
            "command_start_s",
            "command_end_s",
            "sample_index",
            "runs",
            "success_rate",
            "success_rate_wilson95_low",
            "success_rate_wilson95_high",
        }
        return [field(name, quantitative=name in quantitative) for name in names]

    def tip(encoding):
        return {key: value for key, value in encoding.items() if key in ("field", "type", "title")}

    charts, route_indices = [], []
    common = {"color": category("color"), "shape": category("shape")}
    run_tips = tips("algorithm", "trial_directory", "repetition", "seed", "outcome", "error")
    if groups:
        encoding = {
            "x": field("debris_count", "Debris count"),
            "y": {**field("success_rate", "Attempt success fraction"), "scale": {"domain": [0, 1]}},
            "color": category("color"),
        }
        charts.append(
            {
                "title": "Success across sampled seeds",
                "height": 220,
                "description": (
                    "Descriptive fractions. Wilson bounds only for unique seeds; "
                    "full denominators are in the tables."
                ),
                "data": {"name": "groups"},
                "layer": [
                    {
                        "mark": {"type": "line", "invalid": "break-paths-show-domains"},
                        "encoding": {
                            **encoding,
                            "strokeDash": category("strokeDash"),
                            "detail": field("algorithm", quantitative=False),
                        },
                    },
                    {
                        "mark": {"type": "point", "size": 65},
                        "encoding": {
                            **encoding,
                            "shape": category("shape"),
                            "tooltip": tips(
                                "algorithm", "debris_count", "runs", "success_rate", "outcomes"
                            ),
                        },
                    },
                    {
                        "mark": "rule",
                        "encoding": {
                            "x": field("debris_count"),
                            "y": field("success_rate_wilson95_low"),
                            "y2": {"field": "success_rate_wilson95_high"},
                            "color": category("color"),
                        },
                    },
                ],
            }
        )
    if any(row.get("final_score") is not None for row in runs):
        charts.append(
            {
                "title": "Legacy local score receipts",
                "height": 200,
                "data": {"name": "runs"},
                "mark": {"type": "point", "size": 65},
                "encoding": {
                    **common,
                    "x": field("debris_count", "Debris count"),
                    "y": field("final_score", "Recorded local score, not STP points"),
                    "tooltip": run_tips
                    + tips(
                        "final_score",
                        "score_formula",
                        "score_progress_points",
                        "score_collision_penalty_points",
                        "score_fuel_penalty_points",
                        "score_offset_points",
                    ),
                },
            }
        )
    if runs:
        charts.append(
            {
                "title": "Where each run ended",
                "height": 200,
                "data": {"name": "runs"},
                "mark": {"type": "point", "size": 65},
                "encoding": {
                    **common,
                    "x": field("debris_count", "Debris count"),
                    "y": field("final_target_range_m", "Final sampled range (m)"),
                    "tooltip": run_tips + tips("final_target_range_m", "final_relative_speed_mps"),
                },
            }
        )
    if any(row.get("first_damage_by_s") is not None for row in runs):
        damage_encoding = {
            "x": field("debris_count", "Debris count"),
            "color": category("color"),
            "tooltip": run_tips
            + tips("first_damage_after_s", "first_damage_by_s", "first_damage_phase"),
        }
        charts.append(
            {
                "title": "First observed damage intervals",
                "height": 200,
                "data": {"name": "runs"},
                "layer": [
                    {
                        "mark": "rule",
                        "encoding": {
                            **damage_encoding,
                            "y": field("first_damage_after_s", "First damage bounds (s)"),
                            "y2": {"field": "first_damage_by_s"},
                            "strokeDash": category("strokeDash"),
                        },
                    },
                    {
                        "mark": {"type": "point", "size": 65},
                        "encoding": {
                            **damage_encoding,
                            "y": field("first_damage_by_s", "First damage bounds (s)"),
                            "shape": category("shape"),
                        },
                    },
                ],
            }
        )
    time_values = [row["time_s"] for row in trace_rows if row["time_s"] is not None]
    minimum_time, maximum_time = (min(time_values), max(time_values)) if time_values else (0.0, 0.0)
    time_domain = [minimum_time, maximum_time if maximum_time > minimum_time else minimum_time + 1]
    trace_tips = tips(
        "series_label",
        "sample_index",
        "time_s",
        "phase",
        "outcome",
        "command_start_s",
        "command_end_s",
    )
    nearest = [
        {"filter": "isValid(datum.time_s)"},
        {"calculate": "abs(datum.time_s - cursor_time)", "as": "cursor_delta_s"},
        {
            "window": [{"op": "row_number", "as": "cursor_rank"}],
            "groupby": ["series_id"],
            "sort": [
                {"field": "cursor_delta_s", "order": "ascending"},
                {"field": "sample_index", "order": "ascending"},
            ],
        },
        {"filter": "datum.cursor_rank === 1"},
    ]

    def sample_layers(x, y):
        encoding = {"x": x, "y": y, "color": category("color"), "tooltip": trace_tips + [tip(y)]}
        layers = [
            {
                "mark": {"type": "point", "size": 12, "opacity": 0.35},
                "encoding": {**encoding, "shape": category("shape")},
            },
            {
                "transform": [{"filter": "datum.damage_bound != null"}],
                "mark": {"type": "point", "shape": "cross", "size": 110, "strokeWidth": 2},
                "encoding": {**encoding, "tooltip": trace_tips + tips("damage_bound") + [tip(y)]},
            },
            {
                "transform": [{"filter": "datum.terminal"}],
                "mark": {"type": "point", "filled": False, "size": 170, "strokeWidth": 2},
                "encoding": {**encoding, "shape": category("shape")},
            },
            {
                "transform": nearest,
                "mark": {
                    "type": "point",
                    "filled": True,
                    "size": 90,
                    "stroke": "#111111",
                    "strokeWidth": 1,
                },
                "encoding": {**encoding, "shape": category("shape")},
            },
        ]
        if y["field"] in ("requested_accel_mps2", "command_along_mps2"):
            for layer in layers:
                layer["transform"] = [
                    *layer.get("transform", []),
                    {"filter": "isValid(datum.command_start_s) && isValid(datum.command_end_s)"},
                ]
        return layers

    def detail(metric):
        return [
            field("series_id", quantitative=False),
            field("repetition", quantitative=False),
            field(f"{metric}_segment", quantitative=False),
        ]

    if trace_rows and frame is not None:
        along_values = [
            0.0,
            frame["length_m"],
            *(row["along_m"] for row in trace_rows if row["along_m"] is not None),
        ]
        cross_values = [
            0.0,
            *(
                row[axis]
                for row in trace_rows
                for axis in ("lateral_m", "normal_m")
                if row[axis] is not None
            ),
        ]
        span = (
            max(max(along_values) - min(along_values), max(cross_values) - min(cross_values), 1.0)
            * 1.1
        )
        along_mid = (max(along_values) + min(along_values)) / 2
        across_mid = (max(cross_values) + min(cross_values)) / 2
        for axis, title in (
            ("lateral_m", "Lateral displacement (m)"),
            ("normal_m", "Normal displacement (m)"),
        ):
            x = {
                **field("along_m", "Along route (m)"),
                "scale": {
                    "domain": [along_mid - span / 2, along_mid + span / 2],
                    "nice": False,
                    "zero": False,
                },
            }
            y = {
                **field(axis, title),
                "scale": {
                    "domain": [across_mid - span / 2, across_mid + span / 2],
                    "nice": False,
                    "zero": False,
                },
            }
            route_indices.append(len(charts))
            charts.append(
                {
                    "title": "Ownship along / " + axis.removesuffix("_m"),
                    "height": 650,
                    "description": (
                        "Actual saved ownship positions in a shared fixed frame, "
                        "equal metres per pixel. The dashed grey segment is a direct "
                        "reference, not a flown policy or collision replay."
                    ),
                    "data": {"name": "trace"},
                    "layer": [
                        {
                            "data": {
                                "values": [
                                    {"along_m": 0, axis: 0},
                                    {"along_m": frame["length_m"], axis: 0},
                                ]
                            },
                            "mark": {"type": "line", "color": "#666666", "strokeDash": [4, 4]},
                            "encoding": {"x": x, "y": y},
                        },
                        {
                            "mark": {"type": "line", "invalid": "break-paths-show-domains"},
                            "encoding": {
                                "x": x,
                                "y": y,
                                "color": category("color"),
                                "strokeDash": category("strokeDash"),
                                "detail": detail(axis),
                                "order": field("sample_index"),
                                "tooltip": trace_tips + [tip(x), tip(y)],
                            },
                        },
                        *sample_layers(x, y),
                    ],
                }
            )
    if trace_rows:
        for metric, title in _TRACE_METRICS:
            x = {
                **field("time_s", "Simulation time (s)"),
                "scale": {"domain": time_domain, "nice": False},
            }
            y = {**field(metric, title), "scale": {"zero": False}}
            encoding = {
                "x": x,
                "y": y,
                "color": category("color"),
                "strokeDash": category("strokeDash"),
                "detail": detail(metric),
                "order": field("sample_index"),
                "tooltip": trace_tips + [tip(y)],
            }
            interval_command = metric in ("requested_accel_mps2", "command_along_mps2")
            mark = (
                {"type": "rule", "strokeWidth": 2}
                if interval_command
                else {"type": "line", "invalid": "break-paths-show-domains"}
            )
            if interval_command:
                encoding["x"] = {**x, "field": "command_start_s"}
                encoding["x2"] = {"field": "command_end_s"}
            charts.append(
                {
                    "title": title,
                    "height": 165,
                    "data": {"name": "trace"},
                    "description": (
                        "One series per trial, never joined across repetitions or "
                        "missing readings. Crosses bracket first observed damage; outlined "
                        "policy symbols mark final saved observations. Commands apply to "
                        "the preceding interval."
                    ),
                    "layer": [
                        {
                            "data": {"name": "events"},
                            "transform": [
                                {
                                    "filter": (
                                        "isValid(datum.first_damage_after_s) && "
                                        "isValid(datum.first_damage_by_s)"
                                    )
                                }
                            ],
                            "mark": {"type": "rect", "opacity": 0.08},
                            "encoding": {
                                "x": {**x, "field": "first_damage_after_s"},
                                "x2": {"field": "first_damage_by_s"},
                                "color": category("color"),
                                "tooltip": tips(
                                    "series_label", "first_damage_after_s", "first_damage_by_s"
                                ),
                            },
                        },
                        {"mark": mark, "encoding": encoding},
                        *sample_layers(x, y),
                        {
                            "data": {"values": [{}]},
                            "mark": {"type": "rule", "color": "#111111", "strokeDash": [2, 2]},
                            "encoding": {
                                "x": {
                                    "datum": {"expr": "cursor_time"},
                                    "type": "quantitative",
                                    "scale": {"domain": time_domain},
                                }
                            },
                        },
                    ],
                }
            )
    chart_html = "<p>No measurements available to chart. Inspect runs.csv and the saved trials.</p>"
    if charts:
        specification = {
            "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
            "vconcat": [{**chart, "width": 650} for chart in charts],
            "datasets": {"groups": groups, "runs": runs, "trace": trace_rows, "events": events},
            "params": [{"name": "cursor_time", "value": minimum_time}],
            "resolve": {"scale": {"color": "shared", "shape": "shared", "strokeDash": "shared"}},
            "config": {
                "font": "system-ui",
                "axis": {
                    "labelFontSize": 12,
                    "titleFontSize": 12,
                    "titleLimit": 180,
                    "labelLimit": 90,
                },
                "title": {"anchor": "start", "fontSize": 15, "limit": 180},
                "view": {"stroke": None},
            },
            "usermeta": {
                "route_chart_indices": route_indices,
                "selected_trial": selected["row"]["trial_directory"] if selected else None,
                "route_frame": frame,
                "excluded_trials": excluded,
            },
        }
        # Validate through Altair, without pandas or the default row-limit transformer.
        specification = alt.VConcatChart.from_dict(specification).to_dict()
        encoded = json.dumps(specification, allow_nan=False).replace("<", "\\u003c")
        chart_html = (
            '<div id="charts" aria-describedby="comparison-note"></div>'
            '<script id="report-spec" type="application/json">' + encoded + "</script>"
        )
    detail_html = (
        "<p>No non-success with saved observations was selected. Use --analyze-only "
        "--trial TRIAL_DIRECTORY to inspect a saved success.</p>"
    )
    if selected is not None:
        row = selected["row"]
        repetition = selected["metadata"].get("repetition", row.get("repetition"))
        detail_html = (
            f"<h2>Paired trace: seed {row['seed']:g}, count {row['debris_count']:g}</h2>"
            + f"<p>Chosen trial <code>{html.escape(row['trial_directory'])}</code>, "
            + f"repetition {html.escape(str(repetition))}. "
            + "Only the same seed, count, explicit repetition, scenario settings, command "
            + "schedule and build receipts are compared. Each saved trial has a separate "
            + "series ID. "
            + "Other repetitions are not overlaid or averaged; select their trial explicitly. "
            + f"{len(excluded)} other same-seed/count trials were excluded for a different "
            + "repetition, unmatched or missing receipts.</p>"
            + (
                "<p>The selected trial lacks complete pairing receipts. It is shown alone, "
                "not as a verified matched comparison.</p>"
                if _pairing_key(selected["metadata"]) is None
                else ""
            )
            + "<p>These sampled lines are not collision replay. They join saved ownship "
            + "positions, not hidden debris or a reconstructed world. Missing readings and "
            + "non-increasing clocks break lines. Crosses mark the known first-damage "
            + "bounds; pale time bands cover the interval. First damage brackets saved "
            + "observations, not an exact contact time or a proven cause. "
            + "An unknown lower bound stays missing. Large outlined policy "
            + "symbols mark terminal endpoints, which may be timeouts or software errors rather "
            + "than arrivals.</p>"
            + "<p>Stored commands accompany post-step state for the PRECEDING interval. "
            + "Command charts draw that interval, not a command starting at the saved post-step "
            + "timestamp. The signed along-route command uses the preceding observation's "
            + "declared "
            + "attitude and frame; missing attitude or convention stays missing. It is requested "
            + "thrust, not net acceleration.</p>"
            + (
                "<p>The shared route frame starts at the chosen trial's initial ownship "
                "position, points along its initial target direction, and stays fixed. "
                "Lateral projects the least-parallel Cartesian axis perpendicular to along; "
                "normal is along cross lateral. Both plots use equal spatial scales. The "
                "grey dashed direct reference is not a policy, obstacle-free corridor, "
                "or a path the controller was told to "
                "follow. The target may move after this initial reference.</p>"
                if frame
                else "<p>Route projections are unavailable because the initial ownship or target "
                "position is missing or degenerate. No route is invented from later samples.</p>"
            )
            + _report_table(
                evidence,
                (
                    "series_label",
                    "algorithm_settings",
                    "preset",
                    "step_s",
                    "duration_s",
                    "engine_build",
                    "settings",
                ),
                "Detailed series and pairing receipts. Wide tables scroll horizontally.",
            )
            + _report_table(
                events,
                (
                    "series_label",
                    "first_damage_after_s",
                    "first_damage_by_s",
                    "first_damage_phase",
                    "terminal_time_s",
                    "outcome",
                    "terminal_range_m",
                    "terminal_speed_mps",
                ),
                "First damage bounds and terminal endpoints",
            )
            + '<fieldset><legend>Shared time cursor</legend><p id="cursor-help">'
            + "Use either keyboard-accessible control. All time charts share this time; path "
            + "symbols and the table show the nearest actual sample per trial, "
            + "never interpolation. "
            + "Samples can be earlier or later, or outside a shorter trial's saved span.</p>"
            + '<label for="cursor-time">Move time cursor (s)</label>'
            + f'<input disabled id="cursor-time" type="range" min="{minimum_time}" '
            + f'max="{maximum_time}" step="any" value="{minimum_time}" '
            + 'aria-describedby="cursor-help">'
            + '<label for="cursor-number">Set exact cursor time (s)</label>'
            + f'<input disabled id="cursor-number" type="number" min="{minimum_time}" '
            + f'max="{maximum_time}" step="any" value="{minimum_time}" '
            + 'aria-describedby="cursor-help">'
            + '<p id="cursor-status" role="status">The full sample tables below remain '
            + "available without chart scripts.</p></fieldset>"
            + '<div class="table-scroll" tabindex="0" role="region" '
            + 'aria-label="Nearest samples at shared cursor, scroll horizontally"><table>'
            + "<caption>Nearest saved samples at the shared cursor. "
            + "Missing values are not zero."
            + "</caption><thead><tr>"
            + "".join(
                f'<th scope="col">{html.escape(key)}</th>'
                for key in (
                    "series_label",
                    "time_s",
                    "cursor_delta_s",
                    "saved_span",
                    "target_distance_m",
                    "relative_speed_mps",
                    "fuel_used_kg_estimate",
                    "requested_accel_mps2",
                    "nearest_radar_range_m",
                    "oldest_radar_age_s",
                    "damage_j_estimate",
                )
            )
            + '</tr></thead><tbody id="cursor-rows"></tbody></table></div>'
        )
    tables = _report_table(
        groups,
        (
            "algorithm",
            "debris_count",
            "runs",
            "successes",
            "success_rate",
            "physical_success_rate",
            "outcomes",
        ),
        "Sweep denominators and outcomes. Wide tables scroll horizontally.",
    )
    tables += (
        "<details><summary>Read every run result, including repetition and legacy local score"
        "</summary>"
        + _report_table(
            runs,
            (
                "trial_directory",
                "algorithm",
                "repetition",
                "seed",
                "debris_count",
                "outcome",
                "error",
                "sample_count",
                "archived_sample_count",
                "report_metric_source",
                "archived_table_incomplete",
                "final_target_range_m",
                "final_relative_speed_mps",
                "elapsed_sim_s",
                "fuel_used_kg_estimate",
                "max_damage_j_estimate",
                "first_damage_after_s",
                "first_damage_by_s",
                "final_score",
                "archived_final_score",
                "score_formula",
            ),
            "Saved run results, not official STP points",
        )
        + "</details>"
    )
    sample_tables = ""
    for item in series:
        identity = item["row"]["trial_directory"]
        rows = [row for row in trace_rows if row["series_id"] == identity]
        sample_tables += (
            "<details><summary>Read "
            + html.escape(rows[0]["series_label"])
            + f" ({len(rows)} saved samples)</summary>"
            + _report_table(
                rows,
                (
                    "sample_index",
                    "time_s",
                    "phase",
                    "along_m",
                    "lateral_m",
                    "normal_m",
                    *(metric for metric, _ in _TRACE_METRICS),
                    "command_start_s",
                    "command_end_s",
                    "command_body_mps2",
                    "future_radar_rows",
                    "damage_bound",
                    "terminal",
                ),
                "Saved and derived sample values. Missing means unavailable; scroll horizontally.",
            )
            + "</details>"
        )
    report = Path(report_file) if report_file is not None else directory / "report.html"
    if report_file is not None and (report.exists() or report.is_symlink()):
        raise FileExistsError("Choose a new report file; existing evidence is not overwritten")
    report.parent.mkdir(parents=True, exist_ok=True)
    content = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Flight sweep analysis</title>"
        '<script src="https://cdn.jsdelivr.net/npm/vega@6"></script>'
        '<script src="https://cdn.jsdelivr.net/npm/vega-lite@6"></script>'
        '<script src="https://cdn.jsdelivr.net/npm/vega-embed@7"></script>'
        + _REPORT_STYLE
        + "</head><body><main><h1>Seeded flight experiments</h1>"
        + _REPORT_INTRO
        + "<p>Per-trial metrics are re-derived with <code>analyze_run(load_trial(...))</code> "
        + "from actual saved observations wherever traces are available. Some archived "
        + "<code>runs.csv</code> tables contain metadata-only rows with sample_count=0 and "
        + "blank finals despite real traces and populated aggregate summaries. Those blanks "
        + "are not zero measurements. "
        + f"{sum(bool(row.get('archived_table_incomplete')) for row in runs)} archived rows "
        + "were incomplete or disagreed with their traces. The run table identifies the "
        + "metric source and retains the archived sample count and score separately. "
        + "Aggregate summaries remain the saved summaries, not a new aggregation. "
        + "No source CSV, summary or trace is overwritten; no STP points are inferred.</p>"
        + tables
        + detail_html
        + '<h2>Comparison charts</h2><p id="comparison-note">Policy colors, line dashes and '
        + "point shapes stay fixed across charts. Series labels and the tables identify trials "
        + "without relying on color. Charts load Vega from the internet; all data and tables "
        + "are embedded locally.</p>"
        + chart_html
        + _REPORT_GUIDE
        + "<h2>Accessible sample tables</h2><p>Expand a trial to read every saved sample and "
        + "derived metric. Wide tables can be scrolled with the keyboard when focused. "
        + "Source trial folders retain the full observations and commands.</p>"
        + sample_tables
        + _REPORT_SCRIPT
        + "</main></body></html>"
    )
    with report.open("x" if report_file is not None else "w", encoding="utf-8") as stream:
        stream.write(content)
    return report


_REPORT_STYLE = """<style>
body{font:1rem/1.55 system-ui;max-width:960px;margin:2rem auto;padding-inline:1rem;
color:#202020;background:#fff}
main{min-width:0}p,li{max-width:72ch}
h1,h2,h3{line-height:1.25;overflow-wrap:anywhere}
h1{font-size:2rem}h2{font-size:1.5rem}h3{font-size:1.125rem}
td,th{padding:.5rem;text-align:start;border-bottom:1px solid #ccc;overflow-wrap:anywhere}
table{border-collapse:collapse;font-variant-numeric:tabular-nums}
caption{text-align:start;font-weight:600;padding-block:.5rem}
.table-scroll{max-width:100%;overflow-x:auto;margin-block:1rem}
.table-scroll table{min-width:42rem}details{margin-block:1rem}
summary{cursor:pointer;padding-block:.5rem}code{overflow-wrap:anywhere}
fieldset{min-inline-size:0;margin-block:1.5rem;padding:1rem}label{display:block}
input{font:inherit;min-height:2rem;max-width:100%;box-sizing:border-box}
input[type=range]{width:100%}#charts{width:100%;min-width:0}
#charts .vega-embed{padding:0}#charts svg{max-width:100%;height:auto}
a{color:#005a8c}a,summary,input{touch-action:manipulation}
</style>"""

_REPORT_INTRO = (
    "<p>Success means entering the local engine's arrival radius. It does not impose an "
    "arrival-speed limit or establish a safe rendezvous. Inspect relative speed separately. "
    "These are practice-model results, not licensed competition validation.</p>"
    "<p>Legacy local scores are recorded diagnostics, not official STP points. Their local SI "
    "fuel and damage counters do not establish compatibility with STP counters, even when a "
    "formula ID refers to published algebra. Scores and saved terms are shown unchanged; "
    "missing historical scores or penalty terms are not reconstructed. "
    "This report does not re-score trials.</p>"
    "<p>Debris count is a density proxy only with fixed region, sizes and distribution. "
    "Seeds are paired across algorithms; changing count need not retain the same rocks. "
    "Finite physics steps and sampled observations can miss between-sample events.</p>"
    "<p>Chart success fractions include all attempts; physical success rates in the table "
    "exclude policy errors, engine errors and interruptions. Error outcomes need separate "
    "investigation. Wilson intervals describe sampled-seed variation, not model accuracy. "
    "Repeated attempts at the same seed are not independent seed draws, so their Wilson "
    "bounds are omitted. Repeated-seed success counts are descriptive, not evidence of "
    "robustness across additional seeds.</p>"
)

_REPORT_GUIDE = (
    "<h2>Reading the comparison</h2><ul>"
    "<li>Braking: compare falling range with relative speed and signed along-route commands. "
    "A high speed at a short range deserves inspection even after local success. A negative "
    "along-route command is not necessarily braking against the actual velocity; gravity, "
    "attitude and lateral motion also matter.</li>"
    "<li>Detours: route projections show actual lateral and normal departures from an initial "
    "direct reference. A longer-looking path may accompany avoidance or another goal. The "
    "reference is not the policy's planned route, and the plot alone cannot say why the "
    "path differs.</li>"
    "<li>Burn reversals and chatter: look for repeated sign changes in the along-route request "
    "and repeated command intervals, then read the stored body commands and preceding "
    "attitude. The norm alone cannot show direction. Saved sampling can hide faster switches; "
    "commands are not measured actuator output.</li>"
    "<li>Fuel and damage: compare both estimates alongside outcomes, elapsed time, range and "
    "speed. A low-fuel timeout and a higher-fuel arrival have different horizons. "
    "Correlated changes do not establish that a burn prevented or caused damage.</li>"
    "<li>Radar: range is the nearest observed row, not hull clearance or collision truth. "
    "The nearest row can change identity between samples. "
    "Youngest and oldest row ages expose mixed acquisition times. "
    "Empty or missing radar stays missing, not zero or proof of open space; negative ages "
    "remain visible and are counted as future rows.</li>"
    "</ul><p>The direct reference is geometry only. Actual controller implementations include "
    "<code>src/wsorb/flight/control.py</code> for basic PD/radar and coast, "
    "<code>src/wsorb/stp_policy.py</code> for checkpoint profiles, and the adapters under "
    "<code>src/wsorb/controllers/</code>. Sweep names can also refer to custom factories or "
    "<code>src/wsorb/controllers/official_profile.py</code>. Check the saved "
    "<code>policies.json</code> factory/module evidence, per-trial algorithm settings and "
    "<code>python.json</code> source snapshots rather than inferring policy logic from a curve. "
    "Current source may differ from a historical saved run.</p>"
    "<p>Physical distance, speed and fuel group medians exclude software-error runs; "
    "completion and timeout horizons still differ. New sweeps snapshot local package sources "
    "and the lockfile hash, but external/custom imported dependencies are not a complete "
    "environment snapshot. The original runs.csv, summary.json, run.json and trace.jsonl "
    "files remain unchanged.</p>"
)


_REPORT_SCRIPT = r"""<script>
(() => {
  const source = document.getElementById('report-spec');
  if (!source) return;
  const spec = JSON.parse(source.textContent);
  const container = document.getElementById('charts');
  const slider = document.getElementById('cursor-time');
  const number = document.getElementById('cursor-number');
  const status = document.getElementById('cursor-status');
  const rows = spec.datasets.trace || [];
  const series = new Map();
  for (const row of rows) {
    if (typeof row.time_s !== 'number') continue;
    if (!series.has(row.series_id)) series.set(row.series_id, []);
    series.get(row.series_id).push(row);
  }
  let view, width, generation = 0;
  let time = spec.params[0].value;
  function readout() {
    const body = document.getElementById('cursor-rows');
    if (!body) return;
    body.replaceChildren();
    for (const samples of series.values()) {
      let nearest = samples[0];
      let low = nearest.time_s, high = low;
      for (const row of samples) {
        if (Math.abs(row.time_s - time) < Math.abs(nearest.time_s - time)) nearest = row;
        low = Math.min(low, row.time_s); high = Math.max(high, row.time_s);
      }
      const tr = document.createElement('tr');
      const values = [nearest.series_label, nearest.time_s, Math.abs(nearest.time_s - time),
        time < low || time > high ? 'outside saved span' : 'within saved span',
        nearest.target_distance_m, nearest.relative_speed_mps, nearest.fuel_used_kg_estimate,
        nearest.requested_accel_mps2, nearest.nearest_radar_range_m,
        nearest.oldest_radar_age_s, nearest.damage_j_estimate];
      for (const value of values) {
        const td = document.createElement('td');
        td.textContent = value == null ? 'missing' : String(value);
        tr.append(td);
      }
      body.append(tr);
    }
    status.textContent = `Cursor ${time} s. Nearest saved samples only; no interpolation.`;
  }
  function update(input) {
    if (!input.validity.valid || input.value.trim() === '') return;
    time = Math.min(Number(slider.max), Math.max(Number(slider.min), Number(input.value)));
    slider.value = number.value = String(time);
    readout();
    if (view) view.signal('cursor_time', time).runAsync();
  }
  if (slider) {
    slider.disabled = number.disabled = false;
    slider.addEventListener('input', () => update(slider));
    number.addEventListener('input', () => update(number));
    readout();
  }
  function render() {
    const available = Math.max(120, Math.floor(container.clientWidth) - 100);
    if (width === available) return;
    width = available;
    const token = ++generation;
    const responsive = JSON.parse(JSON.stringify(spec));
    responsive.params[0].value = time;
    responsive.vconcat.forEach((chart, index) => {
      chart.width = width;
      if (responsive.usermeta.route_chart_indices.includes(index)) chart.height = width;
    });
    if (view) { view.finalize(); view = null; }
    if (typeof vegaEmbed !== 'function') {
      if (status) status.textContent =
        'Charts could not load. Use the embedded tables or reopen with internet to load Vega.';
      return;
    }
    vegaEmbed(container, responsive, {actions:false, renderer:'svg'}).then(result => {
      if (token !== generation) { result.view.finalize(); return; }
      view = result.view;
      view.signal('cursor_time', time).runAsync();
    }).catch(() => {
      if (status) status.textContent =
        'Charts could not load. Read the embedded tables and saved trials, ' +
        'or reload with internet.';
    });
  }
  render();
  if (typeof ResizeObserver === 'function') new ResizeObserver(render).observe(container);
})();
</script>"""
