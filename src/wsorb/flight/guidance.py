"""Opt-in persistent guidance from the versioned local public sensor contract.

This module neither contacts an engine nor applies commands. It selects genuine
radar samples, models ownship between navigation reports, and gates a bounded
waypoint controller. Coasting on lost observability is not braking or a promise
of collision avoidance. Native command leases are still required for worker loss.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from ._validation import count, distance, field, mapping, point, scalar, vector
from .config import PlannerConfig, ResilienceConfig
from .control import _bounded, coast_command, reference_to_body
from .estimation import NavigationSample, StateEstimate, StateEstimator
from .geometry import Capsule, CollisionIndex, segment_clear
from .planning import MAX_PREVIEW_POINTS, RoutePlan
from .prediction import PlanningFrame, predict_obstacles, radar_rows

_RADARS = {"radar_primary": "radar", "radar_backup": "radar_backup"}
_UNITS = (*_RADARS, "camera_top", "camera_bottom", "navigation")
_FAULTS = frozenset((*_UNITS, "radar_bus", "all_avionics"))
_DELIVERIES = frozenset({"ready", "unavailable", "frozen", "waiting_for_sample"})
_NOISE = ("range_noise_m", "range_rate_noise_mps", "diameter_noise_fraction")
_MIN_LEASE_S = 0.05
_CRUISE_CANDIDATES_MPS = (8.0, 4.0, 2.0)


@dataclass(frozen=True, slots=True)
class GuidanceUpdate:
    """Frozen receipt with defensive deep copies, never rewritten measurements.

    ``can_command`` means the sensor/estimator gate passed. ``command`` must
    additionally accept a current checked route. A zero lease means no native
    lease can be submitted. Call ``update`` with raw native observations, not
    this receipt's selected-channel observation.
    """

    _observation: dict
    _report: dict
    can_command: bool
    requires_replan: bool
    lease_sim_s: float

    @property
    def observation(self) -> dict:
        return deepcopy(self._observation)

    @property
    def report(self) -> dict:
        return deepcopy(self._report)


@dataclass(frozen=True, slots=True)
class _Unit:
    id: str
    delivery: str
    active_faults: tuple[str, ...]
    sample_time_s: float | None
    sequence: int | None
    provenance: str
    noise: tuple[float | None, float | None, float | None]
    shared_fault_domains: tuple[str, ...]

    def to_report(self) -> dict:
        return {
            "id": self.id,
            "delivery": self.delivery,
            "active_faults": list(self.active_faults),
            "sample_time_s": self.sample_time_s,
            "sequence": self.sequence,
            "provenance": self.provenance,
            "noise": dict(zip(_NOISE, self.noise, strict=True)),
            "shared_fault_domains": list(self.shared_fault_domains),
        }


def _targets(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > len(_FAULTS):
        raise ValueError(f"Invalid {name}")
    if any(not isinstance(item, str) or item not in _FAULTS for item in value):
        raise ValueError(f"Invalid {name}")
    if len(set(value)) != len(value):
        raise ValueError(f"Duplicate {name}")
    return tuple(value)


def _health(observation: Mapping[str, Any], now: float) -> tuple[int, dict[str, _Unit]]:
    health = mapping(field(observation, "sensor_health"), "sensor_health")
    if type(health.get("schema_version")) is not int or health["schema_version"] != 1:
        raise ValueError("Resilient guidance requires sensor_health schema version 1")
    revision = count(field(health, "revision"), "sensor revision", maximum=0xFFFFFFFF)
    values = field(health, "units")
    if not isinstance(values, (list, tuple)) or len(values) != len(_UNITS):
        raise ValueError("All five public sensor units must be reported")
    units = {}
    for value in values:
        item = mapping(value, "sensor unit")
        identity = field(item, "id")
        if not isinstance(identity, str) or identity not in _UNITS or identity in units:
            raise ValueError("Unknown or duplicate sensor unit")
        delivery = field(item, "delivery")
        if not isinstance(delivery, str) or delivery not in _DELIVERIES:
            raise ValueError("Unknown sensor delivery state")
        timestamp, sequence = field(item, "sample_time_s"), field(item, "sequence")
        if (timestamp is None) != (sequence is None):
            raise ValueError("Sample time and sequence must both be present or null")
        if timestamp is not None:
            timestamp = scalar(timestamp, "sensor sample_time_s")
            sequence = count(sequence, "sensor sequence", maximum=2**53 - 1)
            if timestamp > now:
                raise ValueError("Future-dated sensor metadata")
        if delivery == "ready" and timestamp is None:
            raise ValueError("Ready delivery requires an actual sample clock")
        provenance = field(item, "provenance")
        if not isinstance(provenance, str) or not provenance.strip() or len(provenance) > 240:
            raise ValueError("Sensor provenance must be a nonempty bounded string")
        noise = mapping(field(item, "noise"), "sensor noise")
        noise = tuple(
            None
            if identity not in _RADARS and field(noise, name) is None
            else scalar(field(noise, name), name)
            for name in _NOISE
        )
        domains = _targets(field(item, "shared_fault_domains"), "shared fault domains")
        required = {"all_avionics", "radar_bus"} if identity in _RADARS else {"all_avionics"}
        if not required.issubset(domains):
            raise ValueError("Sensor metadata omitted a known common failure domain")
        units[identity] = _Unit(
            identity,
            delivery,
            _targets(field(item, "active_faults"), "active faults"),
            timestamp,
            sequence,
            provenance,
            noise,
            domains,
        )
    return revision, units


@dataclass(slots=True)
class _SampleClock:
    time_s: float | None = None
    sequence: int | None = None
    fingerprint: Any = None
    new_wall_s: float = 0.0

    def observe(self, unit: _Unit, fingerprint: Any, wall: float) -> bool:
        timestamp, sequence = unit.sample_time_s, unit.sequence
        if timestamp is None or sequence is None:
            raise ValueError("No source sample")
        if self.time_s is not None:
            if timestamp < self.time_s or sequence < self.sequence:
                raise ValueError("Source sample clock or sequence regressed")
            if timestamp == self.time_s:
                if sequence != self.sequence or fingerprint != self.fingerprint:
                    raise ValueError("Source changed without an advancing sample time")
                return False
            if sequence == self.sequence:
                raise ValueError("Source time advanced without a new sequence")
        self.time_s, self.sequence = timestamp, sequence
        self.fingerprint, self.new_wall_s = fingerprint, wall
        return True


@dataclass(slots=True)
class _Dwell:
    last: tuple[float, ...] = ()
    first_s: float | None = None
    latest_s: float | None = None
    samples: int = 0

    def advance(self, stamps: tuple[float, ...]) -> None:
        if self.last and any(a <= b for a, b in zip(stamps, self.last, strict=True)):
            return
        self.last = stamps
        self.latest_s = min(stamps)
        if self.first_s is None:
            self.first_s = self.latest_s
        self.samples += 1

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self.first_s is None else self.latest_s - self.first_s

    def ready(self, config: ResilienceConfig) -> bool:
        return self.samples >= config.recovery_samples and self.elapsed_s >= config.recovery_dwell_s


@dataclass(frozen=True, slots=True)
class _CheckedFrame:
    frame: PlanningFrame
    generation: int
    expires_at_s: float


class ResilientGuidance:
    """One worker's estimator, channel selection, recovery and route progress.

    Instantiate again after explicit consent following a manual/setup change or
    rewind. Sensor recovery alone cannot clear that consent hold. The caller
    owns run consent, actual command transport and native lease/context checks.
    """

    def __init__(self, config: ResilienceConfig | None = None) -> None:
        self.config = ResilienceConfig() if config is None else config
        if not isinstance(self.config, ResilienceConfig):
            raise ValueError("config must be a ResilienceConfig")
        self._estimator = StateEstimator(self.config)
        self._clocks = {identity: _SampleClock() for identity in _UNITS}
        self._epoch: tuple | None = None
        self._tuning: int | None = None
        self._now: float | None = None
        self._wall: float | None = None
        self._running = False
        self._resume_wall = 0.0
        self._revision: int | None = None
        self._selected: str | None = None
        self._consent_hold = False
        self._recovering = False
        self._recovery = _Dwell()
        self._primary_recovery = _Dwell()
        self._recovery_channel: str | None = None
        self._loss_nav_time = -1.0
        self._loss_radar_times: dict[str, float] = {}
        self._generation = 0
        self._frames: deque[_CheckedFrame] = deque(maxlen=8)
        self._route: RoutePlan | None = None
        self._waypoint = 1
        self._next_waypoint: tuple[float, float, float] | None = None
        self._decision = "no_command_calculated"
        self._decision_time: float | None = None
        self._need_plan = True
        self._ready = False
        self._lease = 0.0
        self._observation: dict = {}
        self._signature: tuple | None = None
        self._estimate: StateEstimate | None = None
        self._report: dict = {
            "mode": "coast",
            "reason": "No public observation received",
            "can_command": False,
            "requires_replan": True,
            "lease_sim_s": 0.0,
            "source_time_s": None,
            "next_waypoint_world_m": None,
        }
        self.result: GuidanceUpdate | None = None

    @property
    def report(self) -> dict:
        return deepcopy(self._report)

    def _invalidate(self) -> None:
        self._generation += 1
        self._frames.clear()
        self._route = None
        self._waypoint = 1
        self._next_waypoint = None
        self._decision = "route_invalidated"
        self._decision_time = None
        self._need_plan = True

    @staticmethod
    def _epoch_key(observation: Mapping[str, Any]) -> tuple:
        numbers = tuple(
            count(field(observation, name), name, maximum=0xFFFFFFFF)
            for name in ("seed", "trial_revision", "manual_interventions")
        )
        reference = field(observation, "frame")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("Missing public reference frame")
        if observation.get("rotation_convention") != "active_xyzw":
            raise ValueError("Resilient local navigation requires active_xyzw")
        if observation.get("radar_clock") != "simulation_elapsed_seconds":
            raise ValueError("Resilient local sensors require the simulation elapsed clock")
        return (*numbers, reference, observation["rotation_convention"], observation["radar_clock"])

    def _enter_loss(self) -> None:
        self._ready = False
        self._lease = 0.0
        self._recovering = True
        self._recovery_channel = None
        self._recovery = _Dwell()
        self._loss_nav_time = self._clocks["navigation"].time_s
        if self._loss_nav_time is None:
            self._loss_nav_time = -1.0
        self._loss_radar_times = {
            identity: clock.time_s if clock.time_s is not None else -1.0
            for identity, clock in self._clocks.items()
            if identity in _RADARS
        }
        self._selected = None
        self._invalidate()

    def _unit_sample(
        self, unit: _Unit, fingerprint: Any, faults: set[str], wall: float, running: bool
    ) -> tuple[bool, bool, str]:
        blocked = faults.intersection((*unit.shared_fault_domains, unit.id))
        if unit.delivery != "ready" or blocked:
            return False, False, "Delivery or a shared failure domain is not ready"
        try:
            fresh = self._clocks[unit.id].observe(unit, fingerprint, wall)
        except ValueError:
            return False, False, "Ambiguous or regressed sample clock/sequence/content"
        age_limit = self.config.radar_age_s if unit.id in _RADARS else self.config.navigation_age_s
        if self._now - unit.sample_time_s > age_limit:
            return False, fresh, "Source sample exceeded its simulation-time freshness limit"
        clock = self._clocks[unit.id]
        wall_age = wall - max(clock.new_wall_s, self._resume_wall)
        if running and wall_age > self.config.wall_no_progress_s:
            return False, fresh, "No advancing source data within the running wall-time limit"
        return True, fresh, "Genuine source sample within freshness limits"

    def _choose_channel(self, usable: dict[str, bool], units: dict[str, _Unit]) -> str | None:
        primary, backup = "radar_primary", "radar_backup"
        if self._selected == backup:
            if usable[primary]:
                self._primary_recovery.advance((units[primary].sample_time_s,))
            else:
                previous = self._clocks[primary].time_s
                self._primary_recovery = _Dwell(last=(previous if previous is not None else -1.0,))
            if usable[primary] and self._primary_recovery.ready(self.config):
                return primary
            return backup if usable[backup] else None
        if usable[primary]:
            return primary
        previous = self._clocks[primary].time_s
        self._primary_recovery = _Dwell(last=(previous if previous is not None else -1.0,))
        return backup if usable[backup] else None

    def _available_lease(self, radar_time: float, nav_time: float) -> float:
        remaining = min(
            self.config.lease_sim_s,
            radar_time
            + min(self.config.radar_age_s, self.config.planner.forecast_horizon_s)
            - self._now,
            nav_time + self.config.navigation_age_s - self._now,
        )
        if remaining < _MIN_LEASE_S:
            return 0.0
        end = self._estimator.estimate(self._now + remaining)
        if not self._estimator.uncertainty_within_limits(end):
            low, high = 0.0, remaining
            for _ in range(32):
                middle = (low + high) * 0.5
                end = self._estimator.estimate(self._now + middle)
                if self._estimator.uncertainty_within_limits(end):
                    low = middle
                else:
                    high = middle
            remaining = low
        return remaining if remaining >= _MIN_LEASE_S else 0.0

    def _receipt(self) -> GuidanceUpdate:
        self._report.update(
            source_time_s=self._now,
            next_waypoint_world_m=(
                list(self._next_waypoint) if self._next_waypoint is not None else None
            ),
            waypoint_index=self._waypoint,
            decision=self._decision,
            decision_source_time_s=self._decision_time,
            decision_kind="calculated_only_not_applied_or_acknowledged",
            can_command=self._ready,
            requires_replan=self._need_plan,
            lease_sim_s=self._lease,
            selected_channel=self._selected,
            requires_consent=self._consent_hold,
            recovery_samples=self._recovery.samples,
            recovery_dwell_s=self._recovery.elapsed_s,
            primary_recovery_samples=self._primary_recovery.samples,
            primary_recovery_dwell_s=self._primary_recovery.elapsed_s,
            estimate=self._estimate.to_report() if self._estimate is not None else None,
            ownship_history_samples=len(self._estimator.history),
            model_accel_allowance_mps2=self._estimator.accel_allowance_mps2,
            model_accel_allowance_kind="assumption_not_a_bound_on_gravity_or_contacts",
            last_navigation_innovation_m=self._estimator.last_innovation_m,
            last_velocity_innovation_mps=self._estimator.last_velocity_innovation_mps,
            radar_model="anonymous_sample_envelopes_no_tracks_or_tangential_velocity_estimates",
            navigation_redundancy="none",
            camera_navigation="not_implemented",
            command_gate="Sensor readiness also requires a current checked route at command time",
        )
        self.result = GuidanceUpdate(
            deepcopy(self._observation),
            deepcopy(self._report),
            self._ready,
            self._need_plan,
            self._lease,
        )
        return self.result

    def update(self, observation, *, wall_time_s: float, running: bool) -> GuidanceUpdate:
        """Consume a raw public observation without I/O or synthetic measurements.

        Invalid/missing schema metadata fails closed. Frozen samples remain
        authentic but ineligible. Only advancing source times count for liveness
        and recovery. Wall time measures lack of progress, never measurement age.
        """
        self._ready, self._lease = False, 0.0
        self._report = {"mode": "coast", "reason": "Invalid or missing public sensor contract"}
        self._observation = {}
        self._signature = None
        try:
            observation = mapping(observation, "observation")
            self._observation = deepcopy(dict(observation))
            # No eligible selected channel means no obstacle knowledge, not a clear scan.
            self._observation["radar"] = []
            wall = scalar(wall_time_s, "wall_time_s")
            if type(running) is not bool or (self._wall is not None and wall < self._wall):
                raise ValueError("Invalid or regressed local monotonic clock")
            now = scalar(field(observation, "time_s"), "time_s")
            epoch = self._epoch_key(observation)
            tuning = count(
                field(observation, "tuning_revision"), "tuning_revision", maximum=0xFFFFFFFF
            )
            discontinuity = self._epoch is not None and (epoch != self._epoch or now < self._now)
            if self._tuning is not None and tuning < self._tuning and not discontinuity:
                raise ValueError("Tuning revision regressed without a new flight context")
            if discontinuity:
                self._consent_hold = True
                self._estimator = StateEstimator(self.config)
                self._clocks = {identity: _SampleClock() for identity in _UNITS}
                self._estimate = None
                self._selected = None
                self._invalidate()
            elif self._tuning is not None and tuning != self._tuning:
                self._estimator = StateEstimator(self.config)
                self._estimate = None
                self._invalidate()
            self._epoch, self._tuning, self._now, self._wall = epoch, tuning, now, wall
            if running and not self._running:
                self._resume_wall = wall
            self._running = running
            revision, units = _health(observation, now)
            if self._revision is not None and revision < self._revision and not discontinuity:
                raise ValueError("Sensor revision regressed")
            if revision != self._revision:
                self._revision = revision
                self._invalidate()
            faults = set().union(*(unit.active_faults for unit in units.values()))
            self._report.update(
                source_time_s=now,
                source_seed=epoch[0],
                source_clock=observation["radar_clock"],
                frame=observation["frame"],
                sensor_schema_version=1,
                sensor_revision=revision,
                active_faults=sorted(faults),
                units=[units[identity].to_report() for identity in _UNITS],
            )
            # Both fields are part of v1. Missing metadata is never legacy fallback.
            for name in _RADARS.values():
                field(observation, name)
            usable, rows_by_channel, assessments = {}, {}, {}
            for identity, name in _RADARS.items():
                unit = units[identity]
                try:
                    rows = radar_rows(observation[name])
                    if len(rows) and np.any(rows[:, 2, 0] != unit.sample_time_s):
                        raise ValueError("Radar rows and sample metadata disagree")
                    if self.config.planner.sigma_multiplier * unit.noise[2] >= 1.0:
                        raise ValueError("Diameter uncertainty cannot be represented")
                    valid, fresh, reason = self._unit_sample(
                        unit, rows.tobytes(), faults, wall, running
                    )
                    if valid and not len(rows):
                        valid = False
                        reason = "Ready empty scan; unobserved space is not assumed clear"
                    rows_by_channel[identity] = rows
                except (TypeError, ValueError, OverflowError):
                    valid, fresh = False, False
                    reason = "Invalid radar rows or channel noise metadata"
                usable[identity] = valid
                assessments[identity] = {"usable": valid, "new_sample": fresh, "reason": reason}

            navigation = units["navigation"]
            nav_valid, nav_fresh = False, False
            nav_reason = "Navigation delivery is unavailable or malformed"
            try:
                ship = mapping(field(observation, "shuttle"), "shuttle")
                target = mapping(field(observation, "target"), "target")
                nav_time = scalar(field(observation, "navigation_sample_time_s"), "navigation time")
                if nav_time != navigation.sample_time_s:
                    raise ValueError("Navigation clocks disagree")
                sample = NavigationSample.from_public(nav_time, ship)
                target_position = point(field(target, "position"), "target.position")
                ship_radius = scalar(field(ship, "radius_m"), "shuttle.radius_m", positive=True)
                target_radius = scalar(field(target, "radius_m"), "target.radius_m", positive=True)
                fingerprint = (sample, target_position, ship_radius, target_radius)
                nav_valid, nav_fresh, nav_reason = self._unit_sample(
                    navigation, fingerprint, faults, wall, running
                )
                if nav_valid:
                    gain = self._actuator_gain(observation)
                    limit = scalar(field(observation, "max_accel_mps2"), "max_accel_mps2")
                    scalar(
                        field(observation, "max_angular_accel_rad_s2"), "max_angular_accel_rad_s2"
                    )
                    self._estimator.accel_allowance_mps2 = max(
                        self.config.model_accel_allowance_mps2, min(0.5, limit) * gain
                    )
                    self._estimator.correct(sample)
            except (KeyError, TypeError, ValueError, OverflowError):
                nav_valid = False
                nav_reason = "Navigation sample, timing or actuator limits are invalid"
            assessments["navigation"] = {
                "usable": nav_valid,
                "new_sample": nav_fresh,
                "reason": nav_reason,
            }
            self._report["assessments"] = assessments
            if self._estimator.history:
                self._estimate = self._estimator.estimate(now)
            selected = self._choose_channel(usable, units)
            if not nav_valid or selected is None:
                self._report["reason"] = (
                    nav_reason if not nav_valid else "No eligible radar with obstacle knowledge"
                )
                self._enter_loss()
                return self._receipt()
            if selected != self._selected:
                self._selected = selected
                self._invalidate()
            self._observation["radar"] = deepcopy(observation[_RADARS[selected]])
            self._observation["sensor_model"] = dict(
                zip(_NOISE, units[selected].noise, strict=True)
            )
            self._signature = self._command_signature(observation)
            if self._consent_hold:
                self._report["reason"] = (
                    "Flight context changed; explicit consent and a new guidance "
                    "instance are required"
                )
                return self._receipt()
            if not self._estimator.uncertainty_within_limits(self._estimate):
                self._report["reason"] = "Ownship model uncertainty exceeded its configured limit"
                self._enter_loss()
                return self._receipt()
            if self._recovering:
                if self._recovery_channel != selected:
                    self._recovery_channel = selected
                    self._recovery = _Dwell(
                        last=(self._loss_nav_time, self._loss_radar_times.get(selected, -1.0))
                    )
                self._recovery.advance((navigation.sample_time_s, units[selected].sample_time_s))
                if not self._recovery.ready(self.config):
                    self._report.update(
                        mode="recovering",
                        reason=(
                            "Waiting for distinct advancing navigation/radar samples "
                            "and simulation-time dwell"
                        ),
                    )
                    return self._receipt()
                self._recovering = False
                self._invalidate()
            rows = rows_by_channel[selected]
            self._lease = self._available_lease(float(min(rows[:, 2, 0])), navigation.sample_time_s)
            if self._lease == 0.0:
                self._report["reason"] = (
                    "No minimum-length lease fits sample freshness and model uncertainty"
                )
                self._enter_loss()
                return self._receipt()
            self._ready = True
            if not self._frames or self._frames[-1].expires_at_s < now + self._lease:
                self._need_plan = True
            self._report.update(
                mode="degraded" if selected == "radar_backup" else "nominal",
                reason="Independent backup selected; a checked route is required"
                if selected == "radar_backup"
                else (
                    "Primary radar and direct navigation are eligible; a checked route is required"
                ),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            self._enter_loss()
        return self._receipt()

    @staticmethod
    def _actuator_gain(observation: Mapping[str, Any]) -> float:
        tuning = mapping(field(observation, "ship_tuning"), "ship_tuning")
        gain = scalar(field(tuning, "thrust_scale"), "thrust_scale", positive=True)
        if not 0.1 <= gain <= 10.0:
            raise ValueError("Local actuator gain is outside its public contract")
        return gain

    def _command_signature(self, observation: Mapping[str, Any]) -> tuple:
        ship = mapping(field(observation, "shuttle"), "shuttle")
        health = mapping(field(observation, "sensor_health"), "sensor_health")
        return (
            self._epoch_key(observation),
            scalar(field(observation, "time_s"), "time_s"),
            count(field(observation, "tuning_revision"), "tuning_revision", maximum=0xFFFFFFFF),
            count(field(health, "revision"), "sensor revision", maximum=0xFFFFFFFF),
            scalar(field(observation, "navigation_sample_time_s"), "navigation time"),
            point(field(ship, "position"), "shuttle.position"),
            point(field(ship, "velocity"), "shuttle.velocity"),
            tuple(vector(field(ship, "rotation"), "shuttle.rotation", length=4)),
        )

    def _inflate(self, frame: PlanningFrame) -> PlanningFrame:
        observation, estimate = self._observation, self._estimate
        ship = observation["shuttle"]
        raw_position = point(ship["position"], "shuttle.position")
        raw_velocity = point(ship["velocity"], "shuttle.velocity")
        horizon = frame.forecast_horizon_s
        end = self._estimator.estimate(self._now + horizon)
        base = distance(estimate.position_m, raw_position) + end.position_uncertainty_m
        rows = radar_rows(observation["radar"])
        if len(frame.obstacles) != len(rows) + 3 or frame.measured_obstacles != len(rows):
            raise ValueError("Planning frame does not match the selected anonymous radar envelopes")
        # This allowance depends on the source clock, not the anonymous row.
        # A selected scan normally has one timestamp. Do not repeat the same
        # ownship-history calculation for every one of its hundreds of rows.
        history = {
            float(timestamp): self._estimator.backdating_allowance(
                float(timestamp), self._now, raw_position, raw_velocity, horizon
            )
            for timestamp in np.unique(rows[:, 2, 0])
        }
        allowances = [base] * 3 + [base + history[float(row[2, 0])] for row in rows]
        obstacles = tuple(
            Capsule(obstacle.from_m, obstacle.to_m, obstacle.radius_m + allowance)
            for obstacle, allowance in zip(frame.obstacles, allowances, strict=True)
        )
        return replace(frame, obstacles=obstacles)

    def frame_for_planning(self, frame: PlanningFrame) -> PlanningFrame:
        """Inflate a current selected-channel frame, never shift/shrink its capsules.

        History only adds uncertainty around the original radial forecast. This
        method registers the exact returned frame object for later route checks.
        An edited predictor must retain one envelope per anonymous radar row.
        The endpoint is inset inside the unchanged public arrival region rather
        than letting the controller deadband consume its entire nominal margin.
        """
        if not self._ready or not isinstance(frame, PlanningFrame):
            raise ValueError("Eligible public sensors are required before planning")
        if (
            not isinstance(frame.config, PlannerConfig)
            or scalar(frame.forecast_horizon_s, "forecast_horizon_s")
            != frame.config.forecast_horizon_s
        ):
            raise ValueError("Planning frame must retain its configured forecast horizon")
        if (
            frame.source_time_s != self._now
            or frame.source_seed != self._epoch[0]
            or frame.reference_frame != self._epoch[3]
        ):
            raise ValueError("Planning frame belongs to another public observation")
        rows = radar_rows(self._observation["radar"])
        if frame.radar_fingerprint != rows.tobytes():
            raise ValueError("Planning frame did not preserve the selected actual radar input")
        oldest, newest = float(min(rows[:, 2, 0])), float(max(rows[:, 2, 0]))
        if frame.oldest_measurement_time_s != oldest or frame.newest_measurement_time_s != newest:
            raise ValueError("Planning frame changed the genuine sample timestamps")
        expires = min(
            self._now + min(self.config.max_route_age_s, frame.forecast_horizon_s),
            oldest
            + min(
                self.config.radar_age_s,
                frame.config.max_measurement_age_s,
                frame.forecast_horizon_s,
            ),
            self._estimate.source_time_s + self.config.navigation_age_s,
        )
        if expires < self._now + self._lease:
            raise ValueError("Planning horizon cannot support the current decision lease")
        inflated = self._inflate(frame)
        if not frame.at_waypoint:
            end = self._estimator.estimate(self._now + self._lease)
            inset = max(self.config.arrival_inset_m, 2.0 * end.position_uncertainty_m + 0.01)
            radius = frame.goal_radius_m - inset
            if radius <= 0.0:
                raise ValueError("Checkpoint region cannot fit the ownship arrival allowance")
            approach = np.array(frame.goal_world_m) - frame.goal_center_m
            length = math.hypot(*approach)
            if length == 0.0:
                raise ValueError("Checkpoint approach has no outward direction")
            goal = point(
                np.array(frame.goal_center_m) + approach / length * min(radius, length),
                "interior checkpoint approach",
            )
            if not segment_clear(goal, goal, inflated.obstacles):
                raise ValueError("Interior checkpoint approach intersects an uncertainty envelope")
            inflated = replace(inflated, goal_world_m=goal)
            self._report["arrival_goal_inset_m"] = frame.goal_radius_m - distance(
                goal, frame.goal_center_m
            )
            self._report["arrival_goal_kind"] = "interior_candidate_not_mission_success"
        self._frames.append(_CheckedFrame(inflated, self._generation, expires))
        self._need_plan = False
        self._report["requires_replan"] = False
        self._report["planning_uncertainty_kind"] = "additive_ownship_and_history_model_allowances"
        return inflated

    def _coast(self, reason: str, *, replan: bool = True):
        self._need_plan |= replan
        self._next_waypoint = None
        self._decision = "zero_thrust_not_braking_or_guaranteed_avoidance"
        self._decision_time = self._now
        self._report.update(
            mode="coast",
            reason=reason,
            can_command=False,
            requires_replan=self._need_plan,
            decision=self._decision,
            decision_source_time_s=self._decision_time,
            next_waypoint_world_m=None,
            waypoint_index=self._waypoint,
        )
        return coast_command()

    @staticmethod
    def _cross_track(position, start, end) -> float:
        delta = np.array(end) - start
        length = math.hypot(*delta)
        if length == 0.0:
            return distance(point(position, "position"), point(end, "waypoint"))
        direction = delta / length
        along = min(length, max(0.0, float(np.dot(np.array(position) - start, direction))))
        return math.hypot(*(np.array(position) - (np.array(start) + direction * along)))

    def _departure_obstacles(self, frame: PlanningFrame, route: RoutePlan, trajectory):
        if not route.frame.has_surface_departure or self._waypoint != 1:
            return frame.obstacles
        # Preserve only the original planner's explicit radial-departure exception.
        # This is still not a terrain model. Any sideways or inward departure coasts.
        origin = np.array(route.frame.departure_world_m[0])
        if tuple(route.points_world_m[1]) != route.frame.departure_world_m[-1]:
            raise ValueError("The special departure waypoint was changed")
        axis = origin / math.hypot(*origin)
        previous = math.hypot(*trajectory[0])
        for position in trajectory:
            radius = math.hypot(*position)
            across = math.hypot(*(np.array(position) - axis * float(np.dot(position, axis))))
            if radius < previous or across > self._estimate.position_uncertainty_m + 0.01:
                raise ValueError("Departure is not a supported outward radial segment")
            previous = radius
        return frame.obstacles[1:]

    def _velocity_command(self, route, direction, velocity, speed, response, maximum, gain):
        requested = (direction * speed - velocity) * response
        if route.frame.has_surface_departure and self._waypoint == 1:
            origin = np.array(route.frame.departure_world_m[0])
            axis = origin / math.hypot(*origin)
            lateral_velocity = velocity - axis * float(np.dot(velocity, axis))
            # Preserve the radial-prefix exception rather than allowing a turn.
            requested += lateral_velocity * (response - 1.0 / (gain * self._lease))
        return _bounded(requested, maximum)

    def _curve_allowance(self, acceleration, maximum, gain):
        return (
            math.hypot(*(acceleration * gain)) * self._lease * self._lease / 32.0
            + 0.5
            * (self._estimator.accel_allowance_mps2 + 2.0 * maximum * gain)
            * self._lease
            * self._lease
        )

    def _stopping_corridor(
        self,
        current,
        route,
        position,
        velocity,
        direction,
        waypoint,
        speed,
        acceleration,
        braking,
        maximum,
        gain,
        usable_distance,
    ):
        """Check one bounded cruise candidate, not a renewed route or lease.

        Reserve a straight cruise/brake connector and the actual next lease's
        curved motion followed by nominal active braking. All radar and ownship
        allowances are rebuilt for the entire longer horizon. The brake model
        assumes future eligible control; sensor loss still coasts, not brakes.
        """
        end_velocity = velocity + acceleration * gain * self._lease
        peak = max(speed, math.hypot(*velocity), math.hypot(*end_velocity))
        peak += self._estimate.velocity_uncertainty_mps
        horizon = max(current.forecast_horizon_s, self._lease + peak / braking)
        diagnostic = {"speed_mps": speed, "horizon_s": horizon}
        if horizon > self.config.max_stopping_horizon_s:
            return False, {**diagnostic, "reason": "stopping horizon budget"}
        travel = peak * self._lease + peak * peak / (2.0 * braking)
        if travel > usable_distance:
            return False, {**diagnostic, "reason": "stopping connector exceeds waypoint leg"}
        connector = point(position + direction * travel, "cruise stopping connector")
        immediate = tuple(
            point(position + velocity * t + 0.5 * acceleration * gain * t * t, "cruise lease")
            for t in (0.0, self._lease * 0.5, self._lease)
        )
        stopping = math.hypot(*end_velocity) / braking
        # Collinear constant braking traces a straight geometric segment. Two
        # more points retain the time-varying model for corridor/departure checks.
        trajectory = immediate
        if stopping > 0.0:
            trajectory += tuple(
                point(
                    np.array(immediate[-1]) + end_velocity * t * (1.0 - 0.5 * t / stopping),
                    "active braking trajectory",
                )
                for t in (stopping * 0.5, stopping)
            )
        if any(
            self._cross_track(value, position, waypoint) > self.config.max_cross_track_m
            for value in trajectory
        ):
            return False, {**diagnostic, "reason": "stopping path leaves route corridor"}
        # Reject known-body intersections before rebuilding hundreds of radar
        # capsules. This is only an early veto; acceptance always checks all rows.
        short_end = self._estimator.estimate(self._now + current.forecast_horizon_s)
        extra = self._estimator.estimate(self._now + horizon).position_uncertainty_m
        extra -= short_end.position_uncertainty_m
        known = replace(
            current,
            obstacles=tuple(
                Capsule(item.from_m, item.to_m, item.radius_m + extra)
                for item in current.obstacles[:3]
            ),
        )
        allowance = self._curve_allowance(acceleration, maximum, gain)
        try:
            known_obstacles = self._departure_obstacles(known, route, (*trajectory, connector))
        except ValueError as error:
            return False, {**diagnostic, "reason": str(error)}
        if not CollisionIndex(known_obstacles).clear(point(position, "estimate"), connector):
            return False, {**diagnostic, "reason": "long forecast known-body envelope"}
        settings = replace(current.config, forecast_horizon_s=horizon)
        try:
            forecast = self._inflate(predict_obstacles(self._observation, settings))
            obstacles = self._departure_obstacles(forecast, route, (*trajectory, connector))
        except ValueError as error:
            return False, {**diagnostic, "reason": str(error)}
        swept = CollisionIndex(
            tuple(Capsule(item.from_m, item.to_m, item.radius_m + allowance) for item in obstacles)
        )
        if not swept.clear(point(position, "estimate"), connector) or any(
            not swept.clear(a, b) for a, b in zip(trajectory[:-1], trajectory[1:], strict=True)
        ):
            return False, {**diagnostic, "reason": "long forecast stopping/swept envelope"}
        return True, {
            **diagnostic,
            "reason": "checked longer stopping corridor",
            "distance_m": travel,
        }

    def command(self, observation, route, gains) -> tuple[np.ndarray, np.ndarray]:
        """Return capped body-frame vectors using the persistent ownship estimate.

        The exact latest update and an unexpired route from a registered inflated
        frame are required. Commands never consume telemetry, debug rays, camera
        pixels or private debris truth. Legacy radar repulsion gains are not used.
        """
        if not self._ready or self._recovering or self._consent_hold:
            return self._coast("Sensor readiness, recovery or consent gate requires coasting")
        try:
            observation = mapping(observation, "observation")
            if self._command_signature(observation) != self._signature:
                return self._coast("Decision input is not the exact latest public source context")
            if not isinstance(route, RoutePlan):
                return self._coast("No current checked Python route")
            checked = next((item for item in self._frames if item.frame is route.frame), None)
            if checked is None or checked.generation != self._generation:
                return self._coast("Route was not checked for this sensor selection and revision")
            if checked.expires_at_s < self._now + self._lease:
                return self._coast("Route age or forecast horizon cannot support the whole lease")
            if route.status == "at_waypoint":
                return self._coast(
                    "Inside the reported waypoint region; no arrival braking is implied",
                    replan=False,
                )
            if (
                route.status != "candidate"
                or not 2 <= len(route.points_world_m) <= MAX_PREVIEW_POINTS
            ):
                return self._coast("No complete candidate route")
            if route is not self._route:
                # A refreshed source frame may retain the same geometric suffix.
                # Do not revisit already passed corners just because it is a new
                # RoutePlan object. A changed suffix starts a new checked route.
                same_suffix = self._route is not None and np.array_equal(
                    self._route.points_world_m[1:], route.points_world_m[1:]
                )
                self._waypoint = self._waypoint if same_suffix else 1
                self._route = route
            position = np.array(self._estimate.position_m)
            velocity = np.array(self._estimate.velocity_mps)
            gains = mapping(gains, "gains")
            arrival = scalar(
                gains.get("waypoint_arrival_m", 20.0), "waypoint_arrival_m", positive=True
            )
            kp = scalar(gains.get("position_gain_s2", 0.0012), "position_gain_s2")
            kv = scalar(gains.get("velocity_gain_s", 0.07), "velocity_gain_s")
            points = route.points_world_m
            while (
                self._waypoint < len(points) - 1
                and math.hypot(*(points[self._waypoint] - position)) <= arrival
            ):
                self._waypoint += 1
            waypoint = points[self._waypoint]
            delta = waypoint - position
            length = math.hypot(*delta)
            if length <= self._estimate.position_uncertainty_m:
                return self._coast("Waypoint is within ownship position uncertainty", replan=False)
            cross_track = self._cross_track(position, points[self._waypoint - 1], waypoint)
            if cross_track > self.config.max_cross_track_m:
                return self._coast("Ownship left the supported route corridor")
            gain = self._actuator_gain(self._observation)
            degraded = self._selected == "radar_backup"
            maximum = min(
                0.5,
                self.config.degraded_max_accel_mps2 if degraded else self.config.max_accel_mps2,
                scalar(field(self._observation, "max_accel_mps2"), "max_accel_mps2"),
            )
            scalar(field(self._observation, "max_angular_accel_rad_s2"), "max_angular_accel_rad_s2")
            braking = min(self.config.braking_accel_mps2, 0.5 * maximum * min(1.0, gain))
            remaining_horizon = (
                route.frame.source_time_s + route.frame.forecast_horizon_s - self._now
            )
            stopping_time = max(0.0, remaining_horizon - self._lease)
            usable_distance = max(0.0, length - self._estimate.position_uncertainty_m)
            speed_limits = {
                "configured": self.config.degraded_max_speed_mps
                if degraded
                else self.config.max_speed_mps,
                "waypoint_braking_distance": math.sqrt(2.0 * braking * usable_distance),
                "forecast_braking_time": max(
                    0.0, braking * stopping_time - self._estimate.velocity_uncertainty_mps
                ),
                "position_response": kp * usable_distance / kv if kv > 0.0 else 0.0,
            }
            speed = min(speed_limits.values())
            route_distance = usable_distance + sum(
                math.hypot(*(b - a))
                for a, b in zip(
                    points[self._waypoint : -1], points[self._waypoint + 1 :], strict=True
                )
            )
            if kp == 0.0 or kv == 0.0 or maximum == 0.0:
                return self._coast(
                    "Configured controller gains or actuator limit disable thrust", replan=False
                )
            # Avoid overshooting the desired velocity within one lease under
            # the command/gain model. The dynamics allowance still covers only
            # assumptions, not contacts or a guaranteed acceleration bound.
            response = min(kv, 1.0 / (gain * self._lease))
            direction = delta / length
            acceleration = self._velocity_command(
                route, direction, velocity, speed, response, maximum, gain
            )
            current = self._inflate(predict_obstacles(self._observation, route.frame.config))
            try:
                connector_obstacles = self._departure_obstacles(
                    current, route, (point(position, "estimate"), point(waypoint, "waypoint"))
                )
            except ValueError as error:
                return self._coast(str(error))
            if not CollisionIndex(connector_obstacles).clear(
                point(position, "estimate"), point(waypoint, "waypoint")
            ):
                return self._coast("Current route connector intersects an uncertainty envelope")
            short_speed = speed
            command_horizon = remaining_horizon
            trials = []
            cruise_limit = min(
                8.0,
                speed_limits["configured"],
                speed_limits["waypoint_braking_distance"],
                speed_limits["position_response"],
            )
            candidates = sorted(
                {min(value, cruise_limit) for value in _CRUISE_CANDIDATES_MPS}, reverse=True
            )
            for candidate in candidates:
                if candidate <= short_speed:
                    continue
                requested = self._velocity_command(
                    route, direction, velocity, candidate, response, maximum, gain
                )
                # Reserve stopping distance before an intermediate waypoint's
                # normal arrival gate can advance to a different route leg.
                available = usable_distance - (arrival if self._waypoint < len(points) - 1 else 0)
                clear, trial = self._stopping_corridor(
                    current,
                    route,
                    position,
                    velocity,
                    direction,
                    waypoint,
                    candidate,
                    requested,
                    braking,
                    maximum,
                    gain,
                    max(0.0, available),
                )
                trials.append(trial)
                if clear:
                    speed, acceleration = candidate, requested
                    command_horizon = trial["horizon_s"]
                    break
            active_braking = False
            current_speed = math.hypot(*velocity)
            current_upper_speed = current_speed + self._estimate.velocity_uncertainty_mps
            if (
                speed == short_speed
                and current_speed > short_speed + self._estimate.velocity_uncertainty_mps
                and current_upper_speed > braking * stopping_time
            ):
                # A lost long corridor is not permission to keep cruising. With
                # eligible sensors, request the modeled braking rate now rather
                # than relying on the slow transit pole. Fault/loss gates above
                # still coast, and immediate clearance below can veto this too.
                acceleration = _bounded(
                    -velocity / current_speed * max(braking / gain, math.hypot(*acceleration)),
                    maximum,
                )
                active_braking = True
            self._report.update(
                desired_speed_mps=speed,
                short_forecast_speed_mps=short_speed,
                short_forecast_speed_limits_mps=speed_limits,
                speed_limits_mps={
                    **speed_limits,
                    "forecast_braking_time": max(
                        0.0,
                        braking * (command_horizon - self._lease)
                        - self._estimate.velocity_uncertainty_mps,
                    ),
                },
                speed_limiter="adaptive_stopping_corridor"
                if speed > short_speed
                else min(speed_limits, key=speed_limits.get),
                planning_forecast_horizon_s=route.frame.forecast_horizon_s,
                forecast_remaining_s=remaining_horizon,
                command_stopping_horizon_s=command_horizon,
                command_stop_position_allowance_m=self._estimator.estimate(
                    self._now + command_horizon
                ).position_uncertainty_m,
                modeled_stopping_time_s=max(0.0, command_horizon - self._lease),
                stopping_horizon_limit_s=self.config.max_stopping_horizon_s,
                stopping_candidates=trials,
                active_braking_to_short_forecast=active_braking,
                braking_model_accel_mps2=braking,
                braking_model_kind="assumed_active_braking_not_fault_coasting",
                command_lookahead_kind="public_envelopes_not_complete_debris_or_dynamics_bounds",
                route_remaining_distance_m=route_distance,
                constant_speed_route_time_s=route_distance / speed if speed > 0.0 else None,
                route_time_kind="current_speed_only_not_an_arrival_prediction",
            )
            # The curve is approximated by two chords. Inflate their obstacles by
            # the constant-acceleration chord error plus the explicit dynamics
            # allowance. Contacts and unbounded Gaussian tails remain unsupported.
            duration = self._lease
            applied_model = acceleration * gain
            trajectory = tuple(
                point(position + velocity * t + 0.5 * applied_model * t * t, "command trajectory")
                for t in (0.0, duration * 0.5, duration)
            )
            try:
                obstacles = self._departure_obstacles(current, route, trajectory)
            except ValueError as error:
                return self._coast(str(error))
            curve_allowance = self._curve_allowance(acceleration, maximum, gain)
            swept = CollisionIndex(
                tuple(
                    Capsule(item.from_m, item.to_m, item.radius_m + curve_allowance)
                    for item in obstacles
                )
            )
            if any(
                not swept.clear(a, b) for a, b in zip(trajectory[:-1], trajectory[1:], strict=True)
            ):
                return self._coast(
                    "Immediate swept command path intersects an uncertainty envelope"
                )
            if any(
                self._cross_track(value, position, waypoint) > self.config.max_cross_track_m
                for value in trajectory
            ):
                return self._coast("Immediate command would leave the checked route corridor")
            body = _bounded(
                reference_to_body(acceleration, self._estimate.rotation_xyzw, "active_xyzw"),
                maximum,
            )
            self._next_waypoint = point(waypoint, "next waypoint")
            self._decision = "bounded_waypoint_velocity_following"
            self._decision_time = self._now
            self._need_plan = False
            self._report.update(
                mode="degraded" if degraded else "nominal",
                reason="Current checked route and immediate command envelope passed model gates",
                can_command=True,
                decision=self._decision,
                next_waypoint_world_m=waypoint.tolist(),
                waypoint_index=self._waypoint,
                route_points=len(points),
                desired_speed_mps=speed,
                velocity_response_s=response,
                reference_accel_limit_mps2=maximum,
                braking_model_accel_mps2=braking,
                surface_departure=route.frame.has_surface_departure,
                terrain_clearance_verified=False,
                decision_source_time_s=self._now,
                decision_valid_until_s=self._now + self._lease,
                route_source_time_s=route.frame.source_time_s,
                route_valid_until_s=checked.expires_at_s,
                requires_replan=self._need_plan,
            )
            return body, np.zeros(3, dtype=np.float64)
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._coast("Malformed, stale or unsupported route/controller input")
