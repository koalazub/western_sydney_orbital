"""Versioned algorithm defaults, with no engine or scenario-file reads."""

from __future__ import annotations

from dataclasses import dataclass

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
