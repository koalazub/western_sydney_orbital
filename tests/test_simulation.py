from __future__ import annotations

from dataclasses import replace

import numpy as np

from wsorb.campaign import STRATEGIES
from wsorb.geometry import Vec2
from wsorb.scenario import default_scenario
from wsorb.simulation import Simulation, run_simulation


def test_simulation_is_deterministic_and_min_soc_tracks_minimum() -> None:
    scenario = replace(default_scenario(), horizon_s=120, dt_s=15)
    args = (scenario, STRATEGIES["sector-baseline"])
    first = run_simulation(*args, seed=5, target=Vec2(0, 0))
    second = run_simulation(*args, seed=5, target=Vec2(0, 0))
    assert first.to_dict() == second.to_dict()
    assert first.min_soc == min(item.min_soc for item in first.asset_stats)
    assert first.reported_at_s is None or first.reported_at_s <= scenario.horizon_s


def test_charging_resumes_the_interrupted_goal() -> None:
    scenario = replace(default_scenario(), horizon_s=120, dt_s=15)
    simulation = Simulation(scenario, STRATEGIES["sector-baseline"], seed=5)
    asset = simulation.assets["LTV1"]
    goal = Vec2(0, 0)
    asset.path = [goal]
    asset.soc = 0.1
    simulation._divert_to_charger(asset)
    assert asset.interrupted_goal == goal

    asset.position = scenario.charger
    asset.path = []
    asset.status = "charging"
    asset.soc = 0.899
    simulation.charger.slots.append(asset.name)
    simulation._update_charger(15, 15)

    assert asset.status == "searching"
    assert asset.interrupted_goal is None
    assert asset.path
    assert asset.path[-1] == goal


def test_target_sample_stays_on_a_searchable_grid_cell() -> None:
    import random

    from wsorb.simulation import build_scenario_terrain, sample_target, searchable_mask

    scenario = default_scenario()
    terrain = build_scenario_terrain(scenario, seed=3)
    target = sample_target(scenario, terrain, random.Random(8))
    xs = np.linspace(terrain.bounds.min_x, terrain.bounds.max_x, terrain.shape[1])
    ys = np.linspace(terrain.bounds.min_y, terrain.bounds.max_y, terrain.shape[0])
    ix = min(range(len(xs)), key=lambda index: abs(xs[index] - target.x))
    iy = min(range(len(ys)), key=lambda index: abs(ys[index] - target.y))
    assert searchable_mask(scenario, terrain)[iy, ix]


def test_open_charger_bay_promotes_waiting_asset() -> None:
    scenario = replace(default_scenario(), horizon_s=120, dt_s=15)
    simulation = Simulation(scenario, STRATEGIES["sector-baseline"], seed=5)
    first, waiting = tuple(simulation.assets.values())[:2]
    first.status = "charging"
    waiting.status = "waiting"
    simulation.charger.slots = [first.name]
    simulation.charger.queue = [waiting.name]
    simulation.charger.slots.clear()

    simulation._update_charger(1, 0)

    assert simulation.charger.slots == [waiting.name]
    assert simulation.charger.queue == []
    assert waiting.status == "charging"


def test_deployment_plan_does_not_export_target(tmp_path, monkeypatch) -> None:
    import json

    from wsorb.cli import main

    destination = tmp_path / "plan.json"
    monkeypatch.setattr("wsorb.cli.run_simulation", lambda *args, **kwargs: _result())
    assert main(["sim", "run", "--plan-out", str(destination)]) == 0
    assert "target" not in json.loads(destination.read_text())


def _result():
    from wsorb.model import RunResult

    return RunResult(
        scenario_id="x",
        strategy_name="x",
        seed=0,
        terrain_seed=0,
        target=Vec2(1, 2),
        found=True,
        horizon_s=10,
        coverage_fraction=0,
        comms_fraction=1,
        min_soc=1,
        energy_kwh=0,
        stranded_assets=(),
        charger_wait_s=0,
        detected_at_s=0,
        reported_at_s=0,
        asset_stats=(),
        events=(),
        asset_plans={},
    )
