"""Versioned algorithm defaults, with no engine or scenario-file reads."""

from __future__ import annotations

from dataclasses import dataclass, field

from ._validation import count, scalar


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """Defaults ported from the checked-in Phobos escape scenario.

    Noise values are planning assumptions. A public ``sensor_model``, when
    supplied, sets lower bounds on them. The reference-command observer allowance
    is not a bound on gravity, contacts or debris
    acceleration. ``max_heap_entries`` is a separate Python storage budget for
    heapq's duplicate entries, not an engine frame-work setting.
    """

    cell_size_m: float = 40.0
    corridor_half_width_m: float = 800.0
    max_nodes: int = 50_000
    max_expansions: int = 12_000
    max_heap_entries: int = 100_000
    clearance_m: float = 8.0
    forecast_horizon_s: float = 3.6
    max_measurement_age_s: float = 5.4
    unknown_tangential_speed_mps: float = 12.0
    sigma_multiplier: float = 3.0
    range_noise_m: float = 2.0
    range_rate_noise_mps: float = 0.02
    diameter_noise_fraction: float = 0.05
    observer_accel_allowance_mps2: float = 0.5

    def __post_init__(self) -> None:
        for name in ("cell_size_m", "corridor_half_width_m"):
            object.__setattr__(self, name, scalar(getattr(self, name), name, positive=True))
        for name in ("max_nodes", "max_expansions", "max_heap_entries"):
            object.__setattr__(self, name, count(getattr(self, name), name, minimum=1))
        for name in (
            "clearance_m",
            "forecast_horizon_s",
            "max_measurement_age_s",
            "unknown_tangential_speed_mps",
            "sigma_multiplier",
            "range_noise_m",
            "range_rate_noise_mps",
            "diameter_noise_fraction",
            "observer_accel_allowance_mps2",
        ):
            object.__setattr__(self, name, scalar(getattr(self, name), name))
        if self.sigma_multiplier * self.diameter_noise_fraction >= 1.0:
            raise ValueError("sigma_multiplier * diameter_noise_fraction must be < 1")


@dataclass(frozen=True, slots=True)
class ResilienceConfig:
    """Model assumptions and finite budgets for the opt-in resilient controller.

    These allowances are not calibrated sensor covariances or certified bounds
    on gravity, contacts or Gaussian noise. The navigation input is the local
    engine's unreplicated direct public state. Effective radar/navigation age is
    capped by the planner's radar-age limit. A particular planning frame may
    tighten it further, but can never relax it.

    The resilient default forecasts six seconds rather than the legacy preview's
    3.6. With a fresh 0.75-second lease, 0.25 m/s² modeled braking and 0.05 m/s
    velocity allowance, the short-forecast fallback speed cap is 1.2625 m/s.
    Faster cruise requires a separate, longer stopping-corridor forecast, bounded
    by ``max_stopping_horizon_s``. It retains every obstacle allowance and does
    not extend sensor freshness, route validity or the command lease.

    Time-indexed local motion has a separate nominal braking ceiling. It is
    also capped below current available actuator authority,
    without reducing the independent uncertainty or clearance allowances.
    """

    planner: PlannerConfig = field(default_factory=lambda: PlannerConfig(forecast_horizon_s=6.0))
    lease_sim_s: float = 0.75
    max_radar_age_s: float = 1.5
    max_navigation_age_s: float = 1.5
    max_route_age_s: float = 1.0
    wall_no_progress_s: float = 2.0
    history_window_s: float = 10.0
    history_limit: int = 256
    recovery_samples: int = 3
    recovery_dwell_s: float = 0.5
    position_allowance_m: float = 0.25
    velocity_allowance_mps: float = 0.05
    model_accel_allowance_mps2: float = 0.5
    max_position_uncertainty_m: float = 20.0
    max_velocity_uncertainty_mps: float = 3.0
    max_speed_mps: float = 8.0
    degraded_max_speed_mps: float = 3.0
    max_accel_mps2: float = 0.5
    degraded_max_accel_mps2: float = 0.25
    braking_accel_mps2: float = 0.25
    local_braking_accel_mps2: float = 0.49
    max_cross_track_m: float = 20.0
    arrival_inset_m: float = 1.0
    max_stopping_horizon_s: float = 40.0

    def __post_init__(self) -> None:
        if not isinstance(self.planner, PlannerConfig):
            raise ValueError("planner must be a PlannerConfig")
        for name in (
            "lease_sim_s",
            "max_radar_age_s",
            "max_navigation_age_s",
            "max_route_age_s",
            "wall_no_progress_s",
            "history_window_s",
            "recovery_dwell_s",
            "max_position_uncertainty_m",
            "max_velocity_uncertainty_mps",
            "max_speed_mps",
            "degraded_max_speed_mps",
            "max_accel_mps2",
            "degraded_max_accel_mps2",
            "braking_accel_mps2",
            "local_braking_accel_mps2",
            "max_cross_track_m",
            "arrival_inset_m",
            "max_stopping_horizon_s",
        ):
            object.__setattr__(self, name, scalar(getattr(self, name), name, positive=True))
        for name in (
            "position_allowance_m",
            "velocity_allowance_mps",
            "model_accel_allowance_mps2",
        ):
            object.__setattr__(self, name, scalar(getattr(self, name), name))
        object.__setattr__(
            self, "history_limit", count(self.history_limit, "history_limit", minimum=2)
        )
        object.__setattr__(
            self, "recovery_samples", count(self.recovery_samples, "recovery_samples", minimum=2)
        )
        if not 0.05 <= self.lease_sim_s <= 1.0:
            raise ValueError("lease_sim_s must be between 0.05 and 1.0")
        if self.history_limit > 4096 or self.history_window_s > 300.0:
            raise ValueError("Ownship history is limited to 4096 samples and 300 seconds")
        if self.recovery_samples > self.history_limit or self.recovery_dwell_s > 300.0:
            raise ValueError("Recovery must fit the history count and a 300-second dwell")
        if self.position_allowance_m > self.max_position_uncertainty_m:
            raise ValueError("position_allowance_m exceeds the uncertainty limit")
        if self.velocity_allowance_mps > self.max_velocity_uncertainty_mps:
            raise ValueError("velocity_allowance_mps exceeds the uncertainty limit")
        if self.max_accel_mps2 > 0.5 or self.degraded_max_accel_mps2 > self.max_accel_mps2:
            raise ValueError("Acceleration limits must not exceed the 0.5 reference cap")
        if self.degraded_max_speed_mps > self.max_speed_mps:
            raise ValueError("Degraded speed must not exceed nominal speed")
        if self.braking_accel_mps2 > self.max_accel_mps2:
            raise ValueError("braking_accel_mps2 must not exceed max_accel_mps2")
        if self.local_braking_accel_mps2 > 0.5:
            raise ValueError("local_braking_accel_mps2 must not exceed the reference command cap")
        if self.max_stopping_horizon_s > 60.0:
            raise ValueError("max_stopping_horizon_s must not exceed 60 seconds")

    @property
    def radar_age_s(self) -> float:
        return min(self.max_radar_age_s, self.planner.max_measurement_age_s)

    @property
    def navigation_age_s(self) -> float:
        return min(self.max_navigation_age_s, self.planner.max_measurement_age_s)
