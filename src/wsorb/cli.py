"""Command line interface for the campaign lab."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from wsorb import __version__
from wsorb.campaign import (
    STRATEGIES,
    load_campaign,
    resolve_strategies,
    run_campaign,
    write_campaign,
)
from wsorb.geometry import Vec2
from wsorb.lab import lab_port, launch_lab
from wsorb.notebook import add_entry, list_entries, write_index
from wsorb.report import (
    format_duration,
    render_run_markdown,
    render_summary_table,
)
from wsorb.scenario import list_builtin_scenarios, load_scenario
from wsorb.simulation import run_simulation


def _add_scenario_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario",
        default="lunar_sar",
        help="built-in scenario id or path to a TOML scenario (default: lunar_sar)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wsorb",
        description="Western Sydney Orbital — Space Teams PRO campaign lab",
    )
    parser.add_argument("--version", action="version", version=f"wsorb {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scenario = subparsers.add_parser("scenario", help="inspect mission scenarios")
    scenario_sub = scenario.add_subparsers(dest="scenario_command", required=True)
    scenario_sub.add_parser("list", help="list built-in scenarios")
    show = scenario_sub.add_parser("show", help="show a scenario")
    _add_scenario_arg(show)
    show.add_argument("--json", action="store_true", help="emit JSON")

    sim = subparsers.add_parser("sim", help="run one simulated mission")
    sim_sub = sim.add_subparsers(dest="sim_command", required=True)
    run = sim_sub.add_parser("run", help="run one mission")
    _add_scenario_arg(run)
    run.add_argument(
        "--strategy",
        default="sector-baseline",
        choices=sorted(STRATEGIES),
        help="policy preset",
    )
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--terrain-seed", type=int, default=None)
    run.add_argument("--target-x", type=float, default=None)
    run.add_argument("--target-y", type=float, default=None)
    run.add_argument("--json", action="store_true", help="emit the full result as JSON")
    run.add_argument("--summary-out", type=Path, default=None, help="write a Markdown report")
    run.add_argument(
        "--plan-out",
        type=Path,
        default=None,
        help="export the practice model's asset waypoints",
    )

    campaign = subparsers.add_parser("campaign", help="run a paired Monte Carlo campaign")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_run = campaign_sub.add_parser("run", help="run strategies on shared draws")
    _add_scenario_arg(campaign_run)
    campaign_run.add_argument("--replicates", type=int, default=24)
    campaign_run.add_argument("--seed", type=int, default=42)
    campaign_run.add_argument(
        "--strategies",
        default="all",
        help="comma-separated preset names, or 'all'",
    )
    campaign_run.add_argument("--out", type=Path, default=Path("runs"))
    campaign_run.add_argument("--notebook", action="store_true", help="record a notebook entry")
    campaign_run.add_argument("--author", default="Project team")
    campaign_run.add_argument("--title", default=None)
    campaign_run.add_argument("--json", action="store_true", help="emit the campaign JSON")

    campaign_show = campaign_sub.add_parser("show", help="show a saved campaign")
    campaign_show.add_argument("path", type=Path, help="path to campaign.json or run directory")

    lab = subparsers.add_parser("lab", help="start the private local Marimo editor")
    lab.add_argument(
        "--port",
        type=lab_port,
        default=None,
        help="override WSORB_LAB_PORT (default: 2718); use the same port for the game server",
    )

    notebook = subparsers.add_parser("notebook", help="work with the shared mission notebook")
    notebook_sub = notebook.add_subparsers(dest="notebook_command", required=True)
    nb_add = notebook_sub.add_parser("add", help="add an entry")
    nb_add.add_argument("--title", required=True)
    nb_add.add_argument("--author", required=True)
    nb_add.add_argument("--kind", default="note")
    nb_add.add_argument("--tags", default="", help="comma-separated tags")
    nb_add.add_argument("--run", default=None, help="relative path to a run directory")
    nb_add.add_argument("--body", default="")
    nb_add.add_argument("--body-file", type=Path, default=None)
    nb_add.add_argument("--notebook-dir", type=Path, default=Path("notebook"))
    nb_index = notebook_sub.add_parser("index", help="regenerate NOTEBOOK.md")
    nb_index.add_argument("--notebook-dir", type=Path, default=Path("notebook"))
    nb_list = notebook_sub.add_parser("list", help="list entries")
    nb_list.add_argument("--notebook-dir", type=Path, default=Path("notebook"))

    return parser


def _scenario_from_args(args: argparse.Namespace):
    return load_scenario(args.scenario)


def _target_from_args(args: argparse.Namespace) -> Vec2 | None:
    if (args.target_x is None) != (args.target_y is None):
        raise ValueError("provide both --target-x and --target-y, or neither")
    if args.target_x is None:
        return None
    return Vec2(args.target_x, args.target_y)


def _relative_run(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _cmd_scenario(args: argparse.Namespace) -> int:
    if args.scenario_command == "list":
        for name in list_builtin_scenarios():
            scenario = load_scenario(name)
            print(f"{name:18s} {scenario.name}")
        return 0

    scenario = _scenario_from_args(args)
    if args.json:
        print(json.dumps(scenario.to_dict(), indent=2))
        return 0
    print(f"{scenario.name} (`{scenario.id}`)")
    print(scenario.description.strip())
    print()
    print(
        f"Search box: x [{scenario.bounds.min_x:.0f}, {scenario.bounds.max_x:.0f}], "
        f"y [{scenario.bounds.min_y:.0f}, {scenario.bounds.max_y:.0f}] m"
    )
    print(
        f"Horizon: {format_duration(scenario.horizon_s)} | step: {scenario.dt_s:.0f}s | "
        f"charger bays: {scenario.charger_bays}"
    )
    print()
    print("Assets:")
    for asset in scenario.assets:
        print(
            f"  {asset.name:15s} {asset.kind:10s} speed={asset.max_speed_mps:.1f} m/s  "
            f"battery={asset.battery_kwh:.1f} kWh  sensor={asset.sensor_radius_m:.0f} m"
        )
    return 0


def _cmd_sim_run(args: argparse.Namespace) -> int:
    scenario = _scenario_from_args(args)
    strategy = STRATEGIES[args.strategy]
    result = run_simulation(
        scenario,
        strategy,
        seed=args.seed,
        target=_target_from_args(args),
        terrain_seed=args.terrain_seed,
    )
    if args.json:
        print(json.dumps(result.to_dict(include_plans=True), indent=2))
    else:
        print(result.summary_line())
        print(
            f"  target=({result.target.x:.0f}, {result.target.y:.0f})  "
            f"coverage={result.coverage_fraction:.1%}  "
            f"comms={result.comms_fraction:.1%}  "
            f"energy={result.energy_kwh:.2f} kWh  "
            f"wait={format_duration(result.charger_wait_s)}"
        )
        if result.stranded_assets:
            print(f"  stranded: {', '.join(result.stranded_assets)}")
        if result.relay_completed and result.relay_position:
            print(
                f"  relay online at ({result.relay_position.x:.0f}, {result.relay_position.y:.0f})"
            )

    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(render_run_markdown(result), encoding="utf-8")
        print(f"report: {args.summary_out}", file=sys.stderr if args.json else sys.stdout)

    if args.plan_out:
        args.plan_out.parent.mkdir(parents=True, exist_ok=True)
        plan = {
            "scenario": result.scenario_id,
            "strategy": result.strategy_name,
            "seed": result.seed,
            "terrain_seed": result.terrain_seed,
            "relay": {
                "asset": result.relay_asset,
                "position": (
                    [result.relay_position.x, result.relay_position.y]
                    if result.relay_position
                    else None
                ),
                "completed": result.relay_completed,
            },
            "assets": {
                name: [[round(point.x, 2), round(point.y, 2)] for point in route]
                for name, route in result.asset_plans.items()
            },
        }
        args.plan_out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(f"plan: {args.plan_out}", file=sys.stderr if args.json else sys.stdout)
    return 0 if result.success else 1


def _cmd_campaign_run(args: argparse.Namespace) -> int:
    scenario = _scenario_from_args(args)
    names = None if args.strategies == "all" else args.strategies.split(",")
    strategies = resolve_strategies(names)
    result = run_campaign(
        scenario,
        strategies,
        replicates=args.replicates,
        seed=args.seed,
        command=args.invocation,
    )
    run_dir = write_campaign(result, args.out)
    if args.json:
        print(json.dumps(result.to_dict(include_runs=False), indent=2))
    else:
        print(render_summary_table(result.summaries))
        print()
        print(f"artefacts: {run_dir}")

    if args.notebook:
        relative = _relative_run(run_dir)
        title = args.title or f"Campaign {result.campaign_id}"
        best = result.summaries[0] if result.summaries else None
        body = [
            f"Paired campaign over {result.replicates} terrain/target draws.",
            "",
        ]
        if best is not None:
            body.append(
                f"Top result in this practice sample: `{best.strategy_name}`, "
                f"{best.successes}/{best.runs} successes, median report "
                f"{format_duration(best.median_report_s)}, coverage "
                f"{best.mean_coverage:.1%}."
            )
            body.append("")
        body.append(render_summary_table(result.summaries))
        body.append("")
        body.append(f"Raw data: `{relative}/campaign.json`, `{relative}/results.csv`.")
        entry = add_entry(
            "notebook",
            title=title,
            author=args.author,
            kind="campaign",
            tags=["campaign", scenario.id],
            body="\n".join(body),
            run=relative,
        )
        index = write_index("notebook")
        print(f"notebook: {entry} (index: {index})", file=sys.stderr if args.json else sys.stdout)
    return 0


def _cmd_campaign_show(args: argparse.Namespace) -> int:
    path = args.path
    if path.is_dir():
        path = path / "campaign.json"
    payload = load_campaign(path)
    summaries = payload.get("summaries", [])
    print(f"Campaign {payload.get('campaign_id')} — {payload.get('scenario', {}).get('name')}")
    print()
    print(
        "| Rank | Strategy | Success | Median | Coverage | Min SOC |\n"
        "| ---: | --- | ---: | ---: | ---: | ---: |"
    )
    for rank, item in enumerate(summaries, start=1):
        print(
            f"| {rank} | {item['strategy']} | {item['successes']}/{item['runs']} | "
            f"{format_duration(item['median_report_s'])} | "
            f"{item['mean_coverage']:.1%} | {item['mean_min_soc']:.1%} |"
        )
    return 0


def _cmd_notebook(args: argparse.Namespace) -> int:
    notebook_dir = args.notebook_dir
    if args.notebook_command == "index":
        path = write_index(notebook_dir)
        print(f"wrote {path}")
        return 0
    if args.notebook_command == "list":
        entries = list_entries(notebook_dir)
        if not entries:
            print("no entries")
            return 0
        for entry in entries:
            print(
                f"{entry.created:25s} {entry.kind:10s} {entry.author:20s} "
                f"{entry.title} ({entry.path.name})"
            )
        return 0

    body = args.body
    if args.body_file:
        body = args.body_file.read_text(encoding="utf-8")
    path = add_entry(
        notebook_dir,
        title=args.title,
        author=args.author,
        kind=args.kind,
        tags=[tag for tag in args.tags.split(",") if tag.strip()],
        body=body,
        run=args.run,
    )
    index = write_index(notebook_dir)
    print(f"entry: {path}")
    print(f"index: {index}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.invocation = shlex.join(
        ["uv", "run", "--locked", "wsorb", *(sys.argv[1:] if argv is None else argv)]
    )
    try:
        if args.command == "scenario":
            return _cmd_scenario(args)
        if args.command == "sim":
            return _cmd_sim_run(args)
        if args.command == "campaign":
            if args.campaign_command == "run":
                return _cmd_campaign_run(args)
            return _cmd_campaign_show(args)
        if args.command == "notebook":
            return _cmd_notebook(args)
        if args.command == "lab":
            launch_lab(port=args.port)
    except (ValueError, KeyError, OSError) as exc:
        print(f"wsorb: {exc}", file=sys.stderr)
        return 2
    parser.error("unknown command")
    return 2
