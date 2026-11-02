"""Deterministic synthetic validation checks for the reference control modules.

All gains in this file are example test inputs, not competition recommendations.
The tracker scenarios are synthetic constant-acceleration cases, not real
asteroid or debris forecasts, and no test uses wall-clock timing.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from wsorb.flight.control_validation import (
    MAX_HORIZON_S,
    MAX_SAMPLE_COUNT,
    PIDStepCase,
    TrackerScenario,
    compare_gamma_effect,
    validate_alpha_beta,
    validate_alpha_beta_forecast,
    validate_alpha_beta_gamma,
    validate_alpha_beta_gamma_forecast,
    validate_pid_step,
    validate_zero_gamma_reduction,
)
from wsorb.flight.pid import PIDConfig
from wsorb.flight.tracking_filter import AlphaBetaGains, AlphaBetaGammaTracker, AlphaBetaTracker


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


@pytest.mark.parametrize("kp", [0.5, 1.5, 2.0, 2.5])
def test_unsaturated_p_velocity_loop_has_the_discrete_step_multiplier(kp) -> None:
    case = PIDStepCase(PIDConfig(kp=kp), 1.0, 3.0, (0.001, 0.0, 0.0))
    result = validate_pid_step(case)
    expected_error = 0.001 * (1.0 - kp * case.dt_s) ** np.arange(4)
    np.testing.assert_allclose(0.001 - result.velocity_mps[:, 0], expected_error)
    assert result.metrics.saturation_fraction == 0.0
    if kp < 2.0:
        assert abs(expected_error[-1]) < abs(expected_error[0])
    elif kp == 2.0:
        assert abs(expected_error[-1]) == abs(expected_error[0])
    else:
        assert abs(expected_error[-1]) > abs(expected_error[0])


def test_filtered_position_rmse_is_not_the_measurement_free_forecast_error() -> None:
    # Alpha=1 can copy every exact measured position while learning no velocity.
    scenario = TrackerScenario(
        dt_s=1.0,
        horizon_s=1.0,
        gains=AlphaBetaGains(alpha=1.0, beta=0.0),
        true_position_m=(0.0, 0.0, 0.0),
        true_velocity_mps=(0.0, 0.0, 0.0),
        true_acceleration_mps2=(1.0, 0.0, 0.0),
    )
    result = validate_alpha_beta_forecast(scenario, prediction_horizon_s=2.0)
    assert result.conditioning.metrics.position_rmse_m == 0.0
    assert result.cutoff_s == 1.0
    assert result.prediction_horizon_s == 2.0
    np.testing.assert_array_equal(result.forecast.time_s, [0.0, 1.0, 2.0])
    np.testing.assert_array_equal(result.forecast.estimated_position_m[:, 0], [0.5, 0.5, 0.5])
    np.testing.assert_array_equal(result.forecast.position_error_m[:, 0], [0.0, -1.5, -4.0])
    # The cutoff sample is present for plotting but excluded from holdout RMSE.
    assert result.forecast.metrics.position_rmse_m == pytest.approx(math.sqrt(9.125))
    assert result.forecast.metrics.final_position_error_m == 4.0
    assert result.forecast.metrics.sample_count == 2
    assert result.forecast.metrics.horizon_s == 2.0


@pytest.mark.parametrize("use_gamma", [False, True])
def test_forecast_only_updates_from_measurements_through_the_cutoff(monkeypatch, use_gamma) -> None:
    scenario = TrackerScenario(
        dt_s=0.5,
        horizon_s=2.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.25, gamma=0.125),
        true_position_m=(10.0, -2.0, 3.0),
        true_velocity_mps=(0.5, 0.25, -0.125),
        true_acceleration_mps2=(0.0, 0.0, 0.0),
    )
    tracker_class = AlphaBetaGammaTracker if use_gamma else AlphaBetaTracker
    validate = validate_alpha_beta_gamma_forecast if use_gamma else validate_alpha_beta_forecast
    update = tracker_class.update
    received = []

    def record_update(self, measurement, dt_s):
        received.append(np.array(measurement))
        return update(self, measurement, dt_s)

    monkeypatch.setattr(tracker_class, "update", record_update)
    result = validate(scenario, prediction_horizon_s=3.0)
    assert len(received) == 4
    np.testing.assert_array_equal(received[-1], [11.0, -1.5, 2.75])
    np.testing.assert_array_equal(result.forecast.estimated_position_m[-1], [12.5, -0.75, 2.375])
    assert result.forecast.metrics.position_rmse_m == 0.0
    assert result.forecast.metrics.velocity_rmse_mps == 0.0
    assert result.cutoff_s == 2.0
    assert result.prediction_horizon_s == 3.0
    assert not result.forecast.position_error_m.flags.writeable
    assert not result.conditioning.estimated_position_m.flags.writeable


def test_ca_holdout_matches_analytical_truth_for_a_full_300_second_prediction() -> None:
    scenario = TrackerScenario(
        dt_s=1.0,
        horizon_s=2.0,
        gains=AlphaBetaGains(alpha=0.5, beta=0.25, gamma=0.125),
        true_position_m=(3.0, -4.0, 2.0),
        true_velocity_mps=(0.5, -0.25, 0.125),
        true_acceleration_mps2=(0.25, -0.125, 0.0625),
    )
    result = validate_alpha_beta_gamma_forecast(scenario, prediction_horizon_s=300.0)
    absolute_time = result.cutoff_s + result.forecast.time_s[:, None]
    expected = (
        np.asarray(scenario.true_position_m)
        + np.asarray(scenario.true_velocity_mps) * absolute_time
        + 0.5 * np.asarray(scenario.true_acceleration_mps2) * absolute_time**2
    )
    np.testing.assert_array_equal(result.forecast.estimated_position_m, expected)
    assert result.forecast.metrics.position_rmse_m == 0.0
    assert result.forecast.metrics.velocity_rmse_mps == 0.0
    assert result.forecast.metrics.acceleration_rmse_mps2 == 0.0
    assert result.cutoff_s == 2.0
    assert result.prediction_horizon_s == 300.0
    assert result.forecast.metrics.sample_count == 300


@pytest.mark.parametrize(
    "prediction_horizon_s", [0.0, -1.0, math.nan, math.inf, 0.75, MAX_HORIZON_S + 1.0]
)
def test_forecast_rejects_invalid_prediction_horizons(prediction_horizon_s) -> None:
    scenario = TrackerScenario(0.5, 1.0, AlphaBetaGains(0.5, 0.25), [0.0] * 3, [0.0] * 3, [0.0] * 3)
    for validate in (validate_alpha_beta_forecast, validate_alpha_beta_gamma_forecast):
        with pytest.raises(ValueError):
            validate(scenario, prediction_horizon_s=prediction_horizon_s)


def test_forecast_checks_combined_work_before_running_conditioning(monkeypatch) -> None:
    scenario = TrackerScenario(
        0.001, 60.0, AlphaBetaGains(0.5, 0.25), [0.0] * 3, [0.0] * 3, [0.0] * 3
    )

    def unexpected_update(*_args):
        raise AssertionError("Budget validation must precede any conditioning update")

    monkeypatch.setattr(AlphaBetaTracker, "update", unexpected_update)
    with pytest.raises(ValueError, match="conditioning plus forecast"):
        validate_alpha_beta_forecast(scenario, prediction_horizon_s=60.0)
    with pytest.raises(ValueError, match="conditioning must be"):
        validate_alpha_beta_forecast(object(), prediction_horizon_s=1.0)


def test_large_finite_errors_have_finite_norms_and_rmse() -> None:
    scenario = TrackerScenario(
        1.0,
        1.0,
        AlphaBetaGains(alpha=0.0, beta=0.0),
        (1.0e200, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        initial_position_m=(0.0, 0.0, 0.0),
    )
    metrics = validate_alpha_beta(scenario).metrics
    assert math.isfinite(metrics.position_rmse_m)
    assert metrics.position_rmse_m == pytest.approx(1.0e200)
    assert metrics.position_max_error_m == pytest.approx(1.0e200)
    pid = validate_pid_step(PIDStepCase(PIDConfig(kp=1.0), 1.0, 1.0, (1.0e200, 0.0, 0.0)))
    assert math.isfinite(pid.metrics.steady_state_error_mps)
    assert pid.metrics.steady_state_error_mps == pytest.approx(1.0e200)
    assert pid.metrics.settling_time_s is None


def test_validation_rejects_overflowing_truth_errors_and_step_differences() -> None:
    with pytest.raises(ValueError, match="PID velocity step"):
        PIDStepCase(
            PIDConfig(kp=1.0),
            1.0,
            1.0,
            (1.0e308, 0.0, 0.0),
            initial_velocity_mps=(-1.0e308, 0.0, 0.0),
        )
    overflowing_truth = TrackerScenario(
        1.0,
        2.0,
        AlphaBetaGains(0.0, 0.0),
        (1.0e308, 0.0, 0.0),
        (1.0e308, 0.0, 0.0),
        (0.0, 0.0, 0.0),
    )
    with pytest.raises(ValueError, match="tracker truth"):
        validate_alpha_beta(overflowing_truth)
    overflowing_error = TrackerScenario(
        1.0,
        1.0,
        AlphaBetaGains(0.0, 0.0),
        (1.0e308, 0.0, 0.0),
        (-1.0e308, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        initial_position_m=(-1.0e308, 0.0, 0.0),
        initial_velocity_mps=(1.0e308, 0.0, 0.0),
    )
    with pytest.raises(ValueError, match="tracker errors"):
        validate_alpha_beta(overflowing_error)


def test_forecast_rejects_overflow_after_finite_conditioning() -> None:
    conditioning = TrackerScenario(
        1.0,
        1.0,
        AlphaBetaGains(0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        initial_position_m=(8.0e307, 0.0, 0.0),
        initial_velocity_mps=(2.0e307, 0.0, 0.0),
    )
    for validate in (validate_alpha_beta_forecast, validate_alpha_beta_gamma_forecast):
        with pytest.raises(ValueError, match="tracker forecast"):
            validate(conditioning, prediction_horizon_s=10.0)
