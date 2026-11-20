"""Render the calibrated four-profile benchmark as a deterministic SVG.

The source is ``calibrated-trials.csv``: five seeds, one flight per profile and
seed. Means use arrivals only. This is local practice evidence, not an STP score.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from xml.sax.saxutils import escape

PROFILES = {
    "ali_mppi": ("Ali", "MPPI", "#1d4ed8"),
    "tom_polyfit_mvp": ("Tom", "Polyfit", "#b45309"),
    "bui_alpha_beta_mvp": ("Bui", "alpha-beta adapter", "#0f766e"),
    "basic_pd_radar": ("Harry", "PD/radar", "#7c3aed"),
}
ORDER = tuple(PROFILES)
SEEDS = (100, 101, 102, 103, 104)
OUTCOME_COLORS = {"success": "#15803d", "damage": "#b91c1c", "timeout": "#a16207"}
YELLOW = "#a16207"
GRID = "#e2e8f0"
INK = "#172033"
MUTED = "#475569"
FONT = "system-ui,-apple-system,'Segoe UI',sans-serif"


def _number(value: str, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _boolean(value: str, name: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def load_trials(path: str | Path) -> tuple[dict[str, object], ...]:
    """Read and validate one row per profile and seed."""
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("trials CSV has no header")
        rows = []
        for raw in reader:
            controller = raw.get("controller", "")
            if controller not in PROFILES:
                raise ValueError(f"unexpected controller: {controller!r}")
            seed = int(raw.get("seed", ""))
            if seed not in SEEDS:
                raise ValueError(f"unexpected seed: {seed}")
            state = raw.get("state", "")
            if state not in OUTCOME_COLORS:
                raise ValueError(f"unexpected state: {state!r}")
            arrived = _boolean(raw.get("arrived", ""), f"{controller}.{seed}.arrived")
            if arrived != (state == "success"):
                raise ValueError("success and arrived disagree")
            points_text = raw.get("points", "")
            if arrived != bool(points_text):
                raise ValueError("points must be present exactly for arrived runs")
            points = _number(points_text, f"{controller}.{seed}.points") if points_text else None
            rows.append(
                {
                    "controller": controller,
                    "owner": raw.get("owner", ""),
                    "seed": seed,
                    "state": state,
                    "arrived": arrived,
                    "points": points if arrived else None,
                    "time_s": _number(raw.get("time_s", ""), f"{controller}.{seed}.time_s"),
                    "fuel_kg": _number(raw.get("fuel_kg", ""), f"{controller}.{seed}.fuel_kg"),
                    "damage_j": _number(raw.get("damage_j", ""), f"{controller}.{seed}.damage_j"),
                }
            )
    keys = {(row["controller"], row["seed"]) for row in rows}
    expected = {(controller, seed) for controller in ORDER for seed in SEEDS}
    if keys != expected or len(rows) != len(expected):
        raise ValueError("trials CSV must contain every controller and seed exactly once")
    return tuple(sorted(rows, key=lambda row: (ORDER.index(row["controller"]), row["seed"])))


def summarize(rows: Iterable[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    """Compute per-profile arrivals and arrival-only means."""
    rows = tuple(rows)
    result = {}
    for controller in ORDER:
        group = [row for row in rows if row["controller"] == controller]
        arrived = [row for row in group if row["arrived"]]
        if not arrived:
            raise ValueError(f"{controller} has no arrivals")
        result[controller] = {
            "label": PROFILES[controller][0],
            "detail": PROFILES[controller][1],
            "color": PROFILES[controller][2],
            "arrivals": len(arrived),
            "runs": len(group),
            "mean_points": sum(float(row["points"]) for row in arrived) / len(arrived),
            "mean_time_s": sum(float(row["time_s"]) for row in arrived) / len(arrived),
            "mean_fuel_kg": sum(float(row["fuel_kg"]) for row in arrived) / len(arrived),
            "damage_runs": sum(row["state"] == "damage" for row in group),
            "group": group,
        }
    return result


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.parts: list[str] = []

    def add(self, value: str) -> None:
        self.parts.append(value)

    def text(self, x: float, y: float, value: str, size: int, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}"{extra}>{escape(value)}</text>')

    def line(self, x1: float, y1: float, x2: float, y2: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"{extra}/>')

    def rect(self, x: float, y: float, width: float, height: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}"{extra}/>'
        )

    def circle(self, x: float, y: float, radius: float, **attrs: object) -> None:
        extra = "".join(f' {key.replace("_", "-")}="{value}"' for key, value in attrs.items())
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}"{extra}/>')

    def svg(self) -> str:
        return "".join(self.parts)


def _map(value: float, low: float, high: float, start: float, end: float) -> float:
    return start + (value - low) / (high - low) * (end - start)


def _points_curve(time_s: float, points: float) -> float:
    return 75.0 * (120.0 / time_s + 0.5 - points / 100.0)


def render_svg(rows: Iterable[Mapping[str, object]]) -> str:
    rows = tuple(rows)
    summary = summarize(rows)
    canvas = Canvas(1200, 900)
    canvas.add(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 900" '
        'role="img" aria-labelledby="title" aria-describedby="description">'
    )
    canvas.add(
        '<title id="title">Calibrated four-profile comparison: '
        "outcomes, score, fuel, time and seed spread</title>"
    )
    canvas.add(
        '<desc id="description">Local official-cloud benchmark, five seeds, '
        "one flight per profile. Bars, points and axes are derived from "
        "calibrated-trials.csv. Local points are not STP scores.</desc>"
    )
    canvas.add('<rect width="100%" height="100%" fill="#ffffff"/>')
    canvas.add(
        "<style>"
        f"text{{font-family:{FONT};fill:{INK};font-variant-numeric:tabular-nums}}"
        ".title{font-weight:650}.panel{font-weight:650}.muted{fill:#475569}"
        "</style>"
    )
    canvas.text(28, 36, "Calibrated v0.56 local benchmark", 26, font_weight=650)
    canvas.text(
        28,
        60,
        "Official cloud · seeds 100–104 · 1× · thrust scale 0.22 · one flight per profile and seed",
        15,
        fill=MUTED,
    )
    canvas.text(
        28,
        82,
        "Local points apply the published formula to local counters. "
        "Arrival is entry to the 50 m region, not docking.",
        13,
        fill=MUTED,
    )

    # Outcome matrix
    top = 124.0
    canvas.text(28, top, "Outcome by seed", 16, font_weight=650)
    cell_x = 250.0
    cell_w = 54.0
    for index, seed in enumerate(SEEDS):
        canvas.text(cell_x + index * cell_w + 4, top - 8, str(seed), 12, fill=MUTED)
    for row_index, controller in enumerate(ORDER):
        item = summary[controller]
        y = top + 18 + row_index * 38
        canvas.text(28, y + 16, f"{item['label']} · {item['detail']}", 14)
        by_seed = {row["seed"]: row for row in item["group"]}
        for index, seed in enumerate(SEEDS):
            state = str(by_seed[seed]["state"])
            canvas.rect(
                cell_x + index * cell_w,
                y,
                cell_w - 6,
                22,
                rx=3,
                fill=OUTCOME_COLORS[state],
            )
            if state != "success":
                canvas.text(
                    cell_x + index * cell_w + 4,
                    y + 16,
                    "D" if state == "damage" else "T",
                    13,
                    fill="#ffffff",
                    font_weight=650,
                )
        canvas.text(
            cell_x + len(SEEDS) * cell_w + 14,
            y + 16,
            f"{item['arrivals']}/{item['runs']}",
            14,
            font_weight=650,
        )
    legend_y = top + 18 + len(ORDER) * 38 + 4
    legend = (
        ("success", "#15803d", "arrival"),
        ("damage", "#b91c1c", "damage"),
        ("timeout", "#a16207", "timeout"),
    )
    for index, (_, color, label) in enumerate(legend):
        canvas.rect(28 + index * 140, legend_y, 11, 11, rx=2, fill=color)
        canvas.text(45 + index * 140, legend_y + 10, label, 12, fill=MUTED)

    # Mean points
    panel_x = 700.0
    canvas.text(panel_x, top, "Mean points · arrivals only", 16, font_weight=650)
    max_points = 70.0
    bar_x, bar_w = panel_x + 196.0, 210.0
    for index, controller in enumerate(ORDER):
        item = summary[controller]
        y = top + 18 + index * 38
        canvas.text(panel_x, y + 16, item["label"], 14)
        value = float(item["mean_points"])
        canvas.rect(bar_x, y, bar_w, 22, rx=3, fill=GRID)
        canvas.rect(bar_x, y, bar_w * value / max_points, 22, rx=3, fill=str(item["color"]))
        canvas.text(
            bar_x + bar_w + 8,
            y + 16,
            f"{value:.1f}  n={item['arrivals']}",
            14,
            font_weight=650,
        )

    # Fuel/time trade-off
    left, right = 76.0, 620.0
    bottom_top, bottom_bottom = 380.0, 760.0
    canvas.text(28, bottom_top - 18, "Fuel/time trade-off · mean arrived runs", 16, font_weight=650)
    x_low, x_high = 20.0, 80.0
    y_low, y_high = 170.0, 230.0

    for fuel in (20, 40, 60, 80):
        x = _map(fuel, x_low, x_high, left, right)
        canvas.line(x, bottom_top, x, bottom_bottom, stroke=GRID)
        canvas.text(x - 10, bottom_bottom + 18, f"{fuel:g}", 12, fill=MUTED)
    for time_s in (170, 190, 210, 230):
        y = _map(time_s, y_low, y_high, bottom_bottom, bottom_top)
        canvas.line(left, y, right, y, stroke=GRID)
        canvas.text(left - 32, y + 4, f"{time_s:g}", 12, fill=MUTED)
    canvas.text(
        (left + right) / 2,
        bottom_bottom + 38,
        "fuel used, kg",
        13,
        fill=MUTED,
        text_anchor="middle",
    )
    canvas.text(
        left - 46,
        (bottom_top + bottom_bottom) / 2,
        "arrival time, s",
        13,
        fill=MUTED,
        text_anchor="middle",
        transform=f"rotate(-90 {left - 46:.1f} {(bottom_top + bottom_bottom) / 2:.1f})",
    )
    for points in (20, 30, 40, 50, 60):
        path = []
        for time_s in range(170, 231, 2):
            fuel = _points_curve(float(time_s), float(points))
            if x_low <= fuel <= x_high:
                path.append(
                    f"{_map(fuel, x_low, x_high, left, right):.1f},"
                    f"{_map(float(time_s), y_low, y_high, bottom_bottom, bottom_top):.1f}"
                )
        if len(path) >= 2:
            canvas.add(
                f'<polyline points="{" ".join(path)}" fill="none" stroke="#cbd5e1" '
                f'stroke-width="1.2" stroke-dasharray="4 4"/>'
            )
            x, y = path[-1].split(",")
            canvas.text(float(x) + 3, float(y) - 3, f"{points}", 10, fill="#94a3b8")
    for controller in ORDER:
        item = summary[controller]
        x = _map(float(item["mean_fuel_kg"]), x_low, x_high, left, right)
        y = _map(float(item["mean_time_s"]), y_low, y_high, bottom_bottom, bottom_top)
        canvas.circle(x, y, 7, fill=str(item["color"]), stroke="#ffffff", stroke_width=2)
        canvas.text(
            x + 11, y + 4, f"{item['label']} {float(item['mean_points']):.1f}", 13, font_weight=650
        )
    canvas.text(
        28,
        bottom_bottom + 62,
        "Dashed lines: local points iso-score. Lower-left is cheaper and faster.",
        12,
        fill=MUTED,
    )

    # Points by seed
    seed_left, seed_right = 700.0, 1172.0
    canvas.text(seed_left, bottom_top - 18, "Points by seed", 16, font_weight=650)
    seed_x = {
        seed: seed_left + (index + 0.5) * (seed_right - seed_left) / len(SEEDS)
        for index, seed in enumerate(SEEDS)
    }
    points_low, points_high = 10.0, 70.0
    for points in (20, 40, 60):
        y = _map(points, points_low, points_high, bottom_bottom, bottom_top)
        canvas.line(seed_left, y, seed_right, y, stroke=GRID)
        canvas.text(seed_left - 28, y + 4, f"{points:g}", 12, fill=MUTED)
    for seed, x in seed_x.items():
        canvas.text(x, bottom_bottom + 18, str(seed), 12, fill=MUTED, text_anchor="middle")
    for controller in ORDER:
        item = summary[controller]
        by_seed = {row["seed"]: row for row in item["group"]}
        path = []
        for seed in SEEDS:
            row = by_seed[seed]
            if row["arrived"]:
                x = seed_x[seed]
                y = _map(float(row["points"]), points_low, points_high, bottom_bottom, bottom_top)
                path.append(f"{x:.1f},{y:.1f}")
                canvas.circle(x, y, 5, fill=str(item["color"]), stroke="#ffffff", stroke_width=1.5)
            elif len(path) >= 2:
                canvas.add(
                    f'<polyline points="{" ".join(path)}" fill="none" stroke="{item["color"]}" '
                    f'stroke-width="1.3" opacity="0.55"/>'
                )
                path = []
            else:
                path = []
        if len(path) >= 2:
            canvas.add(
                f'<polyline points="{" ".join(path)}" fill="none" stroke="{item["color"]}" '
                f'stroke-width="1.3" opacity="0.55"/>'
            )
    canvas.text(
        seed_left,
        bottom_bottom + 62,
        "Dots: arrived runs. Gaps: timeout or damage.",
        12,
        fill=MUTED,
    )
    legend_y = 838.0
    for index, controller in enumerate(ORDER):
        item = summary[controller]
        x = 28 + index * 285
        canvas.circle(x + 5, legend_y - 4, 5, fill=str(item["color"]))
        canvas.text(
            x + 16,
            legend_y,
            f"{item['label']} · {item['detail']} · {item['arrivals']}/{item['runs']}",
            12,
            fill=MUTED,
        )
    canvas.text(
        28, 878, "Source: calibrated-trials.csv · local points, not STP scores.", 12, fill=MUTED
    )
    canvas.add("</svg>")
    return canvas.svg()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trials",
        type=Path,
        default=Path("docs/analytics/official-comparison/calibrated-trials.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/analytics/official-comparison/calibrated-performance.svg"),
    )
    args = parser.parse_args(argv)
    rows = load_trials(args.trials)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_svg(rows), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
