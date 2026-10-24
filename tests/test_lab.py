"""Execute the real notebook's safe default graph, without a browser backend."""

from __future__ import annotations

import runpy
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("marimo")

LAB_PATH = Path(__file__).resolve().parents[1] / "notebooks" / "flight_lab.py"


@pytest.fixture
def lab_defs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WSORB_LOCAL_CONNECTION", raising=False)
    namespace = runpy.run_path(str(LAB_PATH), run_name="wsorb_flight_lab_test")
    _outputs, definitions = namespace["app"].run()
    return definitions


def test_lab_default_graph_stays_disarmed_and_idle(lab_defs) -> None:
    required = {"planner_settings", "predict_scene", "find_path", "decide_command", "experiment"}
    assert required.issubset(lab_defs)
    assert all(
        callable(lab_defs[name]) for name in ("predict_scene", "find_path", "decide_command")
    )
    assert lab_defs["controller_mode"].value == "coast"
    assert lab_defs["policy_armed"].value is False
    for name in (
        "apply_and_run",
        "run_policy_step",
        "plan_and_show",
        "stop_run",
        "stop_forces",
        "upright",
        "reset_run",
    ):
        assert lab_defs[name].value is False
    assert lab_defs["reset_confirmed"].value is False
    status = lab_defs["experiment"].status()
    assert (status["state"], status["sample_count"], status["elapsed_sim_s"]) == ("idle", 0, 0.0)
    assert status["latest"] is None
    assert status["error"] is None


def test_lab_coast_policy_returns_zero_three_vectors(lab_defs) -> None:
    accel, angular_accel = lab_defs["decide_command"](None, None, "coast", {})
    assert isinstance(accel, np.ndarray)
    assert isinstance(angular_accel, np.ndarray)
    assert accel.shape == angular_accel.shape == (3,)
    np.testing.assert_array_equal(accel, np.zeros(3))
    np.testing.assert_array_equal(angular_accel, np.zeros(3))
