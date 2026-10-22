"""Tests for scenario loading, validation and serialisation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from wsorb.geometry import Vec2
from wsorb.scenario import (
    ScenarioError,
    default_scenario,
    list_builtin_scenarios,
    load_scenario,
)

MINIMAL_SCENARIO = """
id = "minimal"
name = "Minimal"
description = "A tiny scenario"
terrain_seed = 1
terrain_resolution_m = 20.0
horizon_s = 600.0
dt_s = 10.0
charger_bays = 1
search_spacing_factor = 1.5
comms_check_interval_s = 30.0

[bounds]
min_x = 0.0
max_x = 100.0
min_y = 0.0
max_y = 100.0

[charger]
x = 50.0
y = 50.0

[[assets]]
name = "Rover"
kind = "ltv"
max_speed_mps = 2.0
battery_kwh = 2.0
move_power_kw = 1.0
idle_power_kw = 0.05
sensor_radius_m = 20.0
charge_rate_kw = 1.0
max_slope_deg = 25.0
start = { x = 10.0, y = 10.0 }

[[antennas]]
name = "ANT-1"
x = 90.0
y = 90.0
height_m = 15.0
"""


def test_default_scenario_matches_the_reference_mission() -> None:
    scenario = default_scenario()

    assert scenario.id == "lunar_sar"
    assert scenario.name.startswith("Shackleton")
    assert len(scenario.assets) == 7
    assert len(scenario.antennas) == 3
    assert scenario.bounds.width > 1500.0
    assert scenario.world_bounds.width == pytest.approx(scenario.bounds.width + 1000.0)
    assert scenario.search_area_m2 == pytest.approx(scenario.bounds.width * scenario.bounds.height)
    assert scenario.relay_capable_asset() is not None
    assert scenario.relay_capable_asset().name == "LTV1"  # type: ignore[union-attr]


def test_load_scenario_by_id_and_by_path(tmp_path: Path) -> None:
    by_id = load_scenario("lunar_sar")
    path = tmp_path / "custom.toml"
    path.write_text(MINIMAL_SCENARIO, encoding="utf-8")
    by_path = load_scenario(path)

    assert by_id.id == "lunar_sar"
    assert by_path.id == "minimal"
    assert by_path.asset("Rover").battery_kwh == 2.0
    assert by_path.antenna("ANT-1").height_m == 15.0


def test_list_builtin_scenarios() -> None:
    assert "lunar_sar" in list_builtin_scenarios()


def test_unknown_scenario_lists_builtins() -> None:
    with pytest.raises(ScenarioError, match="unknown scenario"):
        load_scenario("not-a-real-scenario")


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[bounds\n", encoding="utf-8")

    with pytest.raises(ScenarioError, match="invalid TOML"):
        load_scenario(path)


def test_missing_required_key_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"
    path.write_text('id = "missing"\n', encoding="utf-8")

    with pytest.raises(ScenarioError, match="missing required key"):
        load_scenario(path)


def test_to_dict_round_trip_contains_scenario_geometry() -> None:
    scenario = default_scenario()
    payload = scenario.to_dict()

    assert payload["id"] == "lunar_sar"
    assert payload["bounds"]["max_x"] == pytest.approx(1199.211)
    assert payload["charger"] == (-250.0, -1060.0)
    assert payload["world_margin_m"] == scenario.world_margin_m
    assert payload["search_spacing_factor"] == scenario.search_spacing_factor
    assert len(payload["assets"]) == 7
    assert len(payload["antennas"]) == 3
    assert len(payload["obstacles"]) == 5
    assert payload["relay_source_antenna"] == "ANT-1"


def test_scenario_rejects_invalid_combinations() -> None:
    scenario = default_scenario()

    with pytest.raises(ValueError, match="at least one asset"):
        replace(scenario, assets=())

    with pytest.raises(ValueError, match="asset names must be unique"):
        replace(scenario, assets=(scenario.assets[0], scenario.assets[0]))

    with pytest.raises(ValueError, match="charger must lie inside"):
        replace(scenario, charger=Vec2(10_000.0, 10_000.0))

    with pytest.raises(ValueError, match="relay_source_antenna"):
        replace(scenario, relay_source_antenna="NOPE")

    no_carrier = tuple(replace(asset, can_carry_antenna=False) for asset in scenario.assets)
    with pytest.raises(ValueError, match="antenna-capable asset"):
        replace(scenario, assets=no_carrier)

    with pytest.raises(ValueError, match="dt_s cannot exceed"):
        replace(scenario, horizon_s=10.0, dt_s=20.0)

    with pytest.raises(ValueError, match="charger_bays"):
        replace(scenario, charger_bays=0)

    with pytest.raises(ValueError, match="search_spacing_factor"):
        replace(scenario, search_spacing_factor=0.0)

    with pytest.raises(ValueError, match="comms_check_interval_s"):
        replace(scenario, comms_check_interval_s=0.0)

    with pytest.raises(ValueError, match="antenna names must be unique"):
        replace(scenario, antennas=(scenario.antennas[0], scenario.antennas[0]))
