"""Seed fuzzing for reviewed controller profiles on the real headless engine."""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointOptions, isolated_simulation
from .controller_service import JobMonitor, execute_job, parse_job
from .controller_worlds import WORLD_IDS, controller_world, uint32
from .controllers import PROFILE_IDS
from .local_api import control_context

MAX_SEEDS = 256
MAX_ATTEMPTS = 2
RETRYABLE_ERRORS = frozenset({"lease_expired", "claim_failed", "command_failed"})
SCHEMA = "wsorb.fuzz"
SCHEMA_VERSION = 1


def parse_seeds(value: str) -> tuple[int, ...]:
    """Parse ``100-107``, ``100,102,105`` or a mixture into unique uint32 seeds."""
    seeds: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = (uint32(int(piece)) for piece in part.split("-", 1))
                if end < start:
                    raise ValueError("seed range is reversed")
                if len(seeds) + (end - start + 1) > MAX_SEEDS:
                    raise ValueError(f"at most {MAX_SEEDS} seeds are allowed per fuzz run")
                seeds.extend(range(start, end + 1))
            else:
                seeds.append(uint32(int(part)))
        except (TypeError, ValueError):
            raise ValueError("seeds must be uint32 values or inclusive uint32 ranges") from None
    if not seeds:
        raise ValueError("at least one seed is required")
    if len(seeds) > MAX_SEEDS:
        raise ValueError(f"at most {MAX_SEEDS} seeds are allowed per fuzz run")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    return tuple(seeds)


def score_points(measurement: dict[str, Any]) -> float | None:
    """Published practice score, or None when the run has no usable measurement."""
    values = [measurement.get(name) for name in ("time_s", "fuel_used_kg", "damage_j")]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0.0
        for value in values
    ):
        return None
    time_s, fuel_kg, damage_j = (float(value) for value in values)
    if time_s <= 0.0:
        return None
    return 100.0 * (120.0 / time_s - 2.0 * damage_j / 209200.0 - fuel_kg / 75.0 + 0.5)


