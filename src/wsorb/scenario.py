"""Load and validate surrogate mission scenarios."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from wsorb.geometry import Bounds, Vec2
from wsorb.model import AntennaSpec, AssetSpec, Scenario
from wsorb.terrain import Obstacle

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"


class ScenarioError(ValueError):
    """Raised when a scenario file is missing or malformed."""


def _require(table: dict[str, Any], key: str, context: str) -> Any:
    if key not in table:
        raise ScenarioError(f"{context}: missing required key {key!r}")
    return table[key]


def _point(table: dict[str, Any], context: str) -> Vec2:
    return Vec2(float(_require(table, "x", context)), float(_require(table, "y", context)))


def _bounds(table: dict[str, Any], context: str) -> Bounds:
    return Bounds(
        min_x=float(_require(table, "min_x", context)),
        max_x=float(_require(table, "max_x", context)),
        min_y=float(_require(table, "min_y", context)),
        max_y=float(_require(table, "max_y", context)),
    )


def load_scenario(path_or_id: str | Path) -> Scenario:
    """Load a TOML scenario from a path, or a built-in scenario by id."""

    path = Path(path_or_id)
    if not path.exists():
        candidate = DATA_DIR / f"{path_or_id}.toml"
        if candidate.exists():
            path = candidate
        else:
            available = ", ".join(sorted(item.stem for item in DATA_DIR.glob("*.toml")))
            raise ScenarioError(
                f"unknown scenario {path_or_id!r}; available built-ins: {available or 'none'}"
            )

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ScenarioError(f"{path}: invalid TOML: {exc}") from exc

    try:
        return _parse_scenario(raw, path)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ScenarioError):
            raise
        raise ScenarioError(f"{path}: {exc}") from exc


def _parse_scenario(raw: dict[str, Any], path: Path) -> Scenario:
    context = str(path)

    for key in ("id", "name", "bounds", "charger"):
        _require(raw, key, context)

    assets = tuple(
        AssetSpec(
            name=str(_require(item, "name", context)),
            kind=str(_require(item, "kind", context)).lower(),  # type: ignore[arg-type]
            max_speed_mps=float(_require(item, "max_speed_mps", context)),
            battery_kwh=float(_require(item, "battery_kwh", context)),
            move_power_kw=float(_require(item, "move_power_kw", context)),
            idle_power_kw=float(item.get("idle_power_kw", 0.05)),
            sensor_radius_m=float(_require(item, "sensor_radius_m", context)),
            charge_rate_kw=float(_require(item, "charge_rate_kw", context)),
            max_slope_deg=float(_require(item, "max_slope_deg", context)),
            start=_point(_require(item, "start", context), f"{context} asset {item['name']}"),
            can_carry_antenna=bool(item.get("can_carry_antenna", False)),
        )
        for item in raw.get("assets", [])
    )
    if not assets:
        raise ScenarioError(f"{context}: at least one [[assets]] entry is required")

    antennas = tuple(
        AntennaSpec(
            name=str(_require(item, "name", context)),
            position=_point(item, context),
            height_m=float(item.get("height_m", 15.0)),
        )
        for item in raw.get("antennas", [])
    )

    obstacles = tuple(
        Obstacle(
            center=_point(item, context),
            radius_m=float(_require(item, "radius_m", context)),
        )
        for item in raw.get("obstacles", [])
    )

    relay_source = raw.get("relay_source_antenna")
    if relay_source == "":
        relay_source = None

    scenario = Scenario(
        id=str(_require(raw, "id", context)),
        name=str(_require(raw, "name", context)),
        description=str(raw.get("description", "")),
        bounds=_bounds(_require(raw, "bounds", context), context),
        terrain_seed=int(raw.get("terrain_seed", 20261022)),
        terrain_resolution_m=float(raw.get("terrain_resolution_m", 20.0)),
        horizon_s=float(raw.get("horizon_s", 21600.0)),
        dt_s=float(raw.get("dt_s", 15.0)),
        charger=_point(_require(raw, "charger", context), context),
        charger_bays=int(raw.get("charger_bays", 2)),
        assets=assets,
        antennas=antennas,
        obstacles=obstacles,
        world_margin_m=float(raw.get("world_margin_m", 500.0)),
        search_spacing_factor=float(raw.get("search_spacing_factor", 1.5)),
        comms_check_interval_s=float(raw.get("comms_check_interval_s", 30.0)),
        relay_source_antenna=relay_source,
        relay_candidates=tuple(_point(item, context) for item in raw.get("relay_candidates", [])),
    )
    return scenario


def default_scenario() -> Scenario:
    """The reference STP-style lunar search-and-rescue scenario."""

    return load_scenario("lunar_sar")


def list_builtin_scenarios() -> list[str]:
    return sorted(item.stem for item in DATA_DIR.glob("*.toml"))
