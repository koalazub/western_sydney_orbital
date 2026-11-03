"""Versioned launcher worlds and exact, read-only native receipt comparisons.

The team settings come from runs/teammate-analytics-69/configuration.json.
Official uses examples/official_comparison.json over the normalized scenario
defaults. Across Phobos records the current phobos_escape.json defaults.
These definitions grant no setup or actuation permission.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from .local_api import FLIGHT_PRESETS, validate_experiment_settings

WORLD_DEFINITIONS_VERSION = 1


def uint32(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("Seed must be an unsigned 32-bit integer")
    return value


@dataclass(frozen=True, slots=True)
class ControllerWorld:
    id: str
    label: str
    preset: str
    _settings_json: str

    @property
    def settings(self) -> dict[str, Any]:
        """Detached configure_flight settings, never mutable shared definitions."""
        return json.loads(self._settings_json)


def parse_worlds(value: Any) -> tuple[ControllerWorld, ...]:
    """Validate versioned data without connecting to or configuring an engine."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Exactly three reviewed controller worlds are required")
    result = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"id", "label", "preset", "settings"}:
            raise ValueError("Invalid controller world fields")
        if (
            not isinstance(entry["id"], str)
            or entry["id"] not in ("official", "team", "across_phobos")
            or not isinstance(entry["label"], str)
            or not 0 < len(entry["label"]) <= 128
            or not isinstance(entry["preset"], str)
            or entry["preset"] not in FLIGHT_PRESETS
        ):
            raise ValueError("Invalid reviewed controller world")
        settings = validate_experiment_settings(entry["settings"])
        if settings != entry["settings"] or settings["debris"]["count"] != 500:
            raise ValueError("Controller worlds require full normalized stock-500 settings")
        result.append(
            ControllerWorld(
                entry["id"], entry["label"], entry["preset"], json.dumps(settings, allow_nan=False)
            )
        )
    if len({world.id for world in result}) != len(result):
        raise ValueError("Duplicate controller world")
    return tuple(result)


_WORLDS = parse_worlds(
    json.loads(files("wsorb").joinpath("controller_worlds.json").read_text(encoding="utf-8"))
)
WORLD_IDS = tuple(world.id for world in _WORLDS)


def controller_world(world_id: Any) -> ControllerWorld:
    if isinstance(world_id, str):
        for world in _WORLDS:
            if world.id == world_id:
                return world
    raise ValueError("Select a reviewed controller world ID")


def world_matches(snapshot: Mapping[str, Any], world_id: str, seed: int) -> bool:
    """Match intended setup and existing native caps, not just a browser label."""
    try:
        world, seed = controller_world(world_id), uint32(seed)
        observation = snapshot["observation"]
        expected = world.settings
        actual = validate_experiment_settings(observation["experiment_settings"])
        # Native serde omits the default DirectedStream variant. Restore only
        # that documented default before comparing the complete receipt. Other
        # missing fields, extra fields and settings changes still fail closed.
        receipt = observation["experiment_settings"]
        receipt = {
            **receipt,
            "debris": {
                **receipt["debris"],
                "distribution": receipt["debris"].get("distribution", {"kind": "directed_stream"}),
            },
        }
        return (
            type(observation["seed"]) is int
            and observation["seed"] == seed
            and actual == receipt == expected
            and observation["flight_preset"] == world.preset
            and observation["launch_site_direction"] is None
            and observation["geometry"]["debris_count"] == expected["debris"]["count"] == 500
            and observation["geometry"]["arrival_radius_m"] == 50.0
            and observation["ship_tuning"] == {**expected["ship"], "thrust_scale": 1.0}
            and observation["debris_flow"]
            == {
                name: expected["debris"][name]
                for name in ("speed_mps", "speed_spread_mps", "stream_axis", "stream_sign")
            }
            and observation["max_accel_mps2"] == 0.5
            and observation["max_angular_accel_rad_s2"] == 0.05
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
