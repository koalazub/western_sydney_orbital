"""Pure input validation and idle lifecycle, without a substitute simulation.

These tests make no claims about a live worker. Browser playback, manual
interruption and command delivery require the owner's actual browser check.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from wsorb.experiment import Decision, ExperimentRunner, _number
from wsorb.local_api import LocalSimulation


def test_decision_copies_body_frame_vectors_without_clamping_or_sending():
    accel = np.array([0.0, 1.0, 2.0], dtype=np.float32)
    angular = [0.0, 0.0, 0.1]
    decision = Decision(accel, angular)
    accel[1] = 99
    angular[2] = 99
    np.testing.assert_array_equal(decision.accel, [0.0, 1.0, 2.0])
    np.testing.assert_array_equal(decision.angular_accel, [0.0, 0.0, 0.1])
    assert decision.accel.dtype == np.float64
    assert decision.angular_accel.dtype == np.float64
    assert not decision.accel.flags.writeable
    assert not decision.angular_accel.flags.writeable
    assert decision.preview is None
    with pytest.raises(FrozenInstanceError):
        decision.accel = np.zeros(3)


@pytest.mark.parametrize("name", ["accel", "angular_accel"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        [],
        [0, 0],
        [[0, 0, 0]],
        [[0], [0, 0], [0]],
        [np.nan, 0, 0],
        [0, np.inf, 0],
        [0, 0, -np.inf],
        [0, 0, 1j],
        [True, False, True],
        ["0", "0", "0"],
        [object(), 0, 0],
        [10**400, 0, 0],
    ],
)
def test_decision_rejects_non_finite_or_non_vector_commands(name, value):
    arguments = {"accel": [0, 0, 0], "angular_accel": [0, 0, 0], name: value}
    with pytest.raises(ValueError, match=f"{name} must be a finite real vector"):
        Decision(**arguments)


def test_preview_is_copied_without_inventing_a_render_schema():
    packet = {"waypoints": [[1.0, 2.0, 3.0]]}
    decision = Decision([0, 0, 0], [0, 0, 0], packet)
    packet["waypoints"][0][0] = 99
    assert decision.preview == {"waypoints": [[1.0, 2.0, 3.0]]}


@pytest.mark.parametrize("value", [[], "preview", 1, False])
def test_preview_must_be_an_optional_mapping(value):
    with pytest.raises(ValueError, match="preview must be a mapping or None"):
        Decision([0, 0, 0], [0, 0, 0], value)


def test_idle_status_and_stop_have_no_connection_or_measurements():
    runner = ExperimentRunner()
    expected = {
        "state": "idle",
        "sample_count": 0,
        "elapsed_sim_s": 0.0,
        "latest": None,
        "error": None,
    }
    assert runner.status() == expected
    assert runner.stop(timeout_s=0) == expected
    assert runner.stop() == expected
    report = runner.status()
    report.update(state="running", sample_count=10, latest={"not": "a measurement"})
    assert runner.status() == expected


@pytest.mark.parametrize("value", [True, np.bool_(False), "1", None, [], 1j, np.nan, np.inf])
def test_parameters_require_finite_real_numbers(value):
    with pytest.raises(ValueError, match="cadence_hz must be a finite real number"):
        _number(value, "cadence_hz")


@pytest.mark.parametrize("value", [0.1, 1, np.float64(10), np.int64(300)])
def test_parameter_numbers_accept_python_and_numpy_reals(value):
    assert _number(value, "parameter") == float(value)


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("duration_s", 0),
        ("duration_s", -1),
        ("duration_s", 300.01),
        ("duration_s", np.nan),
        ("duration_s", np.inf),
        ("duration_s", True),
        ("duration_s", "30"),
        ("cadence_hz", 0),
        ("cadence_hz", 0.099),
        ("cadence_hz", 10.01),
        ("cadence_hz", np.nan),
        ("cadence_hz", np.inf),
        ("cadence_hz", False),
        ("cadence_hz", "1"),
    ],
)
def test_start_rejects_invalid_limits_before_any_connection(parameter, value):
    # This is the actual client, constructed without connecting. No valid run is
    # started and no browser/network response is replaced or invented.
    sim = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    runner = ExperimentRunner()
    with pytest.raises(ValueError, match=parameter):
        runner.start(sim, lambda observation: Decision([0, 0, 0], [0, 0, 0]), **{parameter: value})
    assert runner.status()["state"] == "idle"
    assert runner.status()["latest"] is None


def test_start_requires_the_real_local_client_and_a_callback():
    runner = ExperimentRunner()
    with pytest.raises(TypeError, match="sim must be a LocalSimulation"):
        runner.start(object(), lambda observation: Decision([0, 0, 0], [0, 0, 0]))
    sim = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(TypeError, match="policy must be callable"):
        runner.start(sim, None)
    assert runner.status()["state"] == "idle"


@pytest.mark.parametrize("value", [-1, np.nan, np.inf, True, "10", None, threading.TIMEOUT_MAX * 2])
def test_stop_rejects_invalid_join_timeout_while_idle(value):
    runner = ExperimentRunner()
    with pytest.raises(ValueError, match="timeout_s"):
        runner.stop(value)
    assert runner.status()["state"] == "idle"
