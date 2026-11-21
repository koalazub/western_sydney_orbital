"""Render Tom's polyfit_dodge local review from saved fuzz telemetry.

This is a local practice analysis, not an STP score. It reads one or more
``wsorb fuzz`` reports, reconstructs each flight in a launch-to-target frame,
and writes a deterministic SVG plus a machine-readable summary.

Example:
    uv run --locked python experiments/tom_polyfit_dodge_review.py \
        --series "6 m/s=runs/.../official-cruise6-seeds100-104.json" \
        --series "8 m/s=runs/.../official-cruise8-seeds100-104.json" \
        --output docs/analytics/tom-polyfit-dodge/review.svg \
        --json docs/analytics/tom-polyfit-dodge/review.json
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

PALETTE = ("#b45309", "#1d4ed8", "#0f766e", "#7c3aed", "#be123c")
INK = "#172033"
MUTED = "#475569"
GRID = "#e2e8f0"
FONT = "system-ui,-apple-system,'Segoe UI',sans-serif"


@dataclass(frozen=True)
class Run:
    series: str
    seed: int
    state: str
    status: str
    points: float | None
    time_s: float
    fuel_kg: float
    damage_j: float
    range_m: float
    speed_mps: float
    samples: tuple[tuple, ...]
    path: tuple[tuple[float, float, float, float], ...]
    path_length_m: float
    extra_distance_m: float
    max_lateral_m: float
    max_altitude_m: float
    boost_fuel_kg: float
    correction_fuel_kg: float
    burn_episodes: int
    correction_episodes: int
    command_dv_mps: float
    max_speed_mps: float

    @property
    def arrived(self) -> bool:
        return self.status == "success"

    @property
    def score_terms(self) -> dict[str, float | None]:
        if self.time_s <= 0:
            return {"time": None, "fuel": None, "damage": None}
        return {
            "time": 120.0 / self.time_s,
            "fuel": self.fuel_kg / 75.0,
            "damage": 2.0 * self.damage_j / 209200.0,
        }


def _unit(value):
    length = math.hypot(*value)
    if length <= 0:
        raise ValueError("zero-length basis vector")
    return tuple(component / length for component in value)


def _cross(left, right):
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right, strict=True))


def _sub(left, right):
    return tuple(a - b for a, b in zip(left, right, strict=True))


def _distance(left, right):
    return math.hypot(*_sub(left, right))


def _basis(start, target):
    along = _unit(_sub(target, start))
    side = _cross(along, (0.0, 0.0, 1.0))
    if math.hypot(*side) < 1e-9:
        side = _cross(along, (0.0, 1.0, 0.0))
    side = _unit(side)
    up = _unit(_cross(side, along))
    return along, side, up


def _coords(position, start, basis):
    relative = _sub(position, start)
    downrange = _dot(relative, basis[0])
    lateral = _dot(relative, basis[1])
    return downrange, lateral, relative[2]


def _burn_episodes(samples, threshold=1e-3):
    episodes = []
    current = None
    previous = samples[0]
    for sample in samples[1:]:
        dt = sample[0] - previous[0]
        fuel = sample[5] - previous[5]
        command = math.hypot(*sample[7])
        if command > threshold and dt > 0:
            if current is None:
                current = {
                    "start_s": previous[0],
                    "end_s": sample[0],
                    "fuel_kg": 0.0,
                    "dv_mps": 0.0,
                }
            current["end_s"] = sample[0]
            current["fuel_kg"] += max(0.0, fuel)
            current["dv_mps"] += command * dt
        elif current is not None:
            episodes.append(current)
            current = None
        previous = sample
    if current is not None:
        episodes.append(current)
    return episodes


def load_run(series: str, raw: dict) -> Run | None:
    telemetry = raw.get("telemetry")
    if not isinstance(telemetry, dict):
        return None
    samples = telemetry.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        return None
    measurement = raw.get("measurement") or {}
    start = tuple(float(value) for value in samples[0][1])
    target = tuple(float(value) for value in samples[0][4])
    basis = _basis(start, target)
    path = []
    path_length = 0.0
    previous_position = None
    for sample in samples:
        position = tuple(float(value) for value in sample[1])
        x, y, z = _coords(position, start, basis)
        speed = math.hypot(*sample[2])
        path.append((float(sample[0]), x, y, z, speed))
        if previous_position is not None:
            path_length += _distance(position, previous_position)
        previous_position = position
    final_position = tuple(float(value) for value in samples[-1][1])
    max_speed = max(entry[4] for entry in path)
    episodes = _burn_episodes(samples)
    boost_fuel = episodes[0]["fuel_kg"] if episodes else 0.0
    total_fuel = float(samples[-1][5])
    command_dv = sum(episode["dv_mps"] for episode in episodes)
    return Run(
        series=series,
        seed=int(raw.get("seed", -1)),
        state=str(raw.get("state")),
        status=str(measurement.get("status", "unknown")),
        points=raw.get("points"),
        time_s=float(measurement.get("time_s") or samples[-1][0]),
        fuel_kg=float(measurement.get("fuel_used_kg") or samples[-1][5]),
        damage_j=float(measurement.get("damage_j") or 0.0),
        range_m=float(measurement.get("range_m") or 0.0),
        speed_mps=float(measurement.get("relative_speed_mps") or 0.0),
        samples=tuple(tuple(sample) for sample in samples),
        path=tuple(path),
        path_length_m=path_length,
        extra_distance_m=path_length - _distance(start, final_position),
        max_lateral_m=max(abs(entry[2]) for entry in path),
        max_altitude_m=max(abs(entry[3]) for entry in path),
        boost_fuel_kg=boost_fuel,
        correction_fuel_kg=max(0.0, total_fuel - boost_fuel),
        burn_episodes=len(episodes),
        correction_episodes=max(0, len(episodes) - 1),
        command_dv_mps=command_dv,
        max_speed_mps=max_speed,
    )


def load_series(label: str, path: Path) -> list[Run]:
    report = json.loads(path.read_text(encoding="utf-8"))
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise ValueError(f"{path} has no runs")
    loaded = [run for raw in runs if (run := load_run(label, raw)) is not None]
    if not loaded:
        raise ValueError(f"{path} has no telemetry runs")
    return loaded


def summarize(runs: list[Run]) -> dict:
    arrived = [run for run in runs if run.arrived]
    points = [run.points for run in arrived if run.points is not None]
    return {
        "runs": len(runs),
        "arrivals": len(arrived),
        "mean_points": sum(points) / len(points) if points else None,
        "min_points": min(points) if points else None,
        "max_points": max(points) if points else None,
        "mean_time_s": sum(run.time_s for run in arrived) / len(arrived) if arrived else None,
        "mean_fuel_kg": sum(run.fuel_kg for run in arrived) / len(arrived) if arrived else None,
        "mean_damage_j": sum(run.damage_j for run in arrived) / len(arrived) if arrived else None,
        "mean_extra_m": sum(run.extra_distance_m for run in arrived) / len(arrived)
        if arrived
        else None,
        "mean_correction_fuel_kg": (
            sum(run.correction_fuel_kg for run in arrived) / len(arrived) if arrived else None
        ),
        "mean_correction_episodes": (
            sum(run.correction_episodes for run in arrived) / len(arrived) if arrived else None
        ),
        "mean_max_lateral_m": (
            sum(run.max_lateral_m for run in arrived) / len(arrived) if arrived else None
        ),
    }


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = []

    def add(self, value: str) -> None:
        self.parts.append(value)

    def text(self, x: float, y: float, value: str, size: int, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        prefix = f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}"{extra}>'
        self.add(prefix + escape(str(value)) + "</text>")

    def line(self, x1: float, y1: float, x2: float, y2: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"{extra}/>')

    def rect(self, x: float, y: float, width: float, height: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        prefix = f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}"'
        self.add(prefix + f' height="{height:.1f}"{extra}/>')

    def circle(self, x: float, y: float, radius: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}"{extra}/>')

    def polyline(self, points, **attrs: object) -> None:
        if len(points) < 2:
            return
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        encoded = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        self.add(f'<polyline points="{encoded}"{extra}/>')

    def svg(self) -> str:
        return "".join(self.parts)


def _map(value, low, high, start, end):
    if high <= low:
        return (start + end) / 2.0
    return start + (value - low) / (high - low) * (end - start)


def _axes(canvas, x, y, width, height, x_max, y_min, y_max, x_label, y_label):
    canvas.rect(x, y, width, height, rx=4, fill="#ffffff", stroke=GRID)
    for index in range(5):
        gx = x + width * index / 4
        canvas.line(gx, y, gx, y + height, stroke=GRID)
        canvas.text(
            gx,
            y + height + 16,
            f"{x_max * index / 4:.0f}",
            11,
            fill=MUTED,
            text_anchor="middle",
        )
    for index in range(5):
        value = y_min + (y_max - y_min) * index / 4
        gy = y + height - height * index / 4
        canvas.line(x, gy, x + width, gy, stroke=GRID)
        canvas.text(x - 6, gy + 4, f"{value:.0f}", 11, fill=MUTED, text_anchor="end")
    canvas.text(x + width / 2, y + height + 32, x_label, 12, fill=MUTED, text_anchor="middle")
    canvas.text(
        x - 38,
        y + height / 2,
        y_label,
        12,
        fill=MUTED,
        text_anchor="middle",
        transform=f"rotate(-90 {x - 38:.1f} {y + height / 2:.1f})",
    )


def render_svg(collections: dict[str, list[Run]]) -> str:
    canvas = Canvas(1400, 1080)
    canvas.add(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1400 1080" '
        'role="img" aria-labelledby="title" aria-describedby="description">'
    )
    canvas.add('<title id="title">Tom polyfit_dodge navigation, fuel and points review</title>')
    canvas.add(
        '<desc id="description">Local official-cloud practice runs reconstructed from '
        "saved fuzz telemetry. Paths are launch-to-target coordinates. Local points "
        "use the published formula on local counters and are not STP scores.</desc>"
    )
    canvas.add('<rect width="100%" height="100%" fill="#ffffff"/>')
    canvas.add(
        "<style>"
        f"text{{font-family:{FONT};fill:{INK};font-variant-numeric:tabular-nums}}"
        ".title{font-weight:650}.panel{font-weight:650}.muted{fill:#475569}"
        "</style>"
    )
    canvas.text(28, 38, "Tom polyfit_dodge · local navigation review", 26, font_weight=650)
    canvas.text(
        28,
        62,
        "Official cloud · seeds 100–104 · 4× owned native runs · "
        "ownship telemetry · local points, not STP scores",
        15,
        fill=MUTED,
    )
    canvas.text(
        28,
        84,
        "Paths use launch-to-target coordinates. Thin lines are individual seeds; "
        "the thick line is seed 100.",
        13,
        fill=MUTED,
    )

    labels = list(collections)
    colors = {label: PALETTE[index % len(PALETTE)] for index, label in enumerate(labels)}
    summaries = {label: summarize(runs) for label, runs in collections.items()}

    # Top-down path.
    left, top, width, height = 64, 120, 600, 300
    max_x = max(entry[1] for runs in collections.values() for run in runs for entry in run.path)
    max_lat = max(
        8.0,
        max(abs(entry[2]) for runs in collections.values() for run in runs for entry in run.path),
    )
    max_lat = max(max_lat * 1.15, 12.0)
    _axes(canvas, left, top, width, height, max_x, -max_lat, max_lat, "downrange, m", "lateral, m")
    canvas.circle(
        _map(max_x, 0, max_x, left, left + width),
        top + height / 2,
        7,
        fill="none",
        stroke="#64748b",
        stroke_dasharray="4 4",
    )
    for label, runs in collections.items():
        for run in runs:
            points = [
                (
                    _map(entry[1], 0, max_x, left, left + width),
                    _map(entry[2], -max_lat, max_lat, top + height, top),
                )
                for entry in run.path
            ]
            canvas.polyline(
                points,
                fill="none",
                stroke=colors[label],
                stroke_width=2.4 if run.seed == 100 else 1.1,
                opacity=0.85 if run.seed == 100 else 0.45,
                stroke_dasharray="" if run.arrived else "5 4",
            )
    canvas.text(left, top - 8, "Top-down path", 15, font_weight=650)

    # Altitude profile.
    left2 = 736
    min_z = min(
        0.0,
        min(entry[3] for runs in collections.values() for run in runs for entry in run.path),
    )
    max_z = max(
        8.0,
        max(entry[3] for runs in collections.values() for run in runs for entry in run.path),
        max(
            run.samples[0][4][2] - run.samples[0][1][2]
            for runs in collections.values()
            for run in runs
        ),
    )
    min_z *= 1.1
    max_z *= 1.1
    _axes(canvas, left2, top, width, height, max_x, min_z, max_z, "downrange, m", "altitude, m")
    target_altitude = max(
        run.samples[0][4][2] - run.samples[0][1][2] for runs in collections.values() for run in runs
    )
    canvas.circle(
        _map(max_x, 0, max_x, left2, left2 + width),
        _map(target_altitude, min_z, max_z, top + height, top),
        7,
        fill="none",
        stroke="#64748b",
        stroke_dasharray="4 4",
    )
    for label, runs in collections.items():
        for run in runs:
            points = [
                (
                    _map(entry[1], 0, max_x, left2, left2 + width),
                    _map(entry[3], min_z, max_z, top + height, top),
                )
                for entry in run.path
            ]
            canvas.polyline(
                points,
                fill="none",
                stroke=colors[label],
                stroke_width=2.4 if run.seed == 100 else 1.1,
                opacity=0.85 if run.seed == 100 else 0.45,
                stroke_dasharray="" if run.arrived else "5 4",
            )
    canvas.text(left2, top - 8, "Altitude profile", 15, font_weight=650)

    # Speed.
    left3, top3, width3, height3 = 64, 490, 600, 220
    max_t = max(run.time_s for runs in collections.values() for run in runs) * 1.03
    max_v = max(4.0, max(run.max_speed_mps for runs in collections.values() for run in runs) * 1.1)
    _axes(canvas, left3, top3, width3, height3, max_t, 0, max_v, "time, s", "speed, m/s")
    for label, runs in collections.items():
        for run in runs:
            points = [
                (
                    _map(entry[0], 0, max_t, left3, left3 + width3),
                    _map(entry[4], 0, max_v, top3 + height3, top3),
                )
                for entry in run.path
            ]
            canvas.polyline(
                points,
                fill="none",
                stroke=colors[label],
                stroke_width=1.8 if run.seed == 100 else 1.0,
                opacity=0.85 if run.seed == 100 else 0.45,
                stroke_dasharray="" if run.arrived else "5 4",
            )
    canvas.text(left3, top3 - 8, "Speed", 15, font_weight=650)

    # Fuel.
    left4, top4 = 736, 490
    max_fuel = max(1.0, max(run.fuel_kg for runs in collections.values() for run in runs) * 1.1)
    _axes(canvas, left4, top4, width3, height3, max_t, 0, max_fuel, "time, s", "fuel used, kg")
    for label, runs in collections.items():
        for run in runs:
            points = [
                (
                    _map(float(sample[0]), 0, max_t, left4, left4 + width3),
                    _map(float(sample[5]), 0, max_fuel, top4 + height3, top4),
                )
                for sample in run.samples
            ]
            canvas.polyline(
                points,
                fill="none",
                stroke=colors[label],
                stroke_width=1.8 if run.seed == 100 else 1.0,
                opacity=0.85 if run.seed == 100 else 0.45,
                stroke_dasharray="" if run.arrived else "5 4",
            )
    canvas.text(left4, top4 - 8, "Fuel", 15, font_weight=650)

    # Points and outcome table.
    table_top = 760
    canvas.text(28, table_top, "Terminal outcome and fuel decomposition", 16, font_weight=650)
    headers = (
        "series",
        "arrivals",
        "points mean",
        "time s",
        "fuel kg",
        "damage J",
        "extra m",
        "later kg",
        "later burns",
    )
    columns = (28, 260, 360, 470, 570, 670, 790, 900, 1040)
    for x, header in zip(columns, headers, strict=True):
        canvas.text(x, table_top + 22, header, 12, fill=MUTED, font_weight=650)
    for row, label in enumerate(labels):
        item = summaries[label]
        y = table_top + 46 + row * 26
        values = (
            label,
            f"{item['arrivals']}/{item['runs']}",
            f"{item['mean_points']:.2f}" if item["mean_points"] is not None else "—",
            f"{item['mean_time_s']:.1f}" if item["mean_time_s"] is not None else "—",
            f"{item['mean_fuel_kg']:.2f}" if item["mean_fuel_kg"] is not None else "—",
            f"{item['mean_damage_j']:.1f}" if item["mean_damage_j"] is not None else "—",
            f"{item['mean_extra_m']:.1f}" if item["mean_extra_m"] is not None else "—",
            (
                f"{item['mean_correction_fuel_kg']:.2f}"
                if item["mean_correction_fuel_kg"] is not None
                else "—"
            ),
            (
                f"{item['mean_correction_episodes']:.1f}"
                if item["mean_correction_episodes"] is not None
                else "—"
            ),
        )
        for index, (x, value) in enumerate(zip(columns, values, strict=True)):
            canvas.text(x, y, value, 12, font_weight=650 if index == 0 else 400)

    # Points by seed.
    points_top = 900
    canvas.text(28, points_top, "Local points by seed", 16, font_weight=650)
    chart_left, chart_width, chart_height = 260, 1050, 120
    seed_x = {}
    all_seeds = sorted({run.seed for runs in collections.values() for run in runs})
    for index, seed in enumerate(all_seeds):
        seed_x[seed] = chart_left + (index + 0.5) * chart_width / len(all_seeds)
        canvas.text(
            seed_x[seed],
            points_top + chart_height + 18,
            str(seed),
            11,
            fill=MUTED,
            text_anchor="middle",
        )
    max_points = max(
        10.0,
        max((run.points or 0.0) for runs in collections.values() for run in runs) * 1.1,
    )
    for value in (0, max_points / 2, max_points):
        y = points_top + chart_height - chart_height * value / max_points
        canvas.line(chart_left, y, chart_left + chart_width, y, stroke=GRID)
        canvas.text(chart_left - 8, y + 4, f"{value:.0f}", 11, fill=MUTED, text_anchor="end")
    for label, runs in collections.items():
        for run in runs:
            x = seed_x[run.seed]
            if run.points is not None:
                y = points_top + chart_height - chart_height * run.points / max_points
                canvas.circle(x, y, 5, fill=colors[label], stroke="#ffffff", stroke_width=1.5)
            else:
                canvas.text(
                    x,
                    points_top + chart_height - 4,
                    "×",
                    16,
                    fill="#b91c1c",
                    text_anchor="middle",
                )

    # Legend.
    legend_y = 1052
    for index, label in enumerate(labels):
        x = 28 + index * 300
        canvas.circle(x + 5, legend_y - 4, 5, fill=colors[label])
        item = summaries[label]
        canvas.text(
            x + 16,
            legend_y,
            f"{label} · {item['arrivals']}/{item['runs']} arrivals",
            12,
            fill=MUTED,
        )
    canvas.text(
        28,
        1072,
        "Source: saved fuzz telemetry · local points use the published formula · "
        "dashed paths did not arrive.",
        11,
        fill=MUTED,
    )
    canvas.add("</svg>")
    return canvas.svg()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--series",
        action="append",
        required=True,
        metavar="LABEL=PATH",
        help="fuzz report label and path; repeat for multiple cruise settings",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/analytics/tom-polyfit-dodge/review.svg"),
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=Path("docs/analytics/tom-polyfit-dodge/review.json"),
    )
    args = parser.parse_args(argv)

    collections: dict[str, list[Run]] = {}
    for item in args.series:
        if "=" not in item:
            raise ValueError("--series must be LABEL=PATH")
        label, path = item.rsplit("=", 1)
        collections[label] = load_series(label, Path(path))

    summary = {
        "schema": "wsorb.tom_polyfit_dodge_review",
        "schema_version": 1,
        "source": "saved local fuzz telemetry; not STP evidence",
        "series": {
            label: {
                "summary": summarize(runs),
                "runs": [
                    {
                        "seed": run.seed,
                        "state": run.state,
                        "status": run.status,
                        "points": run.points,
                        "time_s": run.time_s,
                        "fuel_kg": run.fuel_kg,
                        "damage_j": run.damage_j,
                        "range_m": run.range_m,
                        "speed_mps": run.speed_mps,
                        "path_length_m": run.path_length_m,
                        "extra_distance_m": run.extra_distance_m,
                        "max_lateral_m": run.max_lateral_m,
                        "max_altitude_m": run.max_altitude_m,
                        "boost_fuel_kg": run.boost_fuel_kg,
                        "correction_fuel_kg": run.correction_fuel_kg,
                        "burn_episodes": run.burn_episodes,
                        "correction_episodes": run.correction_episodes,
                        "command_dv_mps": run.command_dv_mps,
                        "max_speed_mps": run.max_speed_mps,
                        "score_terms": run.score_terms,
                    }
                    for run in runs
                ],
            }
            for label, runs in collections.items()
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(collections), encoding="utf-8")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(args.output)
    print(args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