def run_seed(
    seed: int,
    *,
    world_id: str,
    profile_id: str,
    duration_s: float,
    wall_budget_s: float,
    playback_rate: int = 1,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one seed on a fresh owned native server. Never reuses world or controller state."""
    started = time.monotonic()
    try:
        with isolated_simulation() as source:
            world = controller_world(world_id)
            initial = source.configure_flight(world.settings, world.preset, seed)
            job = parse_job(
                {
                    "id": str(uuid.uuid4()),
                    "profile_id": profile_id,
                    "seed": seed,
                    "world_id": world_id,
                    "duration_s": duration_s,
                    "expected_context": control_context(initial),
                    "owner_page_id": None,
                    "status": "running",
                    "stop_requested": False,
                }
            )
            summary = execute_job(
                source,
                job,
                CheckpointOptions(
                    isolated=True,
                    run=True,
                    wall_budget_s=wall_budget_s,
                    cadence_hz=min(50.0, 10.0 * playback_rate),
                    playback_rate=playback_rate,
                    settings=settings,
                ),
                JobMonitor(job, threading.Event()),
                allow_playback=True,
            )
    except Exception as error:
        return {
            "seed": seed,
            "state": "error",
            "native_success": False,
            "measurement": {},
            "points": None,
            "error_code": getattr(error, "code", type(error).__name__),
            "engine_sha256": None,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
    measurement = summary.get("measurement")
    measurement = measurement if isinstance(measurement, dict) else {}
    native_success = summary.get("native_success") is True
    return {
        "seed": seed,
        "state": summary.get("state"),
        "native_success": native_success,
        "measurement": measurement,
        "points": score_points(measurement) if native_success else None,
        "error_code": summary.get("error_code"),
        "engine_sha256": summary.get("engine_sha256"),
        "elapsed_s": summary.get("elapsed_s"),
        "analytics": summary.get("analytics"),
    }


def run_seed_with_retry(seed: int, **options: Any) -> dict[str, Any]:
    """Retry only transport/lease failures on a fresh owned engine, never a controller result."""
    result = run_seed(seed, **options)
    attempts = 1
    while attempts < MAX_ATTEMPTS and result.get("error_code") in RETRYABLE_ERRORS:
        result = run_seed(seed, **options)
        attempts += 1
    return {**result, "attempts": attempts}


def summarize(results: list[dict[str, Any]], threshold: float | None) -> dict[str, Any]:
    points = [row["points"] for row in results if row["points"] is not None]
    below = [
        row["seed"]
        for row in results
        if row["points"] is None or (threshold is not None and row["points"] < threshold)
    ]
    failed = [row["seed"] for row in results if not row["native_success"]]
    damaged = [
        row["seed"]
        for row in results
        if ((row.get("measurement") or {}).get("damage_j") or 0.0) > 0.0
    ]
    negative = [row["seed"] for row in results if row["points"] is not None and row["points"] < 0.0]
    return {
        "min_points": min(points) if points else None,
        "mean_points": sum(points) / len(points) if points else None,
        "max_points": max(points) if points else None,
        "successes": len(results) - len(failed),
        "failures": len(failed),
        "below_threshold": below,
        "failed_seeds": failed,
        "damaged_seeds": damaged,
        "negative_seeds": negative,
        "retried_seeds": [row["seed"] for row in results if row.get("attempts", 1) > 1],
        "all_passed": not below and not failed,
    }


def run_fuzz(
    seeds: tuple[int, ...],
    *,
    world_id: str,
    profile_id: str,
    duration_s: float,
    threshold: float | None,
    workers: int,
    out: Path | None,
    playback_rate: int = 1,
    settings: dict[str, Any] | None = None,
    progress: bool = True,
) -> dict[str, Any]:
    """Run seeds in isolated processes and return the complete report.

    Each seed owns a controller loop and an isolated native server. Threads
    shared one GIL; at accelerated playback a Python scheduling stall could
    outlive the fenced command lease and surface as a transport failure. A
    process per seed preserves the controller timing contract without changing
    the controller or the native lease.
    """
    results: list[dict[str, Any]] = []
    wall_budget_s = duration_s / playback_rate + 120.0
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=multiprocessing.get_context("forkserver")
    ) as pool:
        futures = {
            pool.submit(
                run_seed_with_retry,
                seed,
                world_id=world_id,
                profile_id=profile_id,
                duration_s=duration_s,
                wall_budget_s=wall_budget_s,
                playback_rate=playback_rate,
                settings=settings,
            ): seed
            for seed in seeds
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if progress:
                print(format_row(result), file=sys.stderr, flush=True)
    results.sort(key=lambda row: row["seed"])
    report = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "world": world_id,
        "profile": profile_id,
        "duration_s": duration_s,
        "threshold_points": threshold,
        "settings": settings,
        "workers": workers,
        "playback_rate": playback_rate,
        "seeds": list(seeds),
        "runs": results,
        "summary": summarize(results, threshold),
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def _text(value: Any, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return str(value)


def format_row(row: dict[str, Any]) -> str:
    measurement = row.get("measurement") or {}
    return (
        f"{row['seed']:>4}  {str(row.get('state')):<18}  {_text(row.get('points'), 2):>7}  "
        f"{_text(measurement.get('time_s'), 1):>7}  "
        f"{_text(measurement.get('fuel_used_kg'), 2):>7}  "
        f"{_text(measurement.get('damage_j'), 0):>8}  {_text(measurement.get('range_m'), 1):>7}  "
        f"{_text(measurement.get('relative_speed_mps'), 2):>9}  {row.get('error_code') or '-'}"
    )


def format_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    threshold = report.get("threshold_points")
    lines = [
        "seed  state               points   time_s   fuel_kg  damage_j   range_m  speed_mps  error",
        *[format_row(row) for row in report["runs"]],
        "",
        (
            f"points min/mean/max: {_text(summary['min_points'], 2)} / "
            f"{_text(summary['mean_points'], 2)} / {_text(summary['max_points'], 2)} "
            + (f"(threshold {threshold:.2f})" if threshold is not None else "(no fixed threshold)")
        ),
        f"successes {summary['successes']}, failures {summary['failures']}",
        f"below threshold: {summary['below_threshold'] or 'none'}",
        f"failed seeds: {summary['failed_seeds'] or 'none'}",
        f"damaged seeds: {summary.get('damaged_seeds', []) or 'none'}",
        f"negative points: {summary.get('negative_seeds', []) or 'none'}",
        f"retried seeds: {summary['retried_seeds'] or 'none'}",
    ]
    return "\n".join(lines)


def _default_out(world_id: str, profile_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("runs") / f"fuzz-{profile_id}-{world_id}-{stamp}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wsorb fuzz",
        description="Run reviewed controller profiles across seeds on owned native engines.",
    )
    parser.add_argument("--seeds", default="100-107", help="uint32 seeds, e.g. 100-107")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--world", choices=WORLD_IDS, default="official")
    parser.add_argument("--profile", choices=PROFILE_IDS, default="ali_mppi")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument(
        "--playback-rate",
        type=int,
        choices=(1, 2, 4),
        default=1,
        help="simulation speed for owned native runs",
    )
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument(
        "--settings",
        type=json.loads,
        default=None,
        help="JSON object of controller policy settings",
    )
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    try:
        seeds = parse_seeds(args.seeds)
        if not 1 <= args.workers <= 16:
            raise ValueError("workers must be between 1 and 16")
        if not 0.0 < args.duration <= 1800.0:
            raise ValueError("duration must be between 0 and 1800 seconds")
        if args.threshold is not None and not math.isfinite(args.threshold):
            raise ValueError("threshold must be finite")
        if args.settings is not None and not isinstance(args.settings, dict):
            raise ValueError("settings must be a JSON object")
        out = args.out if args.out is not None else _default_out(args.world, args.profile)
        report = run_fuzz(
            seeds,
            world_id=args.world,
            profile_id=args.profile,
            duration_s=args.duration,
            threshold=args.threshold,
            workers=args.workers,
            out=out,
            playback_rate=args.playback_rate,
            settings=args.settings,
        )
    except (ValueError, OSError) as error:
        print(f"wsorb fuzz: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        print(format_report(report))
        print(f"wrote {out}")
    return 0 if report["summary"]["all_passed"] else 1
