"""Parameter search for reviewed controller profiles using native seed fuzzing."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .controller_worlds import WORLD_IDS
from .controllers import PROFILE_IDS
from .fuzz import parse_seeds, run_fuzz

MAX_CANDIDATES = 64
MAX_DAMAGE_J = 209200.0
SCHEMA = "wsorb.tune"
SCHEMA_VERSION = 1


def candidate_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Expand a JSON parameter grid into a stable list of complete settings maps."""
    if not isinstance(grid, dict) or not grid:
        raise ValueError("grid must be a non-empty JSON object")
    keys = sorted(grid)
    for key in keys:
        values = grid[key]
        if not isinstance(key, str) or not isinstance(values, list) or not values:
            raise ValueError("every grid entry must be a non-empty list")
    count = math.prod(len(grid[key]) for key in keys)
    if count > MAX_CANDIDATES:
        raise ValueError(f"at most {MAX_CANDIDATES} candidates are allowed per search")
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(grid[key] for key in keys))
    ]


def compact_runs(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep the score-bearing fields without the per-run analytics payload."""
    fields = ("time_s", "fuel_used_kg", "damage_j", "range_m", "relative_speed_mps")
    return [
        {
            "seed": row.get("seed"),
            "state": row.get("state"),
            "native_success": row.get("native_success"),
            "points": row.get("points"),
            "error_code": row.get("error_code"),
            "measurement": {name: (row.get("measurement") or {}).get(name) for name in fields},
        }
        for row in report["runs"]
    ]


def objective(report: dict[str, Any]) -> float:
    """Max-min score with native success and zero damage as hard constraints."""
    summary = report["summary"]
    if (
        summary.get("failures")
        or summary.get("below_threshold")
        or summary.get("min_points") is None
    ):
        return float("-inf")
    for row in report["runs"]:
        damage = (row.get("measurement") or {}).get("damage_j")
        if (
            isinstance(damage, bool)
            or not isinstance(damage, (int, float))
            or not math.isfinite(damage)
            or damage > MAX_DAMAGE_J
        ):
            return float("-inf")
    return float(summary["min_points"])


def run_tune(
    candidates: list[dict[str, Any]],
    *,
    seeds: tuple[int, ...],
    validation_seeds: tuple[int, ...],
    world_id: str,
    profile_id: str,
    duration_s: float,
    workers: int,
    out: Path | None,
) -> dict[str, Any]:
    ranked: list[dict[str, Any]] = []
    for index, settings in enumerate(candidates, 1):
        print(
            f"[{index}/{len(candidates)}] {json.dumps(settings, sort_keys=True)}",
            file=sys.stderr,
            flush=True,
        )
        report = run_fuzz(
            seeds,
            world_id=world_id,
            profile_id=profile_id,
            duration_s=duration_s,
            threshold=None,
            workers=workers,
            out=None,
            settings=settings,
            progress=False,
        )
        score = objective(report)
        ranked.append(
            {
                "settings": settings,
                "score": score,
                "train_summary": report["summary"],
                "train_runs": compact_runs(report),
            }
        )
    ranked.sort(
        key=lambda row: (
            row["score"],
            row["train_summary"].get("mean_points") or float("-inf"),
        ),
        reverse=True,
    )
    best = ranked[0]
    validation: dict[str, Any] | None = None
    if validation_seeds:
        print("[validation] best candidate", file=sys.stderr, flush=True)
        report = run_fuzz(
            validation_seeds,
            world_id=world_id,
            profile_id=profile_id,
            duration_s=duration_s,
            threshold=None,
            workers=workers,
            out=None,
            settings=best["settings"],
            progress=False,
        )
        validation = {
            "score": objective(report),
            "summary": report["summary"],
            "runs": compact_runs(report),
        }
    result = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "objective": "max_min_points_with_native_success_and_official_damage_limit",
        "world": world_id,
        "profile": profile_id,
        "duration_s": duration_s,
        "seeds": list(seeds),
        "validation_seeds": list(validation_seeds),
        "workers": workers,
        "candidates": [
            {
                "settings": row["settings"],
                "score": row["score"] if math.isfinite(row["score"]) else None,
                "train_summary": row["train_summary"],
                "train_runs": row["train_runs"],
            }
            for row in ranked
        ],
        "best": {
            "settings": best["settings"],
            "score": best["score"] if math.isfinite(best["score"]) else None,
            "train_summary": best["train_summary"],
        },
        "validation": (
            {
                **validation,
                "score": (validation["score"] if math.isfinite(validation["score"]) else None),
            }
            if validation is not None
            else None
        ),
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return result


def _default_out(profile_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("runs") / f"tune-{profile_id}-{stamp}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wsorb tune",
        description="Search controller policy settings against native seed scores.",
    )
    parser.add_argument("--grid", required=True, help="JSON object or path to one")
    parser.add_argument("--seeds", default="100-107")
    parser.add_argument("--validation-seeds", default=None)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--world", choices=WORLD_IDS, default="official")
    parser.add_argument("--profile", choices=PROFILE_IDS, default="ali_mppi")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        try:
            grid = json.loads(args.grid)
        except json.JSONDecodeError:
            grid = json.loads(Path(args.grid).read_text(encoding="utf-8"))
        candidates = candidate_grid(grid)
        seeds = parse_seeds(args.seeds)
        validation_seeds = parse_seeds(args.validation_seeds) if args.validation_seeds else ()
        if not 1 <= args.workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        if not 0.0 < args.duration <= 1800.0:
            raise ValueError("duration must be between 0 and 1800 seconds")
        out = args.out if args.out is not None else _default_out(args.profile)
        result = run_tune(
            candidates,
            seeds=seeds,
            validation_seeds=validation_seeds,
            world_id=args.world,
            profile_id=args.profile,
            duration_s=args.duration,
            workers=args.workers,
            out=out,
        )
    except (ValueError, OSError) as error:
        print(f"wsorb tune: {error}", file=sys.stderr)
        return 2
    best = result["best"]
    print(f"best score: {best['score'] if best['score'] is not None else 'invalid'}")
    print(f"best settings: {json.dumps(best['settings'], sort_keys=True)}")
    if result["validation"] is not None:
        score = result["validation"]["score"]
        print(f"validation score: {score if score is not None else 'invalid'}")
    print(f"wrote {out}")
    return 0 if best["score"] is not None else 1
