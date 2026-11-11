#!/usr/bin/env python3
"""Summarize the controller_mppi.csv produced by the SDK entrypoint."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def segments(rows: list[dict[str, str]], key: str, threshold: float) -> list[dict[str, float]]:
    result = []
    start = None
    for index, row in enumerate(rows):
        active = float(row[key]) > threshold
        if active and start is None:
            start = index
        if not active and start is not None:
            result.append(
                {
                    "start_s": float(rows[start]["time_s"]),
                    "end_s": float(rows[index - 1]["time_s"]),
                    "duration_s": float(rows[index - 1]["time_s"]) - float(rows[start]["time_s"]),
                    "fuel_kg": float(rows[index - 1]["fuel"]) - float(rows[start]["fuel"]),
                }
            )
            start = None
    if start is not None:
        result.append(
            {
                "start_s": float(rows[start]["time_s"]),
                "end_s": float(rows[-1]["time_s"]),
                "duration_s": float(rows[-1]["time_s"]) - float(rows[start]["time_s"]),
                "fuel_kg": float(rows[-1]["fuel"]) - float(rows[start]["fuel"]),
            }
        )
    return result


def summarize(path: Path) -> dict:
    rows = list(csv.DictReader(path.open()))
    if not rows:
        raise ValueError("No telemetry rows")
    speeds = [float(row["speed"]) for row in rows]
    burn = segments(rows, "accel_norm", 1e-6)
    angular = segments(rows, "ang_norm", 1e-6)
    errors = [float(row["velocity_error"]) for row in rows]
    alignment = [float(row["alignment"]) for row in rows]
    return {
        "source": str(path),
        "samples": len(rows),
        "duration_s": float(rows[-1]["time_s"]) - float(rows[0]["time_s"]),
        "speed_mps": {
            "min": min(speeds),
            "max": max(speeds),
            "mean": sum(speeds) / len(speeds),
            "final": speeds[-1],
        },
        "fuel_kg": float(rows[-1]["fuel"]),
        "damage_j": float(rows[-1]["damage"]),
        "translation_burn": {
            "segments": len(burn),
            "total_s": sum(item["duration_s"] for item in burn),
            "micro_segments_under_0_2_s": sum(item["duration_s"] < 0.2 for item in burn),
            "segments_detail": burn,
        },
        "angular_burn": {
            "segments": len(angular),
            "total_s": sum(item["duration_s"] for item in angular),
            "micro_segments_under_0_2_s": sum(item["duration_s"] < 0.2 for item in angular),
            "segments_detail": angular,
        },
        "velocity_error_mps": {
            "mean": sum(errors) / len(errors),
            "max": max(errors),
        },
        "alignment_rad": {
            "mean": sum(alignment) / len(alignment),
            "max": max(alignment),
            "final": alignment[-1],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, nargs="?", default=Path("controller_mppi.csv"))
    args = parser.parse_args()
    print(json.dumps(summarize(args.log), indent=2))


if __name__ == "__main__":
    main()
