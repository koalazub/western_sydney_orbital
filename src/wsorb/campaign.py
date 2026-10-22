"""Monte Carlo campaign runner with common random numbers."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from wsorb.model import CampaignResult, RunResult, Scenario, Strategy, StrategySummary
from wsorb.report import render_campaign_markdown
from wsorb.simulation import build_scenario_terrain, run_simulation, sample_target

STRATEGIES: dict[str, Strategy] = {
    "sector-baseline": Strategy(
        name="sector-baseline",
        pattern="sector",
        recharge_reserve=0.20,
        description="Even sectors, 20% reserve, no relay.",
    ),
    "sweep-baseline": Strategy(
        name="sweep-baseline",
        pattern="sweep",
        recharge_reserve=0.20,
        description="Contiguous bands, 20% reserve, no relay.",
    ),
    "nearest-baseline": Strategy(
        name="nearest-baseline",
        pattern="nearest",
        recharge_reserve=0.20,
        description="Greedy nearest-workload assignment, no relay.",
    ),
    "sector-conservative": Strategy(
        name="sector-conservative",
        pattern="sector",
        recharge_reserve=0.35,
        description="Even sectors, 35% reserve, no relay.",
    ),
    "sweep-conservative": Strategy(
        name="sweep-conservative",
        pattern="sweep",
        recharge_reserve=0.35,
        description="Contiguous bands, 35% reserve, no relay.",
    ),
    "sector-relay": Strategy(
        name="sector-relay",
        pattern="sector",
        recharge_reserve=0.20,
        deploy_relay=True,
        description="Even sectors, 20% reserve, redeploy ANT-1 to the best relay site.",
    ),
    "sweep-relay": Strategy(
        name="sweep-relay",
        pattern="sweep",
        recharge_reserve=0.20,
        deploy_relay=True,
        description="Contiguous bands, 20% reserve, redeploy ANT-1.",
    ),
}


def resolve_strategies(names: Iterable[str] | None) -> list[Strategy]:
    if names is None:
        return list(STRATEGIES.values())
    resolved: list[Strategy] = []
    for name in names:
        key = name.strip()
        if key == "all":
            return list(STRATEGIES.values())
        if key not in STRATEGIES:
            available = ", ".join(STRATEGIES)
            raise ValueError(f"unknown strategy {key!r}; available: {available}")
        resolved.append(STRATEGIES[key])
    if not resolved:
        raise ValueError("at least one strategy is required")
    return resolved


def _campaign_id(scenario: Scenario, strategies: list[Strategy], replicates: int, seed: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = json.dumps(
        {
            "scenario": scenario.id,
            "strategies": [strategy.to_dict() for strategy in strategies],
            "replicates": replicates,
            "seed": seed,
        },
        sort_keys=True,
    ).encode()
    return f"{stamp}-{hashlib.sha256(payload).hexdigest()[:8]}"


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[index]


def summarize(results: list[RunResult]) -> list[StrategySummary]:
    grouped: dict[str, list[RunResult]] = {}
    for result in results:
        grouped.setdefault(result.strategy_name, []).append(result)

    summaries: list[StrategySummary] = []
    for name, runs in grouped.items():
        reports = [
            run.reported_at_s for run in runs if run.success and run.reported_at_s is not None
        ]
        summaries.append(
            StrategySummary(
                strategy_name=name,
                runs=len(runs),
                successes=len(reports),
                success_rate=len(reports) / len(runs) if runs else 0.0,
                median_report_s=statistics.median(reports) if reports else None,
                p90_report_s=_p90(reports),
                mean_coverage=statistics.fmean(run.coverage_fraction for run in runs)
                if runs
                else 0.0,
                mean_min_soc=statistics.fmean(run.min_soc for run in runs) if runs else 0.0,
                stranded_rate=statistics.fmean(1.0 if run.stranded_assets else 0.0 for run in runs)
                if runs
                else 0.0,
                mean_energy_kwh=statistics.fmean(run.energy_kwh for run in runs) if runs else 0.0,
                mean_comms_fraction=statistics.fmean(run.comms_fraction for run in runs)
                if runs
                else 0.0,
            )
        )
    summaries.sort(key=lambda summary: summary.score, reverse=True)
    return summaries


def run_campaign(
    scenario: Scenario,
    strategies: list[Strategy],
    *,
    replicates: int = 24,
    seed: int = 42,
    campaign_id: str | None = None,
    command: str = "",
) -> CampaignResult:
    """Run every strategy against the same terrain/target draws."""

    if replicates < 1:
        raise ValueError("replicates must be at least 1")
    if not strategies:
        raise ValueError("at least one strategy is required")

    runs: list[RunResult] = []
    for replicate in range(replicates):
        rng = random.Random(seed + replicate)
        terrain_seed = rng.randrange(1, 2**31 - 1)
        terrain = build_scenario_terrain(scenario, terrain_seed)
        target = sample_target(scenario, terrain, rng)
        for strategy in strategies:
            runs.append(
                run_simulation(
                    scenario,
                    strategy,
                    seed=seed + replicate,
                    target=target,
                    terrain=terrain,
                    terrain_seed=terrain_seed,
                )
            )

    return CampaignResult(
        campaign_id=campaign_id or _campaign_id(scenario, strategies, replicates, seed),
        scenario=scenario,
        seed=seed,
        replicates=replicates,
        strategies=tuple(strategies),
        runs=tuple(runs),
        summaries=tuple(summarize(runs)),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        command=command,
    )


def write_campaign(result: CampaignResult, out_dir: str | Path) -> Path:
    """Write JSON, CSV and Markdown artefacts. Returns the run directory."""

    run_dir = Path(out_dir) / result.campaign_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "campaign.json").write_text(
        json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "summary.md").write_text(render_campaign_markdown(result), encoding="utf-8")

    fieldnames = [
        "strategy",
        "replicate",
        "seed",
        "terrain_seed",
        "target_x",
        "target_y",
        "found",
        "success",
        "detected_at_s",
        "reported_at_s",
        "coverage_fraction",
        "comms_fraction",
        "min_soc",
        "energy_kwh",
        "stranded_assets",
        "charger_wait_s",
        "relay_completed",
    ]
    with (run_dir / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, run in enumerate(result.runs):
            writer.writerow(
                {
                    "strategy": run.strategy_name,
                    "replicate": index // len(result.strategies),
                    "seed": run.seed,
                    "terrain_seed": run.terrain_seed,
                    "target_x": round(run.target.x, 2),
                    "target_y": round(run.target.y, 2),
                    "found": run.found,
                    "success": run.success,
                    "detected_at_s": run.detected_at_s,
                    "reported_at_s": run.reported_at_s,
                    "coverage_fraction": round(run.coverage_fraction, 4),
                    "comms_fraction": round(run.comms_fraction, 4),
                    "min_soc": round(run.min_soc, 4),
                    "energy_kwh": round(run.energy_kwh, 3),
                    "stranded_assets": ";".join(run.stranded_assets),
                    "charger_wait_s": round(run.charger_wait_s, 1),
                    "relay_completed": run.relay_completed,
                }
            )
    return run_dir


def load_campaign(path: str | Path) -> dict:
    """Load a campaign.json artefact for inspection."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "summaries" not in payload or "runs" not in payload:
        raise ValueError(f"{path} is not a campaign artefact")
    return payload
