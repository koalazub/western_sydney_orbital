"""Pure PID reference checks; no gains here are flight recommendations."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight.pid import PIDConfig, PIDVelocityController


def test_proportional_only_velocity_error_and_native_bound() -> None:
    controller = PIDVelocityController(PIDConfig(kp=0.2))
    command = controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1)
    np.testing.assert_allclose(command, [0.2, 0.0, 0.0])
    saturated = PIDVelocityController(PIDConfig(kp=100.0)).update(
        [1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1
    )
    assert np.linalg.norm(saturated) <= 0.5


def test_integral_is_clamped_and_can_be_unwound() -> None:
    controller = PIDVelocityController(PIDConfig(kp=0.0, ki=1.0, integral_limit=0.1))
    first = controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0)
    second = controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0)
    assert first[0] == pytest.approx(0.1)
    assert second[0] == pytest.approx(0.1)
    unwound = controller.update([-0.1, 0.0, 0.0], [0.0, 0.0, 0.0], 1.0)
    assert unwound[0] == pytest.approx(0.0)


def test_derivative_is_filtered_and_opposes_a_falling_error() -> None:
    controller = PIDVelocityController(PIDConfig(kp=0.0, ki=0.0, kd=1.0, derivative_tau_s=0.05))
    first = controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1)
    assert first[0] == pytest.approx(0.0)
    second = controller.update([0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1)
    assert second[0] < 0.0
    assert np.linalg.norm(second) <= 0.5


def test_reset_clears_state_and_rejects_invalid_intervals_and_limits() -> None:
    controller = PIDVelocityController(PIDConfig(kp=0.1))
    controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1)
    controller.reset()
    fresh = controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.1)
    assert fresh[0] == pytest.approx(0.1)
    with pytest.raises(ValueError):
        controller.update([1.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0)
    with pytest.raises(ValueError):
        PIDConfig(kp=1.0, accel_limit_mps2=0.500001)
