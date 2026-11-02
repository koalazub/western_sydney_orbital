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


def test_zero_gamma_reduces_to_the_alpha_beta_update() -> None:
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
