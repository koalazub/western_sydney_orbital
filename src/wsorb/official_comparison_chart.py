"""Render the published official-profile summary as a deterministic SVG.

The chart is generated from ``summary.csv`` so the visual labels cannot drift
from the committed metrics. It is a local practice comparison, not an STP score.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ALGORITHM_ORDER = (
    "official_basic_beeline",
    "ali_official_profile",
    "baseline",
    "body-axis",
    "basic_pd_radar",
)
LABELS = {
    "official_basic_beeline": "Published BasicController",
    "ali_official_profile": "Ali official profile",
    "baseline": "WSO checkpoint baseline",
    "body-axis": "WSO checkpoint body-axis",
    "basic_pd_radar": "Basic PD/radar",
}
OUTCOME_COLORS = {
    "success": "#15803d",
    "failed_damage": "#b91c1c",
    "policy_error": "#64748b",
    "timeout": "#a16207",
    "engine_error": "#7c3aed",
    "interrupted": "#0f766e",
}
MISSING = "n/a"


@dataclass(frozen=True, slots=True)
class SummaryRow:
    algorithm: str
    label: str
    runs: int
    outcomes: Mapping[str, int]
    median_success_time_s: float | None
    median_final_relative_speed_mps: float | None
    median_fuel_used_kg_estimate: float | None
    runs_with_observed_damage: int


def _optional_float(value: str, name: str) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        result = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number or blank") from error
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _integer(value: str, name: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if result < 0:
        raise ValueError(f"{name} must be nonnegative")
    return result


def _outcomes(value: str, name: str) -> dict[str, int]:
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"{name} must be a Python-style outcome mapping") from error
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or type(count) is not int or count < 0
        for key, count in parsed.items()
    ):
        raise ValueError(f"{name} must map outcome names to nonnegative integers")
    return dict(parsed)


def load_summary(path: str | Path) -> tuple[SummaryRow, ...]:
    """Read and validate the committed summary; unknown algorithms are rejected."""
    source = Path(path)
    with source.open(newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("summary CSV has no header")
        rows = []
        for raw in reader:
            algorithm = raw.get("algorithm", "")
            if algorithm == "wso_official_profile":
                algorithm = "ali_official_profile"
            if algorithm not in ALGORITHM_ORDER:
                raise ValueError(f"unexpected algorithm in summary: {raw.get('algorithm')!r}")
            if any(row.algorithm == algorithm for row in rows):
                raise ValueError(f"summary contains the same algorithm twice: {algorithm}")
            outcomes = _outcomes(raw.get("outcomes", "{}"), f"{algorithm}.outcomes")
            runs = _integer(raw.get("runs", ""), f"{algorithm}.runs")
            if runs != 6:
                raise ValueError(f"{algorithm} must have exactly six attempts")
            if sum(outcomes.values()) != runs:
                raise ValueError(f"{algorithm} outcomes do not sum to runs")
            rows.append(
                SummaryRow(
                    algorithm=algorithm,
                    label=LABELS[algorithm],
                    runs=runs,
                    outcomes=outcomes,
                    median_success_time_s=_optional_float(
                        raw.get("median_success_time_s", ""),
                        f"{algorithm}.median_success_time_s",
                    ),
                    median_final_relative_speed_mps=_optional_float(
                        raw.get("median_final_relative_speed_mps", ""),
                        f"{algorithm}.median_final_relative_speed_mps",
                    ),
                    median_fuel_used_kg_estimate=_optional_float(
                        raw.get("median_fuel_used_kg_estimate", ""),
                        f"{algorithm}.median_fuel_used_kg_estimate",
                    ),
                    runs_with_observed_damage=_integer(
                        raw.get("runs_with_observed_damage", ""),
                        f"{algorithm}.runs_with_observed_damage",
                    ),
                )
            )
    present = {row.algorithm for row in rows}
    missing = set(ALGORITHM_ORDER) - present
    if missing:
        raise ValueError(f"summary is missing algorithms: {sorted(missing)}")
    return tuple(sorted(rows, key=lambda row: ALGORITHM_ORDER.index(row.algorithm)))


def _number(value: float | None, places: int, suffix: str) -> str:
    return MISSING if value is None else f"{value:.{places}f}{suffix}"


def _bar(
    x: float,
    y: float,
    width: float,
    height: float,
    fraction: float,
    color: str,
) -> str:
    filled = max(0.0, min(1.0, fraction)) * width
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" '
        f'rx="3" fill="#e2e8f0"/>'
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{filled:.1f}" height="{height:.1f}" '
        f'rx="3" fill="{color}"/>'
    )


def _outcome_bar(row: SummaryRow, x: float, y: float, width: float) -> str:
    parts = []
    offset = x
    for outcome, count in sorted(row.outcomes.items()):
        segment = width * count / row.runs
        color = OUTCOME_COLORS.get(outcome, "#475569")
        parts.append(
            f'<rect x="{offset:.1f}" y="{y:.1f}" width="{segment:.1f}" height="16" fill="{color}"/>'
        )
        if segment >= 45:
            parts.append(
                f'<text x="{offset + segment / 2:.1f}" y="{y + 12:.1f}" '
                f'text-anchor="middle" fill="white" font-size="11">{count}</text>'
            )
        offset += segment
    return "".join(parts)


def _legend(y: float) -> str:
    items = [
        ("success", "success"),
        ("failed_damage", "damage failure"),
        ("policy_error", "policy error"),
        ("timeout", "timeout"),
    ]
    parts = []
    x = 28.0
    for outcome, label in items:
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 11:.1f}" width="11" height="11" rx="2" '
            f'fill="{OUTCOME_COLORS[outcome]}"/>'
        )
        parts.append(f'<text x="{x + 17:.1f}" y="{y:.1f}" font-size="13">{label}</text>')
        x += 145.0
    return "".join(parts)


def render_svg(rows: Iterable[SummaryRow], *, source_sha256: str) -> str:
    rows = tuple(rows)
    width, height = 1200, 1120
    labels_x = 28.0
    bar_x = 250.0
    bar_width = 800.0
    value_x = 1070.0
    row_height = 30.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="title" aria-describedby="description" '
        f'data-source-sha256="{escape(source_sha256)}">',
        '<title id="title">Official-profile comparison: outcomes and performance metrics</title>',
        '<desc id="description">Local five-policy comparison on the 1.31 km competition reference '
        "with the 500-body official cloud. Three seeds, two consecutive attempts each. "
        "Values are taken directly from summary.csv. Local fuel and damage are not STP counters; "
        "this is not an STP score or a docking claim.</desc>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<style>text{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;fill:#172033}"
        ".title{font-size:26px;font-weight:650}.sub{font-size:15px}.caveat{font-size:13px;fill:#475569}"
        ".panel{font-size:16px;font-weight:650}.row{font-size:14px}.value{font-size:14px;font-weight:600}"
        ".tick{font-size:12px;fill:#64748b}</style>",
        '<text x="28" y="38" class="title">'
        "Official-profile comparison: outcomes and metrics"
        "</text>",
        '<text x="28" y="63" class="sub">1.31 km competition reference · 500-body official cloud · '
        "3 seeds × 2 consecutive attempts</text>",
        '<text x="28" y="86" class="caveat">Local practice evidence, not an STP score. '
        "Local fuel and damage are not STP counters; arrival is not docking.</text>",
        _legend(112.0),
    ]
    y = 142.0
    parts.append(f'<text x="{labels_x}" y="{y - 10}" class="panel">Outcomes</text>')
    for row in rows:
        parts.append(f'<text x="{labels_x}" y="{y + 12}" class="row">{escape(row.label)}</text>')
        parts.append(_outcome_bar(row, bar_x, y, bar_width))
        parts.append(
            f'<text x="{value_x}" y="{y + 12}" class="value">'
            f"{row.outcomes.get('success', 0)}/{row.runs} success</text>"
        )
        y += row_height
    y += 10.0

    def panel(
        title: str,
        value_of,
        maximum: float,
        places: int,
        suffix: str,
        color: str,
    ) -> None:
        nonlocal y
        parts.append(f'<text x="{labels_x}" y="{y}" class="panel">{escape(title)}</text>')
        parts.append(
            f'<text x="{value_x + 65:.1f}" y="{y}" class="tick" text-anchor="end">'
            f"0–{maximum:g}{suffix}</text>"
        )
        y += 12.0
        for row in rows:
            value = value_of(row)
            parts.append(
                f'<text x="{labels_x}" y="{y + 8:.1f}" class="row">{escape(row.label)}</text>'
            )
            if value is None:
                parts.append(
                    f'<text x="{bar_x}" y="{y + 8:.1f}" class="value" fill="#64748b">'
                    f"{MISSING}</text>"
                )
            else:
                parts.append(_bar(bar_x, y, bar_width, 16, value / maximum, color))
                parts.append(
                    f'<text x="{value_x}" y="{y + 12:.1f}" class="value">'
                    f"{value:.{places}f}{suffix}</text>"
                )
            y += row_height
        y += 14.0

    panel(
        "Median arrival time",
        lambda row: row.median_success_time_s,
        200.0,
        1,
        " s",
        "#075985",
    )
    panel(
        "Median entry speed",
        lambda row: row.median_final_relative_speed_mps,
        10.0,
        2,
        " m/s",
        "#c2410c",
    )
    panel(
        "Median local fuel estimate",
        lambda row: row.median_fuel_used_kg_estimate,
        160.0,
        1,
        " kg",
        "#0f766e",
    )

    parts.append(f'<text x="{labels_x}" y="{y}" class="panel">Runs with observed damage</text>')
    y += 16.0
    cell = bar_width / 6.0
    for row in rows:
        parts.append(
            f'<text x="{labels_x}" y="{y + 10:.1f}" class="row">{escape(row.label)}</text>'
        )
        for index in range(6):
            filled = index < row.runs_with_observed_damage
            parts.append(
                f'<rect x="{bar_x + index * cell:.1f}" y="{y:.1f}" width="{cell - 8:.1f}" '
                f'height="16" rx="3" fill="{"#b91c1c" if filled else "#e2e8f0"}"/>'
            )
        parts.append(
            f'<text x="{value_x}" y="{y + 12:.1f}" class="value">'
            f"{row.runs_with_observed_damage}/{row.runs}</text>"
        )
        y += row_height
    parts.append(
        f'<text x="28" y="{height - 20}" class="caveat">'
        "Source: summary.csv · self-check: bar lengths and labels are derived from the same rows"
        "</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def update_manifest(manifest_path: str | Path, svg_path: str | Path) -> None:
    """Add or replace the SVG entry in the published artifact manifest."""
    manifest_file = Path(manifest_path)
    svg_file = Path(svg_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be a list")
    data = svg_file.read_bytes()
    entry = {
        "path": svg_file.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    artifacts[:] = [item for item in artifacts if item.get("path") != svg_file.name]
    artifacts.append(entry)
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("docs/analytics/official-comparison/summary.csv"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/analytics/official-comparison/performance.svg"),
    )
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    rows = load_summary(args.summary)
    source_sha256 = hashlib.sha256(args.summary.read_bytes()).hexdigest()
    svg = render_svg(rows, source_sha256=source_sha256)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(svg, encoding="utf-8")
    if args.manifest is not None:
        update_manifest(args.manifest, args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
