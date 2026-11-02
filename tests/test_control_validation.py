"""Deterministic synthetic validation checks for the reference control modules.

All gains in this file are example test inputs, not competition recommendations.
The tracker scenarios are synthetic constant-acceleration cases, not real
asteroid or debris forecasts, and no test uses wall-clock timing.
"""

from __future__ import annotations

import math

import pytest

from wsorb.flight.control_validation import (
    MAX_HORIZON_S,
    MAX_SAMPLE_COUNT,
    PIDStepCase,
    TrackerScenario,
    compare_gamma_effect,
    validate_alpha_beta,
    validate_alpha_beta_gamma,
    validate_pid_step,
    validate_zero_gamma_reduction,
)
from wsorb.flight.pid import PIDConfig
from wsorb.flight.tracking_filter import AlphaBetaGains


def test_pid_proportional_step_matches_the_analytical_first_order_response() -> None:
    """With kp=1 and no integral/derivative, the discrete response is exact."""
    case = PIDStepCase(
        config=PIDConfig(kp=1.0),
        dt_s=0.01,
        horizon_s=6.0,
        target_velocity_mps=(0.1, 0.0, 0.0),
    )
    result = validate_pid_step(case)
    metrics = result.metrics

    # v_n = 0.1 * (1 - 0.99**n). The sampled 10-90% rise is 2.19 s and the
    # last sample outside the 2% band is at 3.89 s, so settling is 3.90 s.
    assert metrics.rise_time_s == pytest.approx(2.19, abs=0.02)
    assert metrics.settling_time_s == pytest.approx(3.90, abs=0.02)
    assert metrics.overshoot_fraction == 0.0
    assert metrics.saturation_fraction == 0.0
    assert metrics.steady_state_error_mps < 5.0e-4
    assert result.velocity_mps.shape == (601, 3)
    assert result.command_mps2.shape == (600, 3)
    assert result.time_s[-1] == pytest.approx(6.0)


def test_pid_pi_step_has_the_expected_second_order_overshoot() -> None:
    """kp=1, ki=1 is a lightly damped second-order response with integral zero."""
    case = PIDStepCase(
        config=PIDConfig(kp=1.0, ki=1.0),
        dt_s=0.01,
        horizon_s=25.0,
        target_velocity_mps=(0.1, 0.0, 0.0),
        settle_band_fraction=0.02,
    )
    metrics = validate_pid_step(case).metrics

    assert metrics.rise_time_s is not None
    assert 0.5 <= metrics.rise_time_s <= 2.0
    assert 0.15 <= metrics.overshoot_fraction <= 0.35
    assert metrics.settling_time_s is not None
    assert 6.0 <= metrics.settling_time_s <= 12.0
    assert metrics.saturation_fraction == 0.0


def test_pid_gain_sensitivity_reduces_rise_time_and_overshoot() -> None:
    """Holding ki fixed, a larger kp responds faster and with less overshoot."""
    slow = validate_pid_step(
        PIDStepCase(
            config=PIDConfig(kp=1.0, ki=1.0),
            dt_s=0.01,
            horizon_s=25.0,
            target_velocity_mps=(0.1, 0.0, 0.0),
        )
    ).metrics
    fast = validate_pid_step(
        PIDStepCase(
            config=PIDConfig(kp=2.0, ki=1.0),
            dt_s=0.01,
            horizon_s=25.0,
            target_velocity_mps=(0.1, 0.0, 0.0),
        )
    ).metrics

    assert slow.rise_time_s is not None
    assert fast.rise_time_s is not None
    assert fast.rise_time_s < slow.rise_time_s
    assert fast.overshoot_fraction < slow.overshoot_fraction
    assert slow.saturation_fraction == 0.0
    assert fast.saturation_fraction == 0.0


def test_pid_saturation_fraction_is_exact_for_a_constant_acceleration_cap() -> None:
    """A 0.5 m/s² cap produces constant acceleration for the whole 0.99 s run."""
    case = PIDStepCase(
        config=PIDConfig(kp=1.0, accel_limit_mps2=0.5),
        dt_s=0.01,
        horizon_s=0.99,
        target_velocity_mps=(1.0, 0.0, 0.0),
    )
    result = validate_pid_step(case)
    metrics = result.metrics

    assert metrics.saturation_fraction == 1.0
    assert metrics.max_command_norm_mps2 <= 0.5
    assert metrics.rise_time_s is None
    assert metrics.settling_time_s is None
    assert metrics.overshoot_fraction == 0.0
    assert result.velocity_mps[-1, 0] == pytest.approx(0.495)
    assert result.position_m[-1, 0] == pytest.approx(0.245025)


