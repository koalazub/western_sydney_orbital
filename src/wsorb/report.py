"""Markdown reports for runs and campaigns."""

from __future__ import annotations

from wsorb.model import CampaignResult, RunResult, Strategy, StrategySummary


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_summary_table(summaries: tuple[StrategySummary, ...]) -> str:
    header = (
        "| Rank | Strategy | Success | Median report | P90 report | Coverage | "
        "Min SOC | Stranded | Energy | Comms |\n"
        "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    )
    rows = []
    for rank, summary in enumerate(summaries, start=1):
        rows.append(
            "| "
            + " | ".join(
                [
                    str(rank),
                    f"`{summary.strategy_name}`",
                    f"{summary.successes}/{summary.runs}",
                    format_duration(summary.median_report_s),
                    format_duration(summary.p90_report_s),
                    _percent(summary.mean_coverage),
                    _percent(summary.mean_min_soc),
                    _percent(summary.stranded_rate),
                    f"{summary.mean_energy_kwh:.2f} kWh",
                    _percent(summary.mean_comms_fraction),
                ]
            )
            + " |"
        )
    return header + "\n" + "\n".join(rows)


def render_strategy_table(strategies: tuple[Strategy, ...]) -> str:
    header = (
        "| Strategy | Pattern | Reserve | Relay | Speed | Description |\n"
        "| --- | --- | ---: | :---: | ---: | --- |"
    )
    rows = [
        f"| `{strategy.name}` | {strategy.pattern} | {strategy.recharge_reserve:.0%} | "
        f"{'yes' if strategy.deploy_relay else 'no'} | {strategy.speed_policy:.0%} | "
        f"{strategy.description} |"
        for strategy in strategies
    ]
    return header + "\n" + "\n".join(rows)


def render_campaign_markdown(result: CampaignResult) -> str:
    scenario = result.scenario
    lines = [
        f"# Campaign `{result.campaign_id}`",
        "",
        f"**Scenario:** {scenario.name} (`{scenario.id}`)  ",
        f"**Generated:** {result.created_at}  ",
        f"**Replicates:** {result.replicates}  ",
        f"**Master seed:** {result.seed}",
        "",
        "Each replicate draws one terrain and one target, then runs every strategy on that",
        "same draw. This is a paired comparison; differences come from the policy, not luck.",
        "",
        "## Ranking",
        "",
        render_summary_table(result.summaries),
        "",
        "Ranking is success rate first, then median time-to-report, then coverage.",
        "",
        "## Strategies",
        "",
        render_strategy_table(result.strategies),
        "",
        "## Mission geometry",
        "",
        f"- Search box: x [{scenario.bounds.min_x:.0f}, {scenario.bounds.max_x:.0f}], "
        f"y [{scenario.bounds.min_y:.0f}, {scenario.bounds.max_y:.0f}] m",
        f"- Assets: {len(scenario.assets)}; antennas: {len(scenario.antennas)}; "
        f"charger bays: {scenario.charger_bays}",
        f"- Horizon: {format_duration(scenario.horizon_s)}; step: {scenario.dt_s:.0f} s",
        f"- Search spacing factor: {scenario.search_spacing_factor:.2f} × sensor radius",
        "",
        "## Reproduce",
        "",
        "```bash",
        "uv run wsorb campaign run \\",
        f"  --scenario {scenario.id} --replicates {result.replicates} --seed {result.seed}",
        "```",
        "",
        "## Scope",
        "",
        "This is a campaign-level surrogate, not Space Teams PRO itself. It models terrain,",
        "slope, battery, charging queues, sensor coverage, line-of-sight comms and reporting",
        "delay. It does not model rigid-body dynamics, wheel slip, thermal limits or the",
        "proprietary STP asset models. Use it to reject weak strategies before spending time",
        "in the high-fidelity simulator.",
        "",
    ]
    if result.command:
        lines.extend(["Command:", "", "```bash", result.command, "```", ""])
    return "\n".join(lines)


def render_run_markdown(result: RunResult) -> str:
    lines = [
        f"# Run: {result.strategy_name}",
        "",
        f"- Scenario: `{result.scenario_id}`",
        f"- Seed: `{result.seed}` (terrain seed `{result.terrain_seed}`)",
        f"- Target: ({result.target.x:.1f}, {result.target.y:.1f}) m",
        f"- Outcome: **{'FOUND' if result.success else 'NOT FOUND'}**",
        f"- Detected: {format_duration(result.detected_at_s)}",
        f"- Reported: {format_duration(result.reported_at_s)}",
        f"- Coverage: {_percent(result.coverage_fraction)}",
        f"- Comms availability: {_percent(result.comms_fraction)}",
        f"- Minimum SOC: {_percent(result.min_soc)}",
        f"- Energy used: {result.energy_kwh:.2f} kWh",
        f"- Charger wait: {format_duration(result.charger_wait_s)}",
        f"- Relay: {'yes' if result.relay_completed else 'no'}"
        + (
            f" at ({result.relay_position.x:.0f}, {result.relay_position.y:.0f})"
            if result.relay_position
            else ""
        ),
        "",
        "## Assets",
        "",
        "| Asset | Kind | Distance | Energy | Final SOC | Min SOC | Comms | Wait | Stranded |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | :---: |",
    ]
    for stats in result.asset_stats:
        lines.append(
            f"| {stats.name} | {stats.kind} | {stats.distance_m / 1000:.2f} km | "
            f"{stats.energy_kwh:.2f} kWh | {stats.final_soc:.0%} | {stats.min_soc:.0%} | "
            f"{stats.comms_fraction:.0%} | {format_duration(stats.charge_wait_s)} | "
            f"{'yes' if stats.stranded else 'no'} |"
        )
    lines.extend(["", "## Event tail", "", "```text"])
    for event in result.events[-25:]:
        stamp = format_duration(event.t_s)
        asset = f" [{event.asset}]" if event.asset else ""
        lines.append(f"{stamp:>9s}{asset} {event.kind}: {event.message}")
    lines.extend(["```", ""])
    return "\n".join(lines)
