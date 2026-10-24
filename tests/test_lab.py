"""Execute the real notebook's safe default graph, without a browser backend."""

from __future__ import annotations

import runpy
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("marimo")
pytest.importorskip("altair")

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
        "apply_world_settings",
        "apply_camera_aim",
        "capture_images",
        "prepare_export",
    ):
        assert lab_defs[name].value is False
    assert lab_defs["reset_confirmed"].value is False
    assert lab_defs["world_reset_confirmed"].value is False
    assert lab_defs["world_settings_form"].value is None
    assert lab_defs["camera_aim_form"].value is None
    assert lab_defs["get_applied_config"]() is None
    assert lab_defs["get_camera_report"]() is None
    assert lab_defs["experiment"].history() == []
    assert lab_defs["flight_history_chart"] is None
    assert lab_defs["lab_source_path"] == LAB_PATH
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


def test_lab_uses_real_native_sliders_with_linked_numeric_inputs(lab_defs) -> None:
    import marimo as mo

    slider = lab_defs["lab_slider"](0, 10, 0.5, 2.5, "Numeric UI contract")
    assert isinstance(slider, mo.ui.slider)
    assert (slider.start, slider.stop, slider.step, slider.value) == (0, 10, 0.5, 2.5)
    assert "include-input" in slider.text
    assert "show-value" in slider.text
    assert isinstance(lab_defs["duration_s"], mo.ui.slider)
    assert lab_defs["world_settings_draft"] == lab_defs["default_world_settings"]


def test_real_altair_history_plot_splits_missing_values_and_observed_segments(lab_defs) -> None:
    """Numeric plotting contract only, not flight measurements or a backend mock."""
    import marimo as mo

    rows = [
        {"segment": 1, "telemetry_time_s": 1, "speed_mps": 2.0},
        {"segment": 1, "telemetry_time_s": 2, "speed_mps": None},
        {"segment": 1, "telemetry_time_s": 3, "speed_mps": 4.0},
        {"segment": 2, "telemetry_time_s": 0, "speed_mps": 1.0},
    ]
    chart = lab_defs["history_plot"](rows, "speed_mps", "Numeric contract", "telemetry_time_s")
    spec = chart.to_dict()
    points = spec["data"].get("values")
    if points is None:
        points = spec["datasets"][spec["data"]["name"]]
    assert [point["speed_mps"] for point in points] == [2.0, 4.0, 1.0]
    assert len({point["plot_segment"] for point in points}) == 3
    assert spec["encoding"]["x"]["field"] == "telemetry_time_s"
    assert spec["encoding"]["detail"]["field"] == "plot_segment"
    assert spec["params"]
    widget = mo.ui.altair_chart(chart, chart_selection=False, legend_selection=False)
    assert isinstance(widget, mo.ui.altair_chart)