def test_pid_saturation_fraction_tracks_the_configured_cap() -> None:
    """The same 0.3 m/s step saturates a 0.1 m/s² cap but not a 0.5 m/s² cap."""
    low = validate_pid_step(
        PIDStepCase(
            config=PIDConfig(kp=1.0, accel_limit_mps2=0.1),
            dt_s=0.01,
            horizon_s=1.0,
            target_velocity_mps=(0.3, 0.0, 0.0),
        )
    ).metrics
    high = validate_pid_step(
        PIDStepCase(
            config=PIDConfig(kp=1.0, accel_limit_mps2=0.5),
            dt_s=0.01,
            horizon_s=1.0,
            target_velocity_mps=(0.3, 0.0, 0.0),
        )
    ).metrics

    assert low.saturation_fraction == 1.0
    assert high.saturation_fraction == 0.0
    assert low.max_command_norm_mps2 <= 0.1
    assert high.max_command_norm_mps2 <= 0.5


def test_alpha_beta_tracks_constant_velocity_exactly_when_initialised() -> None:
    """The constant-velocity model is an exact fixed point of alpha-beta."""
    scenario = TrackerScenario(
        dt_s=0.5,
        horizon_s=10.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.2),
        true_position_m=(10.0, -20.0, 5.0),
        true_velocity_mps=(0.1, 0.2, -0.05),
        true_acceleration_mps2=(0.0, 0.0, 0.0),
    )
    metrics = validate_alpha_beta(scenario).metrics

    assert metrics.position_max_error_m <= 1.0e-12
    assert metrics.velocity_rmse_mps <= 1.0e-12
    assert metrics.final_acceleration_error_mps2 == 0.0


def test_alpha_beta_gamma_tracks_constant_acceleration_exactly_when_initialised() -> None:
    """The constant-acceleration model is an exact fixed point of alpha-beta-gamma."""
    scenario = TrackerScenario(
        dt_s=0.1,
        horizon_s=30.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.5, gamma=0.1),
        true_position_m=(3.0, -4.0, 2.0),
        true_velocity_mps=(0.3, -0.2, 0.1),
        true_acceleration_mps2=(0.02, -0.01, 0.005),
    )
    metrics = validate_alpha_beta_gamma(scenario).metrics

    assert metrics.position_max_error_m <= 1.0e-12
    assert metrics.velocity_rmse_mps <= 1.0e-12
    assert metrics.acceleration_rmse_mps2 <= 1.0e-12


def test_zero_gamma_is_bitwise_equivalent_to_alpha_beta() -> None:
    """Forcing gamma to zero and zero initial acceleration removes the extra state."""
    scenario = TrackerScenario(
        dt_s=0.1,
        horizon_s=20.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.3, gamma=0.2),
        true_position_m=(10.0, -4.0, 2.0),
        true_velocity_mps=(0.2, 0.1, -0.05),
        true_acceleration_mps2=(0.03, -0.02, 0.01),
        initial_position_m=(9.5, -4.2, 2.1),
        initial_velocity_mps=(0.1, 0.15, -0.02),
    )
    reduction = validate_zero_gamma_reduction(scenario)

    assert reduction.bitwise_equal is True
    assert reduction.max_position_difference_m == 0.0
    assert reduction.max_velocity_difference_mps == 0.0
    assert reduction.max_acceleration_estimate_mps2 == 0.0
    assert reduction.sample_count == 200
    assert reduction.horizon_s == pytest.approx(20.0)


def test_gamma_reduces_synthetic_constant_acceleration_error_over_300_s() -> None:
    """A synthetic 300 s run quantifies gamma; it is not an asteroid forecast."""
    scenario = TrackerScenario(
        dt_s=0.1,
        horizon_s=300.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.5, gamma=0.1),
        true_position_m=(100.0, -50.0, 20.0),
        true_velocity_mps=(0.2, 0.05, -0.1),
        true_acceleration_mps2=(0.05, -0.03, 0.02),
        initial_acceleration_mps2=(0.0, 0.0, 0.0),
    )
    effect = compare_gamma_effect(scenario)

    assert effect.with_gamma.metrics.sample_count == 3000
    assert effect.with_gamma.metrics.horizon_s == pytest.approx(300.0)
    assert effect.with_gamma.time_s[-1] == pytest.approx(300.0)

    # The constant-acceleration target leaves alpha-beta-gamma converged, while
    # the gamma-zero baseline keeps the alpha-beta steady-state lag.
    assert effect.with_gamma.metrics.final_position_error_m < 1.0e-8
    assert effect.with_gamma.metrics.final_velocity_error_mps < 1.0e-8
    assert effect.with_gamma.metrics.final_acceleration_error_mps2 < 1.0e-8
    assert effect.without_gamma.metrics.final_position_error_m > 1.0e-4
    assert effect.without_gamma.metrics.final_velocity_error_mps > 1.0e-3
    assert effect.without_gamma.metrics.final_acceleration_error_mps2 > 1.0e-2
    assert 4.0e-4 <= effect.without_gamma.metrics.position_rmse_m <= 8.0e-4

    assert effect.position_rmse_change_m < -1.0e-4
    assert effect.velocity_rmse_change_mps < 0.0
    assert effect.acceleration_rmse_change_mps2 < 0.0


