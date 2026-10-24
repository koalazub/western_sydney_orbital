"""Deterministic surrogate of the STP Lunar SAR campaign.

The model is intentionally campaign-level: terrain, slope, energy, charging,
comms line-of-sight, sensor coverage, detection and reporting. It is not a
rigid-body or power-system simulation. The value is in comparing policies under
the same terrain and target draw, then porting the winning route logic to an STP
Python Mission Manager.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

from wsorb.geometry import Vec2
from wsorb.model import (
    AntennaSpec,
    AssetRunStats,
    AssetState,
    Event,
    RunResult,
    Scenario,
    Strategy,
)
from wsorb.planner import PathPlanner, SearchPlan, build_search_plan
from wsorb.terrain import Terrain, build_terrain

PICKUP_DWELL_S = 90.0
PLACE_DWELL_S = 90.0
MAX_EVENTS = 4000


def build_scenario_terrain(scenario: Scenario, seed: int | None = None) -> Terrain:
    return build_terrain(
        scenario.world_bounds,
        scenario.terrain_resolution_m,
        scenario.terrain_seed if seed is None else seed,
        scenario.obstacles,
    )


def searchable_mask(scenario: Scenario, terrain: Terrain) -> np.ndarray:
    """Cells in the search box that at least one asset can traverse."""

    ny, nx = terrain.shape
    xs = np.linspace(terrain.bounds.min_x, terrain.bounds.max_x, nx)
    ys = np.linspace(terrain.bounds.min_y, terrain.bounds.max_y, ny)
    xx, yy = np.meshgrid(xs, ys)
    inside = (
        (xx >= scenario.bounds.min_x)
        & (xx <= scenario.bounds.max_x)
        & (yy >= scenario.bounds.min_y)
        & (yy <= scenario.bounds.max_y)
    )
    traversable = np.zeros((ny, nx), dtype=bool)
    for asset in scenario.assets:
        traversable |= terrain.slopes_deg <= asset.max_slope_deg
    for obstacle in scenario.obstacles:
        traversable &= np.hypot(xx - obstacle.center.x, yy - obstacle.center.y) > obstacle.radius_m
    return inside & traversable


def sample_target(scenario: Scenario, terrain: Terrain, rng: random.Random) -> Vec2:
    mask = searchable_mask(scenario, terrain)
    indices = np.argwhere(mask)
    if len(indices) == 0:
        raise ValueError("scenario has no searchable target cells")
    iy, ix = indices[rng.randrange(len(indices))]
    ny, nx = terrain.shape
    xs = np.linspace(terrain.bounds.min_x, terrain.bounds.max_x, nx)
    ys = np.linspace(terrain.bounds.min_y, terrain.bounds.max_y, ny)
    x = float(xs[ix])
    y = float(ys[iy])
    # Grid samples are screened against slope and obstacle constraints.
    # Jitter would invalidate that guarantee.
    return Vec2(x, y)


def _log(
    events: list[Event],
    t_s: float,
    kind: str,
    message: str,
    asset: str | None = None,
    position: Vec2 | None = None,
) -> None:
    if len(events) < MAX_EVENTS:
        events.append(Event(t_s=t_s, kind=kind, message=message, asset=asset, position=position))


@dataclass(slots=True)
class _Charger:
    bays: int
    slots: list[str]
    queue: list[str]


class Simulation:
    def __init__(
        self,
        scenario: Scenario,
        strategy: Strategy,
        *,
        seed: int = 0,
        target: Vec2 | None = None,
        terrain: Terrain | None = None,
        terrain_seed: int | None = None,
    ):
        self.scenario = scenario
        self.strategy = strategy
        self.seed = seed
        self._time = 0.0
        self.terrain_seed = scenario.terrain_seed if terrain_seed is None else terrain_seed
        self.terrain = terrain or build_scenario_terrain(scenario, self.terrain_seed)
        self.target = target or sample_target(scenario, self.terrain, random.Random(seed))
        if not scenario.bounds.contains(self.target):
            raise ValueError("target must lie inside the scenario search bounds")

        self.rng = random.Random(seed)
        self.planners = {
            asset.name: PathPlanner(self.terrain, asset.max_slope_deg) for asset in scenario.assets
        }
        self.search_plan: SearchPlan = build_search_plan(scenario, strategy, self.terrain)
        self.assets = {
            asset.name: AssetState(
                spec=asset,
                position=asset.start,
                soc=1.0,
                min_soc=1.0,
            )
            for asset in scenario.assets
        }
        self.charger = _Charger(
            bays=scenario.charger_bays,
            slots=[],
            queue=[],
        )
        self.active_antennas = list(scenario.antennas)
        self.events: list[Event] = []
        self.reported = False
        self.reported_at_s: float | None = None
        self.detected_at_s: float | None = None
        self.relay_asset = self.search_plan.relay_asset
        self.relay_position = self.search_plan.relay_position
        self.relay_completed = False
        self._grid_axes = self._make_grid_axes()
        self._search_mask = searchable_mask(scenario, self.terrain)
        self._coverage = np.zeros_like(self._search_mask, dtype=bool)
        self._init_assets()

    def _make_grid_axes(self) -> tuple[np.ndarray, np.ndarray]:
        ny, nx = self.terrain.shape
        xs = np.linspace(self.terrain.bounds.min_x, self.terrain.bounds.max_x, nx)
        ys = np.linspace(self.terrain.bounds.min_y, self.terrain.bounds.max_y, ny)
        return xs, ys

    def _grid_index(self, point: Vec2) -> tuple[int, int]:
        xs, ys = self._grid_axes
        ix = int(np.argmin(np.abs(xs - point.x)))
        iy = int(np.argmin(np.abs(ys - point.y)))
        return ix, iy

    def _init_assets(self) -> None:
        for asset in self.assets.values():
            asset.route = list(self.search_plan.routes.get(asset.name, []))
            if asset.name == self.relay_asset and self.relay_position is not None:
                asset.status = "relay"
                asset.relay_stage = 0
                self._plan_to(asset, self.search_plan.relay_source.position)  # type: ignore[union-attr]
                _log(
                    self.events,
                    0.0,
                    "relay",
                    f"{asset.name} tasked to redeploy {self.search_plan.relay_source.name}",
                    asset.name,
                )
            else:
                asset.status = "searching"
                self._next_leg(asset)

        _log(
            self.events,
            0.0,
            "mission",
            f"Campaign start: {len(self.assets)} assets, "
            f"{len(self.active_antennas)} antennas, "
            f"{self._search_mask.sum()} searchable cells",
        )

    @property
    def search_cells(self) -> int:
        return int(self._search_mask.sum())

    @property
    def coverage_fraction(self) -> float:
        total = self.search_cells
        if total == 0:
            return 0.0
        return float((self._coverage & self._search_mask).sum()) / total

    def _plan_to(self, asset: AssetState, target: Vec2) -> bool:
        path = self.planners[asset.name].plan(asset.position, target)
        if not path:
            _log(
                self.events,
                0.0,
                "navigation",
                f"{asset.name}: no traversable route to {target.x:.0f},{target.y:.0f}",
                asset.name,
                asset.position,
            )
            asset.path = []
            return False
        asset.path = path
        return True

    def _next_leg(self, asset: AssetState) -> None:
        while asset.route:
            target = asset.route.pop(0)
            if self._plan_to(asset, target):
                return
        asset.path = []
        if asset.status != "charging":
            asset.status = "idle"

    def _divert_to_charger(self, asset: AssetState) -> None:
        if asset.status in ("to_charger", "charging", "waiting"):
            return
        asset.interrupted_goal = asset.path[-1] if asset.path else None
        asset.status = "to_charger"
        _log(
            self.events,
            self._time,
            "power",
            f"{asset.name}: SOC {asset.soc:.0%}, returning to charger",
            asset.name,
            asset.position,
        )
        if not self._plan_to(asset, self.scenario.charger):
            asset.status = "idle"

    def _request_charger(self, asset: AssetState) -> None:
        if asset.stranded:
            return
        if asset.name in self.charger.slots or asset.name in self.charger.queue:
            return
        if len(self.charger.slots) < self.charger.bays:
            self.charger.slots.append(asset.name)
            asset.status = "charging"
            _log(
                self.events,
                self._time,
                "power",
                f"{asset.name}: charging (bay {len(self.charger.slots)}/{self.charger.bays})",
                asset.name,
            )
        else:
            self.charger.queue.append(asset.name)
            asset.status = "waiting"
            _log(self.events, self._time, "power", f"{asset.name}: charger queue", asset.name)

    def _release_charger(self, asset: AssetState) -> None:
        if asset.name in self.charger.slots:
            self.charger.slots.remove(asset.name)
        if asset.name in self.charger.queue:
            self.charger.queue.remove(asset.name)
        while self.charger.queue and len(self.charger.slots) < self.charger.bays:
            next_name = self.charger.queue.pop(0)
            next_asset = self.assets[next_name]
            self.charger.slots.append(next_name)
            next_asset.status = "charging"
            _log(
                self.events,
                self._time,
                "power",
                f"{next_name}: charging (bay {len(self.charger.slots)}/{self.charger.bays})",
                next_name,
            )

    def _consume_power(
        self,
        asset: AssetState,
        dt: float,
        moving: bool,
        slope: float,
        *,
        strand_on_depletion: bool = True,
    ) -> float:
        """Consume available energy and return the portion of ``dt`` powered."""

        if dt <= 0:
            if strand_on_depletion and asset.soc <= 1e-9 and not asset.stranded:
                self._strand(asset)
            return 0.0
        power = asset.spec.idle_power_kw
        if moving:
            power += asset.spec.move_power_kw * (1.0 + slope / 25.0)
        available_kwh = asset.soc * asset.spec.battery_kwh
        required_kwh = power * dt / 3600.0
        energy = min(available_kwh, required_kwh)
        powered_dt = (
            dt if power <= 0 or available_kwh >= required_kwh else available_kwh * 3600.0 / power
        )
        asset.energy_kwh += energy
        asset.soc = max(0.0, (available_kwh - energy) / asset.spec.battery_kwh)
        asset.min_soc = min(asset.min_soc, asset.soc)
        if strand_on_depletion and asset.soc <= 1e-9:
            self._strand(asset)
        return powered_dt

    def _strand(self, asset: AssetState) -> None:
        if asset.stranded:
            return
        asset.stranded = True
        asset.status = "stranded"
        asset.path = []
        if asset.name in self.charger.slots:
            self.charger.slots.remove(asset.name)
        if asset.name in self.charger.queue:
            self.charger.queue.remove(asset.name)
        _log(
            self.events,
            self._time,
            "power",
            f"{asset.name}: battery depleted; asset stranded",
            asset.name,
            asset.position,
        )

    def _speed(self, asset: AssetState) -> float:
        slope = self.terrain.slope_at(asset.position)
        factor = 1.0 / (1.0 + slope / 18.0)
        factor = min(1.0, max(0.3, factor))
        return asset.spec.max_speed_mps * self.strategy.speed_policy * factor

    def _advance(self, asset: AssetState, dt: float) -> bool:
        """Move along the current path. Returns whether the asset moved."""

        while asset.path and asset.position.distance_to(asset.path[0]) <= 1e-9:
            asset.path.pop(0)
        slope = self.terrain.slope_at(asset.position)
        speed = self._speed(asset)
        powered_dt = self._consume_power(
            asset, dt, bool(asset.path), slope, strand_on_depletion=False
        )
        remaining = speed * powered_dt
        moved = False
        while remaining > 1e-9 and asset.path:
            target = asset.path[0]
            distance = asset.position.distance_to(target)
            if distance <= 1e-9:
                asset.path.pop(0)
                continue
            if distance <= remaining:
                asset.position = target
                asset.path.pop(0)
                asset.distance_m += distance
                remaining -= distance
                moved = True
            else:
                asset.position = asset.position.lerp(target, remaining / distance)
                asset.distance_m += remaining
                remaining = 0.0
                moved = True
        if asset.soc <= 1e-9:
            self._strand(asset)
        return moved

    def _update_comms(self, asset: AssetState, t_s: float) -> None:
        if t_s - asset.last_comms_check_s < self.scenario.comms_check_interval_s:
            return
        asset.last_comms_check_s = t_s
        asset.comms_checks += 1
        asset.comms = any(
            self.terrain.comms_visible(asset.position, antenna.position, antenna.height_m)
            for antenna in self.active_antennas
        )
        if asset.comms:
            asset.comms_ok += 1
        if asset.detected and not asset.reported and asset.comms:
            self._report(asset, t_s)

    def _scan(self, asset: AssetState) -> None:
        radius = asset.spec.sensor_radius_m
        resolution = self.terrain.resolution_m
        cell_radius = int(math.ceil(radius / resolution))
        ix, iy = self._grid_index(asset.position)
        xs, ys = self._grid_axes
        ny, nx = self.terrain.shape
        for dy in range(-cell_radius, cell_radius + 1):
            for dx in range(-cell_radius, cell_radius + 1):
                cx, cy = ix + dx, iy + dy
                if not (0 <= cx < nx and 0 <= cy < ny):
                    continue
                if not self._search_mask[cy, cx]:
                    continue
                if (Vec2(float(xs[cx]), float(ys[cy])).distance_to(asset.position)) <= radius:
                    self._coverage[cy, cx] = True

    def _check_target(self, asset: AssetState, t_s: float) -> None:
        if self.reported:
            return
        distance = asset.position.distance_to(self.target)
        if distance > asset.spec.sensor_radius_m:
            return
        if not self.terrain.line_of_sight(asset.position, self.target, 2.0, 1.0):
            return
        if not asset.detected:
            asset.detected = True
            asset.detected_at_s = t_s
            if self.detected_at_s is None:
                self.detected_at_s = t_s
            _log(
                self.events,
                t_s,
                "detection",
                f"{asset.name}: target detected at {self.target.x:.0f},{self.target.y:.0f}",
                asset.name,
                asset.position,
            )
        if asset.comms:
            self._report(asset, t_s)
        else:
            _log(
                self.events,
                t_s,
                "comms",
                f"{asset.name}: detection awaiting comms window",
                asset.name,
                asset.position,
            )

    def _report(self, asset: AssetState, t_s: float) -> None:
        if self.reported:
            return
        asset.reported = True
        asset.reported_at_s = t_s
        self.reported = True
        self.reported_at_s = t_s
        _log(
            self.events,
            t_s,
            "report",
            f"{asset.name}: crash site reported to mission control",
            asset.name,
            asset.position,
        )

    def _update_relay(self, asset: AssetState, t_s: float, dt: float) -> None:
        if asset.relay_done:
            asset.status = "searching"
            self._next_leg(asset)
            return

        if asset.relay_stage == 0:
            if not asset.path:
                source = self.search_plan.relay_source
                if (
                    source is None
                    or asset.position.distance_to(source.position) > self.terrain.resolution_m * 2.0
                ):
                    asset.relay_done = True
                    asset.status = "searching"
                    _log(
                        self.events,
                        t_s,
                        "relay",
                        f"{asset.name}: relay task aborted (no route to pickup)",
                        asset.name,
                    )
                    self._next_leg(asset)
                    return
                asset.relay_stage = 1
                asset.relay_dwell_s = PICKUP_DWELL_S
                _log(self.events, t_s, "relay", f"{asset.name}: picking up antenna", asset.name)
                return
            self._advance(asset, dt)
            return

        if asset.relay_stage == 1:
            powered_dt = self._consume_power(
                asset, dt, False, self.terrain.slope_at(asset.position)
            )
            if asset.stranded:
                return
            asset.relay_dwell_s -= powered_dt
            if asset.relay_dwell_s <= 0:
                source = self.search_plan.relay_source
                if source is not None:
                    self.active_antennas = [
                        antenna for antenna in self.active_antennas if antenna.name != source.name
                    ]
                asset.relay_stage = 2
                assert self.relay_position is not None
                if not self._plan_to(asset, self.relay_position):
                    asset.relay_stage = 0
                    asset.relay_done = True
                    asset.status = "searching"
                    self._next_leg(asset)
                    return
                _log(
                    self.events,
                    t_s,
                    "relay",
                    f"{asset.name}: antenna stowed; moving to relay site",
                    asset.name,
                )
            return

        if asset.relay_stage == 2:
            if not asset.path:
                if (
                    self.relay_position is None
                    or asset.position.distance_to(self.relay_position)
                    > self.terrain.resolution_m * 2.0
                ):
                    asset.relay_done = True
                    asset.status = "searching"
                    _log(
                        self.events,
                        t_s,
                        "relay",
                        f"{asset.name}: relay task aborted (no route to site)",
                        asset.name,
                    )
                    self._next_leg(asset)
                    return
                asset.relay_stage = 3
                asset.relay_dwell_s = PLACE_DWELL_S
                _log(self.events, t_s, "relay", f"{asset.name}: placing antenna", asset.name)
                return
            self._advance(asset, dt)
            return

        if asset.relay_stage == 3:
            powered_dt = self._consume_power(
                asset, dt, False, self.terrain.slope_at(asset.position)
            )
            if asset.stranded:
                return
            asset.relay_dwell_s -= powered_dt
            if asset.relay_dwell_s > 0:
                return
            assert self.relay_position is not None
            self.active_antennas.append(
                AntennaSpec(name="RELAY", position=self.relay_position, height_m=18.0)
            )
            asset.relay_done = True
            asset.relay_stage = 4
            self.relay_completed = True
            asset.status = "searching"
            _log(self.events, t_s, "relay", f"{asset.name}: relay online", asset.name)
            self._next_leg(asset)

    def _update_search(self, asset: AssetState, dt: float) -> None:
        if asset.status == "idle":
            self._consume_power(asset, dt, False, self.terrain.slope_at(asset.position))
            return

        if asset.status == "searching" and asset.soc <= self.strategy.recharge_reserve:
            self._divert_to_charger(asset)
            return

        if asset.status == "to_charger":
            if not asset.path:
                if asset.position.distance_to(self.scenario.charger) <= self.terrain.resolution_m:
                    self._request_charger(asset)
                else:
                    asset.status = "idle"
                return
            self._advance(asset, dt)
            if asset.stranded:
                return
            if asset.position.distance_to(self.scenario.charger) <= self.terrain.resolution_m * 0.5:
                asset.path = []
                self._request_charger(asset)
            return

        if not asset.path:
            self._next_leg(asset)
            if not asset.path:
                self._consume_power(asset, dt, False, self.terrain.slope_at(asset.position))
                return
        if asset.path:
            self._advance(asset, dt)

    def _update_charger(self, t_s: float, dt: float, just_arrived: set[str] | None = None) -> None:
        just_arrived = just_arrived or set()
        for name in list(self.charger.slots):
            asset = self.assets[name]
            if asset.status != "charging" or name in just_arrived:
                continue
            charge_kwh = asset.spec.charge_rate_kw * dt / 3600.0
            asset.soc = min(1.0, asset.soc + charge_kwh / asset.spec.battery_kwh)
            if asset.soc >= self.strategy.recharge_target:
                asset.soc = self.strategy.recharge_target
                self._release_charger(asset)
                asset.status = "searching"
                _log(
                    self.events,
                    t_s,
                    "power",
                    f"{asset.name}: charge complete at {asset.soc:.0%}",
                    asset.name,
                )
                if asset.interrupted_goal is not None:
                    goal, asset.interrupted_goal = asset.interrupted_goal, None
                    if not self._plan_to(asset, goal):
                        self._next_leg(asset)
                else:
                    self._next_leg(asset)

        for name in list(self.charger.queue):
            asset = self.assets[name]
            if name not in just_arrived:
                asset.wait_s += dt
                self._consume_power(asset, dt, False, self.terrain.slope_at(asset.position))
        while self.charger.queue and len(self.charger.slots) < self.charger.bays:
            next_name = self.charger.queue.pop(0)
            self.charger.slots.append(next_name)
            self.assets[next_name].status = "charging"

    def run(self) -> RunResult:
        self._time = 0.0
        self._observe(0.0)
        if self.reported:
            return self._result()
        steps = int(math.ceil(self.scenario.horizon_s / self.scenario.dt_s))
        for step in range(steps):
            t_s = min(step * self.scenario.dt_s, self.scenario.horizon_s)
            dt = min(self.scenario.dt_s, self.scenario.horizon_s - t_s)
            self._time = t_s
            slots_at_start = set(self.charger.slots)
            queue_at_start = set(self.charger.queue)
            for asset in self.assets.values():
                if asset.stranded:
                    continue
                if asset.status == "relay":
                    self._update_relay(asset, t_s, dt)
                elif asset.status in ("searching", "to_charger", "idle"):
                    self._update_search(asset, dt)
            elapsed = t_s + dt
            self._observe(elapsed)
            if self.reported:
                break
            just_arrived = (set(self.charger.slots) - slots_at_start) | (
                set(self.charger.queue) - queue_at_start
            )
            self._update_charger(elapsed, dt, just_arrived)
            if self.reported:
                break
            if all(asset.stranded for asset in self.assets.values()):
                _log(self.events, t_s, "mission", "All assets stranded; mission failed")
                break

        if not self.reported:
            _log(
                self.events,
                self.scenario.horizon_s,
                "mission",
                "Mission clock expired without a confirmed report",
            )

        return self._result()

    def _observe(self, t_s: float) -> None:
        self._time = t_s
        for asset in self.assets.values():
            if asset.stranded:
                continue
            self._update_comms(asset, t_s)
            self._scan(asset)
            self._check_target(asset, t_s)
            if self.reported:
                return

    def _result(self) -> RunResult:
        asset_stats: list[AssetRunStats] = []
        for asset in self.assets.values():
            comms_fraction = asset.comms_ok / asset.comms_checks if asset.comms_checks else 1.0
            asset_stats.append(
                AssetRunStats(
                    name=asset.name,
                    kind=asset.spec.kind,
                    distance_m=asset.distance_m,
                    energy_kwh=asset.energy_kwh,
                    final_soc=asset.soc,
                    min_soc=asset.min_soc,
                    comms_fraction=comms_fraction,
                    charge_wait_s=asset.wait_s,
                    stranded=asset.stranded,
                    reported=asset.reported,
                )
            )

        total_checks = sum(asset.comms_checks for asset in self.assets.values())
        total_ok = sum(asset.comms_ok for asset in self.assets.values())
        comms_fraction = total_ok / total_checks if total_checks else 1.0
        stranded = tuple(asset.name for asset in self.assets.values() if asset.stranded)

        return RunResult(
            scenario_id=self.scenario.id,
            strategy_name=self.strategy.name,
            seed=self.seed,
            terrain_seed=self.terrain_seed,
            target=self.target,
            found=self.detected_at_s is not None,
            horizon_s=self.scenario.horizon_s,
            coverage_fraction=self.coverage_fraction,
            comms_fraction=comms_fraction,
            min_soc=min(asset.min_soc for asset in self.assets.values()),
            energy_kwh=sum(asset.energy_kwh for asset in self.assets.values()),
            stranded_assets=stranded,
            charger_wait_s=sum(asset.wait_s for asset in self.assets.values()),
            detected_at_s=self.detected_at_s,
            reported_at_s=self.reported_at_s,
            asset_stats=tuple(asset_stats),
            events=tuple(self.events),
            asset_plans={name: list(route) for name, route in self.search_plan.routes.items()},
            relay_position=self.relay_position,
            relay_asset=self.relay_asset,
            relay_completed=self.relay_completed,
        )


def run_simulation(
    scenario: Scenario,
    strategy: Strategy,
    *,
    seed: int = 0,
    target: Vec2 | None = None,
    terrain: Terrain | None = None,
    terrain_seed: int | None = None,
) -> RunResult:
    return Simulation(
        scenario,
        strategy,
        seed=seed,
        target=target,
        terrain=terrain,
        terrain_seed=terrain_seed,
    ).run()
