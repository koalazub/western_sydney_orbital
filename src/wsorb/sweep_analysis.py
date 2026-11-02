"""Pure summaries of seeded local flight sweeps, with no simulator dependency.

Debris count is a density proxy only while the region, sizes and distribution
are fixed. Radar ranges are noisy, incomplete observations, not collision truth.
Damage changes locate an interval to inspect; they do not identify the collider
or establish why the controller failed. Fuel and damage are local estimates.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .flight_analytics import COUNTERS, NATIVE_NUMBERS, FlightAnalyticsRecorder


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


def write_report(directory: str | Path, *, trial: str | None = None) -> Path:
    """Write an interactive HTML report from saved results; no simulation runs.

    Altair is imported only here (the existing ``lab`` extra). Charts load their
    Vega libraries from a CDN; all experiment data stays embedded in the file.
    The detailed trace is the first non-success with samples, with other
    algorithms at that same seed/count shown for comparison, not cherry-picked
    as a causal explanation. Full evidence remains in the saved trial folders.
    """
    import csv
    import html
    import json

    import altair as alt

    directory = Path(directory).resolve()
    with (directory / "runs.csv").open(newline="") as stream:
        runs = list(csv.DictReader(stream))
    groups = json.loads((directory / "summary.json").read_text())
    numeric = (
        "seed",
        "debris_count",
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
    charts = []
    if groups:
        base = alt.Chart(alt.Data(values=groups)).encode(
            x=alt.X("debris_count:Q", title="Debris count (fixed-region density proxy)"),
            color="algorithm:N",
            tooltip=["algorithm:N", "debris_count:Q", "runs:Q", "success_rate:Q", "outcomes:N"],
        )
        rate = base.mark_line(point=True).encode(
            y=alt.Y(
                "success_rate:Q",
                title="Success fraction of attempted runs",
                scale=alt.Scale(domain=[0, 1]),
            )
        )
        interval = base.mark_rule().encode(
            y="success_rate_wilson95_low:Q", y2="success_rate_wilson95_high:Q"
        )
        charts.append(
            (rate + interval).properties(
                title="Success across sampled seeds (95% Wilson intervals only for unique seeds)",
                height=220,
            )
        )
    if runs:
        scored = [row for row in runs if row.get("final_score") is not None]
        if scored:
            charts.append(
                alt.Chart(alt.Data(values=scored))
                .mark_point(size=65)
                .encode(
                    x=alt.X("debris_count:Q", title="Debris count"),
                    y=alt.Y(
                        "final_score:Q", title="Recorded native score estimate, higher is better"
                    ),
                    color="algorithm:N",
                    tooltip=[
                        "algorithm:N",
                        "seed:Q",
                        "outcome:N",
                        "final_score:Q",
                        "score_progress_points:Q",
                        "score_collision_penalty_points:Q",
                        "score_fuel_penalty_points:Q",
                        "score_offset_points:Q",
                    ],
                )
                .properties(
                    title="STP-formula score using local fuel and damage estimates", height=220
                )
            )
        points = alt.Chart(alt.Data(values=runs)).encode(
            x=alt.X("debris_count:Q", title="Debris count"),
            shape="algorithm:N",
            tooltip=["algorithm:N", "seed:Q", "debris_count:Q", "outcome:N", "error:N"],
        )
        charts.append(
            points.mark_point(size=65, opacity=0.7)
            .encode(
                y=alt.Y("final_target_range_m:Q", title="Final sampled target distance (m)"),
                color="outcome:N",
            )
            .properties(title="Where each run ended", height=220)
        )
        damage = [row for row in runs if row["first_damage_by_s"] is not None]
        if damage:
            event = alt.Chart(alt.Data(values=damage)).encode(
                x=alt.X("debris_count:Q", title="Debris count"),
                color="first_damage_phase:N",
                shape="algorithm:N",
                tooltip=[
                    "algorithm:N",
                    "seed:Q",
                    "first_damage_phase:N",
                    "first_damage_after_s:Q",
                    "first_damage_by_s:Q",
                ],
            )
            charts.append(
                (
                    event.mark_rule().encode(y="first_damage_after_s:Q", y2="first_damage_by_s:Q")
                    + event.mark_point(size=65).encode(
                        y=alt.Y(
                            "first_damage_by_s:Q",
                            title="First observed damage by simulation time (s)",
                        )
                    )
                ).properties(title="First damage intervals and observed phases", height=220)
            )
    selected = None
    for row in runs:
        if trial is not None and row.get("trial_directory") != trial:
            continue
        if trial is None and row["outcome"] == "success":
            continue
        trial_dir = directory / row.get("trial_directory", "")
        if trial_dir.parent == directory and (trial_dir / "trace.jsonl").is_file():
            with (trial_dir / "trace.jsonl").open() as trace:
                if next(trace, None):
                    selected = row
                    break
    if trial is not None and selected is None:
        raise ValueError("Selected trial has no saved observations")
    trace_rows = []
    if selected is not None:
        for row in runs:
            if (row["seed"], row["debris_count"]) != (selected["seed"], selected["debris_count"]):
                continue
            trial_dir = directory / row.get("trial_directory", "")
            if trial_dir.parent != directory or not (trial_dir / "trace.jsonl").is_file():
                continue
            metadata = json.loads((trial_dir / "run.json").read_text())
            with (trial_dir / "trace.jsonl").open() as stream:
                for line in stream:
                    sample = json.loads(line)
                    observation = _mapping(sample.get("observation"))
                    mission = _mapping(observation.get("mission"))
                    ranges, _ = _radar(observation)
                    trace_rows.append(
                        {
                            "algorithm": metadata["algorithm"],
                            "time_s": _number(observation.get("time_s")),
                            "target_distance_m": _target_metric(observation, "target_distance_m"),
                            "relative_speed_mps": _target_metric(observation, "relative_speed_mps"),
                            "damage_j_estimate": _number(mission.get("damage")),
                            "terrain_center_clearance_m": _clearance(observation),
                            "nearest_radar_range_m": _minimum(ranges),
                            "requested_accel_mps2": _distance(sample.get("accel_command")),
                        }
                    )
        for metric, title in (
            ("target_distance_m", "Target distance (m)"),
            ("relative_speed_mps", "Target relative speed (m/s)"),
            ("damage_j_estimate", "Local damage estimate (J)"),
            ("terrain_center_clearance_m", "Terrain centre clearance (m)"),
            ("nearest_radar_range_m", "Nearest observed radar range (m)"),
            ("requested_accel_mps2", "Requested acceleration magnitude (m/s²)"),
        ):
            charts.append(
                alt.Chart(alt.Data(values=trace_rows))
                .mark_line()
                .encode(
                    x=alt.X("time_s:Q", title="Simulation time (s)"),
                    y=alt.Y(f"{metric}:Q", title=title),
                    color="algorithm:N",
                    tooltip=["algorithm:N", "time_s:Q", f"{metric}:Q"],
                )
                .properties(
                    title=(
                        f"Paired trace: seed {selected['seed']:g}, "
                        f"count {selected['debris_count']:g}"
                    ),
                    height=150,
                )
            )
    table_keys = (
        "algorithm",
        "debris_count",
        "runs",
        "successes",
        "physical_success_rate",
        "outcomes",
    )
    table = "<table><tr>" + "".join(f"<th>{html.escape(key)}</th>" for key in table_keys) + "</tr>"
    for group in groups:
        table += (
            "<tr>"
            + "".join(f"<td>{html.escape(str(group.get(key)))}</td>" for key in table_keys)
            + "</tr>"
        )
    table += "</table>"
    chart_html = "<p>No measurements available to chart.</p>"
    if charts:
        chart = alt.vconcat(*(item.properties(width=800) for item in charts)).resolve_scale(
            color="independent", shape="independent"
        )
        specification = json.dumps(chart.to_dict(), allow_nan=False).replace("<", "\\u003c")
        chart_html = (
            '<div id="charts"></div><script>vegaEmbed("#charts", '
            + specification
            + ", {actions:false});</script>"
        )
    report = directory / "report.html"
    report.write_text(
        '<!doctype html><html><head><meta charset="utf-8"><title>Flight sweep analysis</title>'
        '<script src="https://cdn.jsdelivr.net/npm/vega@6"></script>'
        '<script src="https://cdn.jsdelivr.net/npm/vega-lite@6"></script>'
        '<script src="https://cdn.jsdelivr.net/npm/vega-embed@7"></script>'
        "<style>body{font:16px system-ui;max-width:1000px;margin:32px auto;padding:0 20px}"
        "td,th{padding:8px;text-align:left;border-bottom:1px solid #ddd}"
        "table{border-collapse:collapse}</style>"
        "</head><body><h1>Seeded flight experiments</h1>"
        "<p>Success means entering the local engine's arrival radius. It does not impose "
        "an arrival-speed limit or establish a safe rendezvous. Inspect relative speed separately. "
        "These are practice-model results, not licensed competition validation.</p>"
        "<p>The native score applies the published STP success/failure formula to local "
        "fuel and collision-damage estimates. Completion time supplies the speed bonus; "
        "arrival velocity is separate. Higher scores are better; negative scores are valid. "
        "Missing historical scores or penalty terms are not reconstructed.</p>"
        "<p>Debris count is a density proxy only with fixed region, sizes and distribution. "
        "Seeds are paired across algorithms; changing count need not retain the same rocks. "
        "Finite physics steps and sampled observations can miss between-sample events.</p>"
        "<p>Fuel and damage are local estimates. First damage brackets an observation interval, "
        "not an exact contact time or a proven cause. The selected trial (by default the first "
        "non-success with samples) is shown alongside algorithms with the same seed/count. "
        "Other trials remain "
        "available in their saved folders.</p>"
        "<p>Chart success fractions include all attempts; physical success rates in the table "
        "exclude policy errors, engine errors and interruptions. Error outcomes need separate "
        "investigation. Wilson intervals describe sampled-seed variation, not model accuracy. "
        "Repeated attempts at the same seed are not independent seed draws, so their Wilson "
        "bounds are omitted. Repeated-seed success counts are descriptive, not evidence of "
        "robustness across additional seeds. "
        "Charts require internet access to load Vega; experiment data is embedded locally.</p>"
        "<p>Physical distance, speed and fuel group medians exclude software-error runs; "
        "completion and timeout horizons still differ. Policy/module source is recorded when "
        "inspectable. New sweeps also snapshot local package sources and the lockfile hash, "
        "but external/custom imported dependencies are not a complete environment snapshot.</p>"
        + table
        + chart_html
        + "</body></html>",
        encoding="utf-8",
    )
    return report
