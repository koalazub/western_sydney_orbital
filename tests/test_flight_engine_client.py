"""The script adapter against the real built WASM, without browser rendering."""

from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from wsorb.flight_engine import EngineError, HeadlessSimulation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine():
    if shutil.which("node") is None or not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").exists():
        pytest.skip("Run just build and use the project environment for real WASM tests")
    with HeadlessSimulation(ROOT) as simulation:
        yield simulation


def test_real_engine_repeats_seeded_steps_and_clears_commands(engine):
    settings = deepcopy(engine.scenario["experiment_settings"])
    settings["debris"]["count"] = 10
    initial = engine.configure_flight(settings, "far_side_l2", 42)
    first = engine.step([0, 0, 0.5], [0, 0, 0], 0.2)
    assert first["time_s"] == pytest.approx(0.2)
    assert first["shuttle"]["position"] != initial["shuttle"]["position"]
    assert first["shuttle"]["commanded_accel_mps2"] == [0, 0, 0.5]
    assert first.get("command_watchdog", {}).get("status") != "active"
    assert engine.observation()["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    engine.configure_flight(settings, "far_side_l2", 42)
    repeated = engine.step([0, 0, 0.5], [0, 0, 0], 0.2)
    assert repeated["shuttle"] == first["shuttle"]
    assert repeated["radar"] == first["radar"]
    assert len(engine.metadata["wasm_sha256"]) == 64
    assert engine.metadata["node_version"].startswith("v")
    assert engine.metadata["control_mode"] == "isolated_legacy_command_step_stop"


def test_rejected_configuration_does_not_replace_trial(engine):
    before = engine.observation()
    with pytest.raises(ValueError, match="seed"):
        engine.configure_flight(engine.scenario["experiment_settings"], "far_side_l2", -1)
    assert engine.observation() == before


def test_many_headless_steps_do_not_fill_native_action_queue(engine):
    settings = deepcopy(engine.scenario["experiment_settings"])
    settings["debris"]["count"] = 0
    engine.configure_flight(settings, "far_side_l2", 42)
    for index in range(40):
        observation = engine.step([0, 0, 0.5], [0, 0, 0], 0.05)
        assert observation["time_s"] == pytest.approx((index + 1) * 0.05)
    replacement = engine.configure_flight(settings, "far_side_l2", 43)
    assert replacement["time_s"] == 0
    assert replacement["seed"] == 43
    assert engine.step([0, 0, 0.5], [0, 0, 0], 0.05)["time_s"] == pytest.approx(0.05)


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), 31, True])
def test_invalid_steps_fail_before_transport(seconds):
    engine = HeadlessSimulation(ROOT)
    with pytest.raises(ValueError, match="seconds"):
        engine.step([0, 0, 0], [0, 0, 0], seconds)


def test_invalid_command_fails_before_transport():
    engine = HeadlessSimulation(ROOT)
    with pytest.raises(ValueError, match="finite real vector"):
        engine.step(np.array([0, np.nan, 0]), [0, 0, 0], 0.1)


def test_closed_context_rejects_requests(engine):
    engine.close()
    with pytest.raises(EngineError, match="with statement"):
        engine.observation()


def test_request_timeout_kills_uncertain_world_instead_of_retrying(engine):
    process = engine._process
    engine.timeout_s = 0
    with pytest.raises(EngineError, match="timed out"):
        engine.observation()
    assert process.poll() is not None
    with pytest.raises(EngineError, match="with statement"):
        engine.observation()


def test_unavailable_node_has_actionable_error():
    with (
        pytest.raises(EngineError, match="Cannot start Node"),
        HeadlessSimulation(ROOT, node="/nonexistent/wsorb-node"),
    ):
        pass


def test_missing_build_has_actionable_error(tmp_path):
    directory = tmp_path / "simulation/scenarios"
    directory.mkdir(parents=True)
    (directory / "phobos_escape.json").write_text("{}")
    with pytest.raises(EngineError, match="just build"), HeadlessSimulation(tmp_path):
        pass