def test_pid_rejects_invalid_timing_horizons_and_sample_counts() -> None:
    target = (1.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.0, 1.0, target)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.01, 0.0, target)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.01, float("nan"), target)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.01, MAX_HORIZON_S + 0.1, target)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 1.0 / (MAX_SAMPLE_COUNT + 1), 1.0, target)
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.3, 1.0, target)


def test_pid_rejects_invalid_configs_gains_and_nonfinite_inputs() -> None:
    with pytest.raises(ValueError):
        PIDStepCase(object(), 0.01, 1.0, (1.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        PIDConfig(kp=-1.0)
    with pytest.raises(ValueError):
        PIDConfig(kp=float("nan"))
    with pytest.raises(ValueError):
        PIDStepCase(PIDConfig(kp=1.0), 0.01, 1.0, (math.nan, 0.0, 0.0))
    with pytest.raises(ValueError):
        PIDStepCase(
            PIDConfig(kp=1.0),
            0.01,
            1.0,
            (1.0, 0.0, 0.0),
            initial_position_m=(math.inf, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        PIDStepCase(
            PIDConfig(kp=1.0),
            0.01,
            1.0,
            (0.0, 0.0, 0.0),
            initial_velocity_mps=(0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        PIDStepCase(
            PIDConfig(kp=1.0),
            0.01,
            1.0,
            (1.0, 0.0, 0.0),
            settle_band_fraction=1.0,
        )


def test_tracker_rejects_invalid_gains_and_nonfinite_inputs() -> None:
    with pytest.raises(ValueError):
        AlphaBetaGains(alpha=-0.1, beta=0.5)
    with pytest.raises(ValueError):
        AlphaBetaGains(alpha=0.5, beta=0.5, gamma=1.1)
    with pytest.raises(ValueError):
        TrackerScenario(
            dt_s=0.1,
            horizon_s=1.0,
            gains=object(),
            true_position_m=(0.0, 0.0, 0.0),
            true_velocity_mps=(0.0, 0.0, 0.0),
            true_acceleration_mps2=(0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        TrackerScenario(
            dt_s=0.1,
            horizon_s=1.0,
            gains=AlphaBetaGains(alpha=0.5, beta=0.5),
            true_position_m=(math.nan, 0.0, 0.0),
            true_velocity_mps=(0.0, 0.0, 0.0),
            true_acceleration_mps2=(0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        TrackerScenario(
            dt_s=0.1,
            horizon_s=1.0,
            gains=AlphaBetaGains(alpha=0.5, beta=0.5),
            true_position_m=(0.0, 0.0, 0.0),
            true_velocity_mps=(0.0, 0.0, 0.0),
            true_acceleration_mps2=(0.0, 0.0, 0.0),
            initial_velocity_mps=(math.inf, 0.0, 0.0),
        )


def test_tracker_rejects_invalid_timing_horizons_and_sample_counts() -> None:
    gains = AlphaBetaGains(alpha=0.5, beta=0.5)
    with pytest.raises(ValueError):
        TrackerScenario(0.0, 1.0, gains, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        TrackerScenario(0.1, 0.0, gains, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        TrackerScenario(
            0.1,
            MAX_HORIZON_S + 0.1,
            gains,
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        TrackerScenario(
            1.0 / (MAX_SAMPLE_COUNT + 1),
            1.0,
            gains,
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0),
        )
    with pytest.raises(ValueError):
        TrackerScenario(0.3, 1.0, gains, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))


def test_gamma_effect_requires_a_positive_gamma() -> None:
    scenario = TrackerScenario(
        dt_s=0.1,
        horizon_s=1.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.5),
        true_position_m=(0.0, 0.0, 0.0),
        true_velocity_mps=(0.0, 0.0, 0.0),
        true_acceleration_mps2=(0.01, 0.0, 0.0),
    )
    with pytest.raises(ValueError):
        compare_gamma_effect(scenario)
