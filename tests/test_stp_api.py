"""Pure adapter input checks. No SDK substitute or simulator connection."""

from importlib.util import find_spec

import numpy as np
import pytest

from wsorb.stp_api import _finite_real_vector, connect_from_argv


def test_command_vector_validation_returns_an_independent_real_three_vector():
    original = np.array([0.0, 0.0, 0.5])
    result = _finite_real_vector(original, 3, "accelCommand")
    np.testing.assert_array_equal(result, original)
    assert result.dtype == np.float64
    result[0] = 0.25
    np.testing.assert_array_equal(original, [0.0, 0.0, 0.5])


@pytest.mark.parametrize(
    "value",
    [
        [0.0, 0.0],
        [[0.0, 0.0, 0.5]],
        [0.0, np.nan, 0.0],
        [0.0, np.inf, 0.0],
        [0.0, 0.0, 1j],
        [True, False, True],
        ["0", "0", "0.5"],
    ],
)
def test_invalid_command_vectors_are_rejected_without_an_sdk(value):
    with pytest.raises(ValueError, match="finite real vector of shape"):
        _finite_real_vector(value, 3, "accelCommand")


def test_missing_official_sdk_fails_clearly_instead_of_using_a_local_substitute():
    if find_spec("spaceteams") is not None:
        pytest.skip("A licensed SDK is installed; connecting is not part of this test")
    with pytest.raises(RuntimeError, match="real 'spaceteams' SDK could not be loaded"):
        connect_from_argv([])
