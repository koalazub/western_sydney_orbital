"""Pure tracker checks from the 29 October filter discussion; no forecast claim."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.tracking_filter import (
    AlphaBetaGains,
    AlphaBetaGammaTracker,
    AlphaBetaTracker,
)


def test_alpha_beta_matches_the_minutes_position_example() -> None:
    tracker = AlphaBetaTracker(
        [3.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        AlphaBetaGains(alpha=0.5, beta=0.5),
    )
    position, velocity = tracker.update([4.0, 0.0, 0.0], 1.0)
    np.testing.assert_allclose(position, [3.5, 0.0, 0.0])
    np.testing.assert_allclose(velocity, [0.5, 0.0, 0.0])


def test_zero_gamma_and_zero_acceleration_reduce_to_the_alpha_beta_update() -> None:
    gains = AlphaBetaGains(alpha=0.5, beta=0.5)
    alpha_beta = AlphaBetaTracker([3.0, 0.0, 0.0], [0.0, 0.0, 0.0], gains)
    alpha_beta_gamma = AlphaBetaGammaTracker(
        [3.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        gains,
    )
    expected_position, expected_velocity = alpha_beta.update([4.0, 0.0, 0.0], 1.0)
    position, velocity, acceleration = alpha_beta_gamma.update([4.0, 0.0, 0.0], 1.0)
    np.testing.assert_allclose(position, expected_position)
    np.testing.assert_allclose(velocity, expected_velocity)
    np.testing.assert_allclose(acceleration, [0.0, 0.0, 0.0])


def test_gamma_updates_acceleration_with_the_documented_convention() -> None:
    tracker = AlphaBetaGammaTracker(
        [3.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        AlphaBetaGains(alpha=0.5, beta=0.5, gamma=0.1),
    )
    position, velocity, acceleration = tracker.update([4.0, 0.0, 0.0], 1.0)
    np.testing.assert_allclose(position, [3.5, 0.0, 0.0])
    np.testing.assert_allclose(velocity, [0.5, 0.0, 0.0])
    np.testing.assert_allclose(acceleration, [0.2, 0.0, 0.0])
    assert np.isfinite(acceleration).all()


def test_trackers_reject_invalid_gains_and_intervals() -> None:
    with pytest.raises(ValueError):
        AlphaBetaGains(alpha=1.1, beta=0.5)
    with pytest.raises(ValueError):
        AlphaBetaTracker([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], AlphaBetaGains(0.5, 0.5)).update(
            [1.0, 0.0, 0.0], 0.0
        )


def test_zero_gamma_keeps_nonzero_acceleration_in_the_predictor() -> None:
    gains = AlphaBetaGains(alpha=0.5, beta=0.5, gamma=0.0)
    ab = AlphaBetaTracker([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], gains)
    abg = AlphaBetaGammaTracker([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [2.0, 0.0, 0.0], gains)
    ab_position, ab_velocity = ab.update([1.0, 0.0, 0.0], 1.0)
    position, velocity, acceleration = abg.update([1.0, 0.0, 0.0], 1.0)
    np.testing.assert_array_equal(position, [1.0, 0.0, 0.0])
    np.testing.assert_array_equal(velocity, [2.0, 0.0, 0.0])
    np.testing.assert_array_equal(acceleration, [2.0, 0.0, 0.0])
    assert not np.array_equal(position, ab_position)
    assert not np.array_equal(velocity, ab_velocity)


def test_zero_gamma_does_not_divide_by_an_underflowed_dt_squared() -> None:
    tracker = AlphaBetaGammaTracker(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [2.0, 0.0, 0.0],
        AlphaBetaGains(alpha=0.5, beta=0.5, gamma=0.0),
    )
    position, velocity, acceleration = tracker.update([0.0, 0.0, 0.0], 1.0e-200)
    assert np.isfinite(position).all()
    assert np.isfinite(velocity).all()
    np.testing.assert_array_equal(acceleration, [2.0, 0.0, 0.0])


@pytest.mark.parametrize(
    ("dt", "measurement", "expected_acceleration"),
    [(1.0e-200, 1.0e-300, 2.0e99), (1.0e200, 1.0e300, 2.0e-101)],
)
def test_gamma_correction_avoids_squaring_extreme_intervals(
    dt, measurement, expected_acceleration
) -> None:
    tracker = AlphaBetaGammaTracker(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        AlphaBetaGains(alpha=0.5, beta=0.25, gamma=0.1),
    )
    _position, _velocity, acceleration = tracker.update([measurement, 0.0, 0.0], dt)
    assert acceleration[0] == pytest.approx(expected_acceleration, rel=1.0e-12, abs=0.0)


@pytest.mark.parametrize("use_gamma", [False, True])
def test_overflowing_update_leaves_every_state_unchanged(use_gamma) -> None:
    gains = AlphaBetaGains(alpha=0.5, beta=0.25, gamma=0.1)

    def make_tracker():
        if use_gamma:
            return AlphaBetaGammaTracker([0.0] * 3, [0.0] * 3, [0.0] * 3, gains)
        return AlphaBetaTracker([0.0] * 3, [0.0] * 3, gains)

    tracker = make_tracker()
    peer = make_tracker()
    for estimator in (tracker, peer):
        estimator.update([1.0, 0.0, 0.0], 1.0)
    position = tracker.position_m.copy()
    velocity = tracker.velocity_mps.copy()
    if use_gamma:
        acceleration = tracker.acceleration_mps2.copy()
        # Position and velocity corrections fit; only acceleration overflows.
        measurement, dt = 1.0e100, 1.0e-150
    else:
        measurement, dt = 1.0e308, 1.0e-308
    with pytest.raises(ValueError):
        tracker.update([measurement, 0.0, 0.0], dt)
    np.testing.assert_array_equal(tracker.position_m, position)
    np.testing.assert_array_equal(tracker.velocity_mps, velocity)
    if use_gamma:
        np.testing.assert_array_equal(tracker.acceleration_mps2, acceleration)
    for actual, expected in zip(
        tracker.update([2.0, 0.0, 0.0], 1.0),
        peer.update([2.0, 0.0, 0.0], 1.0),
        strict=True,
    ):
        np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize("use_gamma", [False, True])
def test_unrepresentable_prediction_is_rejected_without_state_change(use_gamma) -> None:
    gains = AlphaBetaGains(alpha=0.5, beta=0.5)
    if use_gamma:
        tracker = AlphaBetaGammaTracker([1.0e308, 0.0, 0.0], [1.0e308, 0.0, 0.0], [0.0] * 3, gains)
    else:
        tracker = AlphaBetaTracker([1.0e308, 0.0, 0.0], [1.0e308, 0.0, 0.0], gains)
    with pytest.raises(ValueError):
        tracker.update([0.0] * 3, 2.0)
    np.testing.assert_array_equal(tracker.position_m, [1.0e308, 0.0, 0.0])
    np.testing.assert_array_equal(tracker.velocity_mps, [1.0e308, 0.0, 0.0])
