from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from wsorb.campaign import STRATEGIES
from wsorb.geometry import Vec2
from wsorb.model import Strategy
from wsorb.planner import SearchPlan
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


def test_low_battery_limits_movement_and_energy_to_available_charge() -> None:
    scenario = default_scenario()
    simulation = Simulation(scenario, STRATEGIES["sector-baseline"], seed=5)
    asset = simulation.assets["LTV1"]
    asset.position = scenario.charger
    asset.spec = replace(asset.spec, battery_kwh=0.01)
    asset.soc = 0.01
    asset.path = [Vec2(scenario.charger.x + 100, scenario.charger.y)]
    available_kwh = asset.spec.battery_kwh * asset.soc
    requested_dt = 15.0
    requested_distance = simulation._speed(asset) * requested_dt

    simulation._advance(asset, requested_dt)

    assert asset.stranded
    assert asset.status == "stranded"
    assert asset.soc == 0.0
    assert asset.min_soc == 0.0
    assert asset.energy_kwh == pytest.approx(available_kwh)
    assert asset.energy_kwh <= available_kwh
    assert 0 < asset.distance_m < requested_distance
    assert asset.position.distance_to(scenario.charger) == pytest.approx(asset.distance_m)


def test_zero_duration_does_not_move_or_consume_energy() -> None:
    simulation = Simulation(default_scenario(), STRATEGIES["sector-baseline"], seed=5)
    asset = simulation.assets["LTV1"]
    start = asset.position
    asset.path = [Vec2(start.x + 100, start.y)]

    simulation._advance(asset, 0.0)

    assert asset.position == start
    assert asset.distance_m == 0.0
    assert asset.energy_kwh == 0.0
    assert not asset.stranded


def test_stranded_asset_cannot_be_returned_to_charging() -> None:
    simulation = Simulation(default_scenario(), STRATEGIES["sector-baseline"], seed=5)
    asset = simulation.assets["LTV1"]
    asset.soc = 0.0
    simulation._strand(asset)
    simulation._request_charger(asset)
    assert asset.status == "stranded"
    assert asset.name not in simulation.charger.slots
    assert asset.name not in simulation.charger.queue


def test_waypoint_at_current_position_only_draws_idle_power() -> None:
    simulation = Simulation(default_scenario(), STRATEGIES["sector-baseline"], seed=5)
    asset = simulation.assets["LTV1"]
    asset.path = [asset.position]
    assert not simulation._advance(asset, 15)
    assert asset.energy_kwh == pytest.approx(asset.spec.idle_power_kw * 15 / 3600)


def test_relay_pickup_movement_and_placement_complete(monkeypatch) -> None:
    original = default_scenario()
    asset = original.assets[0]
    source = replace(original.antennas[0], position=asset.start)
    destination = Vec2(asset.start.x + 10, asset.start.y)
    scenario = replace(
        original, assets=(asset,), antennas=(source, *original.antennas[1:]), horizon_s=600
    )
    plan = SearchPlan(
        routes={asset.name: []},
        relay_asset=asset.name,
        relay_position=destination,
        relay_source=source,
    )
    monkeypatch.setattr("wsorb.simulation.build_search_plan", lambda *args: plan)
    simulation = Simulation(
        scenario, Strategy("relay", deploy_relay=True), target=scenario.bounds.center
    )
    result = simulation.run()
    assert result.relay_completed
    assert simulation.assets[asset.name].relay_done
    assert source.name not in {antenna.name for antenna in simulation.active_antennas}
    assert any(antenna.position == destination for antenna in simulation.active_antennas)
    assert any(event.message.endswith("relay online") for event in result.events)


def test_detection_without_comms_is_found_but_not_successful() -> None:
    simulation = Simulation(
        default_scenario(), STRATEGIES["sector-baseline"], seed=5, target=Vec2(0, 0)
    )
    asset = simulation.assets["LTV1"]
    asset.position = simulation.target
    asset.comms = False

    simulation._check_target(asset, 10.0)
    result = simulation._result()

    assert result.detected_at_s == 10.0
    assert result.found
    assert result.reported_at_s is None
    assert not result.success


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
