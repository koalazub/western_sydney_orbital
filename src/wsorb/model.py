"""Core data model shared by scenarios, simulation, campaign and reporting."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from wsorb.geometry import Bounds, Vec2
from wsorb.terrain import Obstacle

AssetKind = Literal["ltv", "scout", "truck", "excavator", "sampler"]
SearchPattern = Literal["sector", "sweep", "nearest"]


def _fraction(value: float, name: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class AssetSpec:
    """The fixed capabilities of one surface asset."""

    name: str
    kind: AssetKind
    max_speed_mps: float
    battery_kwh: float
    move_power_kw: float
    idle_power_kw: float
    sensor_radius_m: float
    charge_rate_kw: float
    max_slope_deg: float
    start: Vec2
    can_carry_antenna: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("asset name cannot be empty")
        for attr in (
            "max_speed_mps",
            "battery_kwh",
            "move_power_kw",
            "sensor_radius_m",
            "charge_rate_kw",
            "max_slope_deg",
        ):
            if getattr(self, attr) <= 0:
                raise ValueError(f"{self.name}.{attr} must be positive")
        if self.idle_power_kw < 0:
            raise ValueError(f"{self.name}.idle_power_kw cannot be negative")
        if not all(
            math.isfinite(getattr(self, attr))
            for attr in (
                "max_speed_mps",
                "battery_kwh",
                "move_power_kw",
                "idle_power_kw",
                "sensor_radius_m",
                "charge_rate_kw",
                "max_slope_deg",
            )
        ):
            raise ValueError(f"{self.name} capabilities must be finite")

    @property
    def label(self) -> str:
        return f"{self.name} ({self.kind})"


@dataclass(frozen=True, slots=True)
class AntennaSpec:
    name: str
    position: Vec2
    height_m: float = 15.0

    def __post_init__(self) -> None:
        if self.height_m <= 0:
            raise ValueError(f"{self.name} antenna height must be positive")


@dataclass(frozen=True, slots=True)
class Scenario:
    """A complete, self-contained surrogate mission definition."""

    id: str
    name: str
    description: str
    bounds: Bounds
    terrain_seed: int
    terrain_resolution_m: float
    horizon_s: float
    dt_s: float
    charger: Vec2
    charger_bays: int
    assets: tuple[AssetSpec, ...]
    antennas: tuple[AntennaSpec, ...]
    obstacles: tuple[Obstacle, ...] = ()
    world_margin_m: float = 500.0
    search_spacing_factor: float = 1.35
    comms_check_interval_s: float = 30.0
    relay_source_antenna: str | None = None
    relay_candidates: tuple[Vec2, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("scenario id cannot be empty")
        if not self.assets:
            raise ValueError("scenario must define at least one asset")
        numeric = (
            self.terrain_resolution_m,
            self.horizon_s,
            self.dt_s,
            self.world_margin_m,
            self.search_spacing_factor,
            self.comms_check_interval_s,
        )
        if not all(math.isfinite(value) for value in numeric):
            raise ValueError("scenario numeric fields must be finite")
        if self.terrain_resolution_m <= 0:
            raise ValueError("terrain_resolution_m must be positive")
        if self.world_margin_m < 0:
            raise ValueError("world_margin_m cannot be negative")
        if self.horizon_s <= 0 or self.dt_s <= 0:
            raise ValueError("horizon_s and dt_s must be positive")
        if self.dt_s > self.horizon_s:
            raise ValueError("dt_s cannot exceed horizon_s")
        if self.charger_bays < 1:
            raise ValueError("charger_bays must be at least 1")
        if self.search_spacing_factor <= 0:
            raise ValueError("search_spacing_factor must be positive")
        if self.comms_check_interval_s <= 0:
            raise ValueError("comms_check_interval_s must be positive")
        if not self.world_bounds.contains(self.charger):
            raise ValueError("charger must lie inside the world bounds")
        names = [asset.name for asset in self.assets]
        if len(names) != len(set(names)):
            raise ValueError("asset names must be unique")
        antenna_names = [antenna.name for antenna in self.antennas]
        if len(antenna_names) != len(set(antenna_names)):
            raise ValueError("antenna names must be unique")
        if self.relay_source_antenna and self.relay_source_antenna not in antenna_names:
            raise ValueError("relay_source_antenna must name a declared antenna")
        if self.relay_source_antenna and not any(a.can_carry_antenna for a in self.assets):
            raise ValueError("relay deployment needs an antenna-capable asset")

    @property
    def world_bounds(self) -> Bounds:
        return self.bounds.expanded(self.world_margin_m)

    @property
    def search_area_m2(self) -> float:
        return self.bounds.width * self.bounds.height

    def asset(self, name: str) -> AssetSpec:
        for asset in self.assets:
            if asset.name == name:
                return asset
        raise KeyError(name)

    def antenna(self, name: str) -> AntennaSpec:
        for antenna in self.antennas:
            if antenna.name == name:
                return antenna
        raise KeyError(name)

    def relay_capable_asset(self) -> AssetSpec | None:
        for asset in self.assets:
            if asset.can_carry_antenna:
                return asset
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "bounds": {
                "min_x": self.bounds.min_x,
                "max_x": self.bounds.max_x,
                "min_y": self.bounds.min_y,
                "max_y": self.bounds.max_y,
            },
            "terrain_seed": self.terrain_seed,
            "terrain_resolution_m": self.terrain_resolution_m,
            "world_margin_m": self.world_margin_m,
            "search_spacing_factor": self.search_spacing_factor,
            "horizon_s": self.horizon_s,
            "dt_s": self.dt_s,
            "charger": self.charger.as_tuple(),
            "charger_bays": self.charger_bays,
            "comms_check_interval_s": self.comms_check_interval_s,
            "relay_source_antenna": self.relay_source_antenna,
            "relay_candidates": [point.as_tuple() for point in self.relay_candidates],
            "assets": [
                {
                    "name": asset.name,
                    "kind": asset.kind,
                    "max_speed_mps": asset.max_speed_mps,
                    "battery_kwh": asset.battery_kwh,
                    "move_power_kw": asset.move_power_kw,
                    "idle_power_kw": asset.idle_power_kw,
                    "sensor_radius_m": asset.sensor_radius_m,
                    "charge_rate_kw": asset.charge_rate_kw,
                    "max_slope_deg": asset.max_slope_deg,
                    "start": asset.start.as_tuple(),
                    "can_carry_antenna": asset.can_carry_antenna,
                }
                for asset in self.assets
            ],
            "antennas": [
                {
                    "name": antenna.name,
                    "position": antenna.position.as_tuple(),
                    "height_m": antenna.height_m,
                }
                for antenna in self.antennas
            ],
            "obstacles": [
                {"center": obstacle.center.as_tuple(), "radius_m": obstacle.radius_m}
                for obstacle in self.obstacles
            ],
        }


@dataclass(frozen=True, slots=True)
class Strategy:
    """A Mission Manager policy to evaluate."""

    name: str
    pattern: SearchPattern = "sector"
    recharge_reserve: float = 0.2
    recharge_target: float = 0.9
    deploy_relay: bool = False
    speed_policy: float = 1.0
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("strategy name cannot be empty")
        if self.pattern not in ("sector", "sweep", "nearest"):
            raise ValueError(f"unknown search pattern: {self.pattern}")
        _fraction(self.recharge_reserve, "recharge_reserve")
        _fraction(self.recharge_target, "recharge_target")
        _fraction(self.speed_policy, "speed_policy")
        if self.recharge_target <= self.recharge_reserve:
            raise ValueError("recharge_target must exceed recharge_reserve")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "recharge_reserve": self.recharge_reserve,
            "recharge_target": self.recharge_target,
            "deploy_relay": self.deploy_relay,
            "speed_policy": self.speed_policy,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class Event:
    t_s: float
    kind: str
    message: str
    asset: str | None = None
    position: Vec2 | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "t_s": round(self.t_s, 1),
            "kind": self.kind,
            "message": self.message,
        }
        if self.asset is not None:
            payload["asset"] = self.asset
        if self.position is not None:
            payload["position"] = [round(self.position.x, 1), round(self.position.y, 1)]
        return payload


@dataclass(frozen=True, slots=True)
class AssetRunStats:
    name: str
    kind: str
    distance_m: float
    energy_kwh: float
    final_soc: float
    min_soc: float
    comms_fraction: float
    charge_wait_s: float
    stranded: bool
    reported: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "distance_m": round(self.distance_m, 1),
            "energy_kwh": round(self.energy_kwh, 3),
            "final_soc": round(self.final_soc, 4),
            "min_soc": round(self.min_soc, 4),
            "comms_fraction": round(self.comms_fraction, 4),
            "charge_wait_s": round(self.charge_wait_s, 1),
            "stranded": self.stranded,
            "reported": self.reported,
        }


@dataclass(slots=True)
class AssetState:
    """Mutable per-run asset state (not part of the public scenario model)."""

    spec: AssetSpec
    position: Vec2
    soc: float
    status: str = "searching"
    route: list[Vec2] = field(default_factory=list)
    path: list[Vec2] = field(default_factory=list)
    distance_m: float = 0.0
    energy_kwh: float = 0.0
    min_soc: float = 1.0
    wait_s: float = 0.0
    comms: bool = True
    comms_checks: int = 0
    comms_ok: int = 0
    last_comms_check_s: float = -1e9
    stranded: bool = False
    detected: bool = False
    detected_at_s: float | None = None
    reported: bool = False
    reported_at_s: float | None = None
    relay_stage: int = 0
    relay_dwell_s: float = 0.0
    relay_done: bool = False
    last_position: Vec2 | None = None
    interrupted_goal: Vec2 | None = None

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def kind(self) -> str:
        return self.spec.kind


@dataclass(frozen=True, slots=True)
class RunResult:
    scenario_id: str
    strategy_name: str
    seed: int
    terrain_seed: int
    target: Vec2
    found: bool
    horizon_s: float
    coverage_fraction: float
    comms_fraction: float
    min_soc: float
    energy_kwh: float
    stranded_assets: tuple[str, ...]
    charger_wait_s: float
    detected_at_s: float | None
    reported_at_s: float | None
    asset_stats: tuple[AssetRunStats, ...]
    events: tuple[Event, ...]
    asset_plans: dict[str, list[Vec2]]
    relay_position: Vec2 | None = None
    relay_asset: str | None = None
    relay_completed: bool = False

    @property
    def success(self) -> bool:
        return self.found and self.reported_at_s is not None

    @property
    def time_to_report_s(self) -> float | None:
        return self.reported_at_s

    def to_dict(
        self, *, include_events: bool = True, include_plans: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "scenario_id": self.scenario_id,
            "strategy": self.strategy_name,
            "seed": self.seed,
            "terrain_seed": self.terrain_seed,
            "target": [round(self.target.x, 2), round(self.target.y, 2)],
            "found": self.found,
            "success": self.success,
            "detected_at_s": self.detected_at_s,
            "reported_at_s": self.reported_at_s,
            "horizon_s": self.horizon_s,
            "coverage_fraction": round(self.coverage_fraction, 4),
            "comms_fraction": round(self.comms_fraction, 4),
            "min_soc": round(self.min_soc, 4),
            "energy_kwh": round(self.energy_kwh, 3),
            "stranded_assets": list(self.stranded_assets),
            "charger_wait_s": round(self.charger_wait_s, 1),
            "relay_position": (
                [round(self.relay_position.x, 1), round(self.relay_position.y, 1)]
                if self.relay_position
                else None
            ),
            "relay_asset": self.relay_asset,
            "relay_completed": self.relay_completed,
            "asset_stats": [stats.to_dict() for stats in self.asset_stats],
        }
        if include_events:
            payload["events"] = [event.to_dict() for event in self.events]
        if include_plans:
            payload["asset_plans"] = {
                name: [[round(point.x, 1), round(point.y, 1)] for point in plan]
                for name, plan in self.asset_plans.items()
            }
        return payload

    def summary_line(self) -> str:
        status = "FOUND" if self.success else "MISS"
        when = f"{self.reported_at_s / 60:.1f} min" if self.reported_at_s is not None else "—"
        return (
            f"{self.strategy_name:18s} {status:5s} report={when:>9s} "
            f"coverage={self.coverage_fraction:5.1%} minSOC={self.min_soc:5.1%}"
        )


@dataclass(frozen=True, slots=True)
class StrategySummary:
    strategy_name: str
    runs: int
    successes: int
    success_rate: float
    median_report_s: float | None
    p90_report_s: float | None
    mean_coverage: float
    mean_min_soc: float
    stranded_rate: float
    mean_energy_kwh: float
    mean_comms_fraction: float

    @property
    def score(self) -> tuple[float, float, float]:
        """Higher is better: success first, then speed, then coverage."""
        report = self.median_report_s if self.median_report_s is not None else math.inf
        return (self.success_rate, -report, self.mean_coverage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy_name,
            "runs": self.runs,
            "successes": self.successes,
            "success_rate": round(self.success_rate, 4),
            "median_report_s": (
                round(self.median_report_s, 1) if self.median_report_s is not None else None
            ),
            "p90_report_s": (
                round(self.p90_report_s, 1) if self.p90_report_s is not None else None
            ),
            "mean_coverage": round(self.mean_coverage, 4),
            "mean_min_soc": round(self.mean_min_soc, 4),
            "stranded_rate": round(self.stranded_rate, 4),
            "mean_energy_kwh": round(self.mean_energy_kwh, 3),
            "mean_comms_fraction": round(self.mean_comms_fraction, 4),
        }


@dataclass(frozen=True, slots=True)
class CampaignResult:
    campaign_id: str
    scenario: Scenario
    seed: int
    replicates: int
    strategies: tuple[Strategy, ...]
    runs: tuple[RunResult, ...]
    summaries: tuple[StrategySummary, ...]
    created_at: str
    command: str = ""

    def to_dict(self, *, include_runs: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "created_at": self.created_at,
            "scenario": self.scenario.to_dict(),
            "seed": self.seed,
            "replicates": self.replicates,
            "strategies": [strategy.to_dict() for strategy in self.strategies],
            "summaries": [summary.to_dict() for summary in self.summaries],
            "command": self.command,
        }
        if include_runs:
            payload["runs"] = [
                run.to_dict(include_events=False, include_plans=False) for run in self.runs
            ]
        return payload
