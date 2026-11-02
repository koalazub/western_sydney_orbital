"""Checks for local calibration and covariance reference math; not STP evidence."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.calibration import (
    FrameCovariance,
    estimate_process_noise,
    fit_measurement_calibration,
    predict_covariance,
    regularize_covariance,
    update_covariance,
)


def test_fit_recovers_known_bias_scale_and_noise_covariance() -> None:
    rng = np.random.default_rng(20261020)
    reference = rng.normal(size=(2000, 2))
    true_scale = np.array([1.5, -0.75])
    true_bias = np.array([2.0, -4.0])
    noise_covariance = np.array([[0.04, 0.012], [0.012, 0.09]])
    noise = rng.multivariate_normal(np.zeros(2), noise_covariance, size=2000)
    measured = reference * true_scale + true_bias + noise
    time_s = np.arange(2000) * 0.25

    calibration = fit_measurement_calibration(
        measured,
        reference,
        time_s,
        measurement_frame="world",
        reference_frame="world",
        timebase="mission_time",
    )

    np.testing.assert_allclose(calibration.scale, true_scale, rtol=0.05, atol=0.01)
    np.testing.assert_allclose(calibration.bias, true_bias, rtol=0.0, atol=0.03)
    np.testing.assert_allclose(
        calibration.residual_covariance.matrix,
        noise_covariance,
        rtol=0.15,
        atol=0.02,
    )
    assert calibration.sample_count == 2000
    assert calibration.frame == "world"
    assert calibration.timebase == "mission_time"
    assert calibration.residual_covariance.frame == "world"
    assert calibration.residual_covariance.timebase == "mission_time"

    residuals = measured - (reference * calibration.scale + calibration.bias)
    expected_covariance = residuals.T @ residuals / (reference.shape[0] - 1)
    np.testing.assert_allclose(
        calibration.residual_covariance.matrix,
        expected_covariance,
        rtol=1e-12,
        atol=1e-15,
    )

    inverse_scale = 1.0 / calibration.scale
    expected_corrected = expected_covariance * inverse_scale[:, None] * inverse_scale[None, :]
    np.testing.assert_allclose(
        calibration.corrected_covariance().matrix,
        expected_corrected,
        rtol=1e-12,
        atol=1e-15,
    )


def test_scalar_series_correction_preserves_one_dimensional_shape() -> None:
    reference = np.linspace(-3.0, 5.0, 12)
    measured = 2.5 * reference + 1.25
    time_s = np.arange(reference.size) * 0.1

    calibration = fit_measurement_calibration(
        measured,
        reference,
        time_s,
        measurement_frame="camera",
        reference_frame="camera",
        timebase="frame_clock",
    )

    assert calibration.bias.shape == (1,)
    assert calibration.scale.shape == (1,)
    np.testing.assert_allclose(calibration.scale, [2.5], rtol=1e-12)
    np.testing.assert_allclose(calibration.bias, [1.25], rtol=0.0, atol=1e-12)
    corrected = calibration.correct(measured, frame="camera")
    assert corrected.shape == (reference.size,)
    np.testing.assert_allclose(corrected, reference, rtol=0.0, atol=1e-12)


def test_regularization_loads_clips_and_reports_psd_repair() -> None:
    regularized = regularize_covariance(
        np.array([[1.0, 2.0], [2.0, 1.0]]),
        diagonal_loading=0.5,
        eigenvalue_floor=0.1,
    )

    assert regularized.clipped_eigenvalues == 1
    assert regularized.min_eigenvalue_before == pytest.approx(-1.0)
    assert regularized.min_eigenvalue_after == pytest.approx(0.1)
    assert regularized.condition_number_after == pytest.approx(35.0)
    assert np.linalg.eigvalsh(regularized.matrix).min() >= 0.1 - 1e-12
    np.testing.assert_allclose(regularized.matrix, regularized.matrix.T)

    rescued = regularize_covariance(np.zeros((2, 2)), diagonal_loading=0.01)
    assert rescued.clipped_eigenvalues == 0
    assert rescued.min_eigenvalue_after == pytest.approx(0.01)
    np.testing.assert_allclose(rescued.matrix, np.eye(2) * 0.01)


def test_regularization_rejects_singular_asymmetric_and_ill_conditioned() -> None:
    with pytest.raises(ValueError):
        regularize_covariance(np.zeros((2, 2)))
    with pytest.raises(ValueError):
        regularize_covariance(np.array([[2.0, 0.0], [1.0, 2.0]]))
    with pytest.raises(ValueError):
        regularize_covariance(np.diag([1.0, 1e-20]), condition_limit=1e6)
    with pytest.raises(ValueError):
        regularize_covariance(np.eye(2), diagonal_loading=-1.0)


def test_calibration_rejects_frame_timebase_and_missing_label_mismatch() -> None:
    reference = np.arange(6.0)
    measured = 2.0 * reference + 1.0
    time_s = np.arange(reference.size)

    calibration = fit_measurement_calibration(
        measured,
        reference,
        time_s,
        measurement_frame="body",
        reference_frame="body",
        timebase="sample_clock",
    )
    with pytest.raises(ValueError):
        calibration.correct(measured, frame="world")
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            measured,
            reference,
            time_s,
            measurement_frame="body",
            reference_frame="world",
            timebase="sample_clock",
        )
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            measured,
            reference,
            time_s,
            measurement_frame="",
            reference_frame="body",
            timebase="sample_clock",
        )
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            measured,
            reference,
            time_s,
            measurement_frame="body",
            reference_frame="body",
            timebase="",
        )
    with pytest.raises(ValueError):
        FrameCovariance("", "sample_clock", np.eye(1))
    with pytest.raises(ValueError):
        FrameCovariance("body", "", np.eye(1))


def test_calibration_rejects_nonfinite_too_few_and_shape_mismatch() -> None:
    reference = np.arange(6.0)
    measured = 2.0 * reference + 1.0
    time_s = np.arange(reference.size)

    too_few = slice(0, 2)
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            measured[too_few],
            reference[too_few],
            time_s[too_few],
            measurement_frame="body",
            reference_frame="body",
            timebase="sample_clock",
        )

    nonfinite_measured = measured.copy()
    nonfinite_measured[2] = np.nan
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            nonfinite_measured,
            reference,
            time_s,
            measurement_frame="body",
            reference_frame="body",
            timebase="sample_clock",
        )

    nonfinite_time = time_s.astype(np.float64)
    nonfinite_time[3] = np.inf
    with pytest.raises(ValueError):
        fit_measurement_calibration(
            measured,
            reference,
            nonfinite_time,
            measurement_frame="body",
            reference_frame="body",
            timebase="sample_clock",
        )

    with pytest.raises(ValueError):
        fit_measurement_calibration(
            np.zeros((6, 2)),
            np.zeros((6, 3)),
            time_s,
            measurement_frame="body",
            reference_frame="body",
            timebase="sample_clock",
        )

    with pytest.raises(ValueError):
        fit_measurement_calibration(
            np.zeros(6),
            np.zeros(6),
            time_s,
            measurement_frame="body",
            reference_frame="body",
            timebase="sample_clock",
        )

    with pytest.raises(ValueError):
        FrameCovariance("body", "sample_clock", np.array([[1.0, np.nan], [np.nan, 1.0]]))


def test_process_noise_recovers_known_transition_residual_covariance() -> None:
    rng = np.random.default_rng(7)
    transition = np.array([[1.0, 1.0], [0.0, 1.0]])
    noise_covariance = np.array([[0.25, 0.05], [0.05, 0.04]])
    noise = rng.multivariate_normal(np.zeros(2), noise_covariance, size=600)
    states = np.empty((601, 2))
    states[0] = [1.0, -2.0]
    for index in range(600):
        states[index + 1] = transition @ states[index] + noise[index]
    time_s = np.arange(601) * 0.5

    estimate = estimate_process_noise(
        time_s,
        states,
        transition,
        frame="world",
        timebase="mission_time",
    )

    assert estimate.sample_count == 600
    assert estimate.frame == "world"
    assert estimate.timebase == "mission_time"
    np.testing.assert_allclose(estimate.mean_residual, noise.mean(axis=0), atol=1e-12)
    expected = np.cov(noise, rowvar=False, ddof=1)
    np.testing.assert_allclose(estimate.covariance.matrix, expected, rtol=1e-12, atol=1e-12)
    np.testing.assert_allclose(estimate.covariance.matrix, noise_covariance, rtol=0.2, atol=0.02)


def test_process_noise_accepts_scalar_states_and_rejects_too_few_samples() -> None:
    rng = np.random.default_rng(11)
    increments = rng.normal(0.0, 0.3, size=80)
    states = np.concatenate(([0.0], np.cumsum(increments)))
    time_s = np.arange(states.size)

    estimate = estimate_process_noise(
        time_s,
        states,
        np.array([[1.0]]),
        frame="world",
        timebase="mission_time",
    )

    assert estimate.covariance.matrix.shape == (1, 1)
    assert estimate.mean_residual.shape == (1,)
    np.testing.assert_allclose(
        estimate.covariance.matrix[0, 0],
        np.var(increments, ddof=1),
        rtol=1e-12,
    )

    with pytest.raises(ValueError):
        estimate_process_noise(
            np.arange(3.0),
            np.zeros((3, 2)),
            np.eye(2),
            frame="world",
            timebase="mission_time",
        )


def test_process_noise_rejects_transition_shape_and_nonfinite_inputs() -> None:
    time_s = np.arange(6.0)
    states = np.zeros((6, 2))

    with pytest.raises(ValueError):
        estimate_process_noise(time_s, states, np.eye(3), frame="world", timebase="mission_time")
    with pytest.raises(ValueError):
        estimate_process_noise(
            time_s,
            states,
            np.tile(np.eye(2), (2, 1, 1)),
            frame="world",
            timebase="mission_time",
        )

    nonfinite_states = states.copy()
    nonfinite_states[2, 1] = np.inf
    with pytest.raises(ValueError):
        estimate_process_noise(
            time_s,
            nonfinite_states,
            np.eye(2),
            frame="world",
            timebase="mission_time",
        )

    with pytest.raises(ValueError):
        estimate_process_noise(
            time_s,
            states,
            np.full((2, 2), np.nan),
            frame="world",
            timebase="mission_time",
        )


def test_covariance_prediction_and_update_match_known_scalar_values() -> None:
    prior = FrameCovariance("world", "mission_time", np.array([[4.0]]))
    process = FrameCovariance("world", "mission_time", np.array([[0.25]]))
    predicted = predict_covariance(prior, np.array([[1.0]]), process)
    np.testing.assert_allclose(predicted.matrix, [[4.25]])
    assert predicted.matrix[0, 0] > prior.matrix[0, 0]
    assert predicted.frame == "world"
    assert predicted.timebase == "mission_time"

    measurement = FrameCovariance("world", "mission_time", np.array([[1.0]]))
    updated = update_covariance(prior, np.array([[1.0]]), measurement)
    np.testing.assert_allclose(updated.gain, [[0.8]])
    np.testing.assert_allclose(updated.innovation_covariance, [[5.0]])
    np.testing.assert_allclose(updated.posterior.matrix, [[0.8]])
    assert updated.posterior.frame == "world"
    assert updated.posterior.timebase == "mission_time"


def test_multivariate_covariance_update_uses_joseph_form() -> None:
    prior = FrameCovariance("world", "mission_time", np.diag([2.0, 3.0]))
    measurement = FrameCovariance("world", "mission_time", np.array([[1.0]]))
    observation = np.array([[1.0, 0.0]])

    updated = update_covariance(prior, observation, measurement)

    np.testing.assert_allclose(updated.innovation_covariance, [[3.0]])
    np.testing.assert_allclose(updated.gain, [[2.0 / 3.0], [0.0]])
    np.testing.assert_allclose(updated.posterior.matrix, np.diag([2.0 / 3.0, 3.0]))


def test_covariance_utilities_reject_frame_singular_and_shape_problems() -> None:
    prior = FrameCovariance("world", "mission_time", np.eye(2))
    process = FrameCovariance("world", "mission_time", np.eye(2))
    measurement = FrameCovariance("world", "mission_time", np.array([[1.0]]))

    with pytest.raises(ValueError):
        predict_covariance(prior, np.eye(2), FrameCovariance("body", "mission_time", np.eye(2)))
    with pytest.raises(ValueError):
        predict_covariance(prior, np.eye(2), FrameCovariance("world", "other_time", np.eye(2)))
    with pytest.raises(ValueError):
        predict_covariance(prior, np.eye(3), process)
    with pytest.raises(ValueError):
        predict_covariance(prior, np.full((2, 2), np.nan), process)

    singular = FrameCovariance("world", "mission_time", np.zeros((2, 2)))
    with pytest.raises(ValueError):
        predict_covariance(singular, np.eye(2), process)

    ill_conditioned = FrameCovariance("world", "mission_time", np.diag([1.0, 1e-20]))
    with pytest.raises(ValueError):
        predict_covariance(ill_conditioned, np.eye(2), process)

    with pytest.raises(ValueError):
        update_covariance(prior, np.ones((2, 1)), measurement)
    with pytest.raises(ValueError):
        update_covariance(
            prior,
            np.array([[1.0, 0.0]]),
            FrameCovariance("body", "mission_time", np.array([[1.0]])),
        )
    with pytest.raises(ValueError):
        update_covariance(
            prior,
            np.array([[1.0, 0.0]]),
            FrameCovariance("world", "mission_time", np.array([[0.0]])),
        )
