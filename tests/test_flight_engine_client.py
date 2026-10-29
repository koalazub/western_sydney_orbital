"""The compatibility facade against the real owned native API and built WASM."""

from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from wsorb import checkpoint, flight_engine
from wsorb.flight_engine import EngineError, HeadlessSimulation
from wsorb.local_api import LocalApiError, LocalSimulation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def no_launch(monkeypatch):
    def unexpected_launch():
        pytest.fail("Validation must not start an isolated server")

    monkeypatch.setattr(flight_engine, "isolated_simulation", unexpected_launch)


@pytest.fixture
def owned_context(monkeypatch):
    if (
        shutil.which("node") is None
        or not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").is_file()
    ):
        pytest.skip("Run just build and use the project environment for real WASM tests")
    ownership = {"launches": 0, "closed": False}

    @contextmanager
    def tracked_context():
        ownership["launches"] += 1
        try:
            with checkpoint.isolated_simulation() as client:
                ownership["client"] = client
                yield client
        finally:
            ownership["closed"] = True

    monkeypatch.setattr(flight_engine, "isolated_simulation", tracked_context)
    return ownership


@pytest.fixture
def engine(owned_context):
    with HeadlessSimulation(ROOT) as simulation:
        yield simulation


def test_real_engine_repeats_seeded_steps_and_clears_commands(engine):
    settings = deepcopy(engine.scenario["experiment_settings"])
    settings["debris"]["count"] = 10
    initial = engine.configure_flight(settings, "far_side_l2", 42)
    first = engine.step([0, 0, 0.5], [0, 0, 0], 0.2)
    assert initial["time_s"] == 0
    assert initial["seed"] == 42
    assert initial["experiment_settings"] == settings
    assert initial["applied_physics"]["mass_kg"] == pytest.approx(settings["ship"]["mass_kg"])
    assert first["time_s"] == pytest.approx(0.2)
    assert first["shuttle"]["position"] != initial["shuttle"]["position"]
    assert first["shuttle"]["commanded_accel_mps2"] == [0, 0, 0.5]
    assert first.get("command_watchdog", {}).get("status") != "active"
    assert engine.observation()["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    engine.configure_flight(settings, "far_side_l2", 42)
    repeated = engine.step([0, 0, 0.5], [0, 0, 0], 0.2)
    assert repeated["shuttle"] == first["shuttle"]
    assert repeated["radar"] == first["radar"]


def test_native_api_and_provenance_are_the_executed_backend(engine, owned_context):
    client = owned_context["client"]
    assert isinstance(client, LocalSimulation)
    assert client.timeout_s == 30.0
    status = client.status()
    assert status["backend"] == "native_headless"
    assert status["physics_ready"] is True
    assert status["physics_status_cached"] is False
    snapshot = client.snapshot()
    assert snapshot["runtime"]["running"] is False
    assert engine.observation() == snapshot["observation"]
    metadata = engine.metadata
    manifest = json.loads((ROOT / "worker/public/sim/manifest.json").read_text())
    assert metadata["engine_build"] == snapshot["engine_build"] == manifest
    assert metadata["wasm_sha256"] == manifest["sha256"]
    for name, path in {
        "wasm_sha256": "worker/public/sim/wsorb_bevy_bg.wasm",
        "manifest_sha256": "worker/public/sim/manifest.json",
        "driver_sha256": "worker/scripts/headless.mjs",
        "local_api_adapter_sha256": "src/wsorb/local_api.py",
    }.items():
        assert metadata[name] == hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    assert metadata["driver_source"] == "worker/scripts/headless.mjs"
    assert metadata["control_mode"] == "owned_native_API_manual_step"
    assert metadata["execution_kind"] == "owned_native_manual_step"
    assert metadata["maximum_step_s"] == 1.0
    assert "node_version" not in metadata
    assert metadata["scenario_source"] == engine.scenario
    metadata["scenario_source"]["integration"]["fixed_step_s"] = 99
    scenario = engine.scenario
    scenario["experiment_settings"]["debris"]["count"] = 99
    assert engine.scenario["experiment_settings"]["debris"]["count"] == 500
    assert engine.metadata["scenario_source"]["integration"]["fixed_step_s"] == 0.05
    with pytest.raises(EngineError, match="already open"):
        engine.__enter__()
    assert owned_context["launches"] == 1


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
    assert engine.step([0, 0, 0.5], [0, 0, 0], 1.0)["time_s"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "seconds", [0, -1, float("nan"), float("inf"), 1.05, 30, 31, True, None, "0.1"]
)
def test_invalid_steps_fail_before_transport(seconds, no_launch):
    engine = HeadlessSimulation(ROOT)
    with pytest.raises(ValueError, match="seconds"):
        engine.step([0, 0, 0], [0, 0, 0], seconds)


def test_invalid_command_fails_before_transport(no_launch):
    engine = HeadlessSimulation(ROOT)
    with pytest.raises(ValueError, match="finite real vector"):
        engine.step(np.array([0, np.nan, 0]), [0, 0, 0], 0.1)


@pytest.mark.parametrize("field", ["settings", "preset", "seed"])
def test_invalid_configuration_fails_without_opening_context(field, no_launch):
    engine = HeadlessSimulation(ROOT)
    arguments = {
        "settings": engine.scenario["experiment_settings"],
        "preset": "far_side_l2",
        "seed": 42,
    }
    arguments[field] = {"settings": {}, "preset": "typo", "seed": -1}[field]
    with pytest.raises(ValueError):
        engine.configure_flight(**arguments)


@pytest.mark.parametrize("timeout", [0, -1, 30.01, float("nan"), float("inf"), True, None, "1"])
def test_invalid_timeout_rejected_before_launch(timeout, no_launch):
    with pytest.raises(ValueError, match="timeout_s"):
        HeadlessSimulation(timeout_s=timeout)


def test_unsupported_overrides_rejected_before_launch(tmp_path, no_launch):
    with pytest.raises(ValueError, match="root overrides are unsupported"):
        HeadlessSimulation(tmp_path)
    with pytest.raises(ValueError, match="node overrides are unsupported"):
        HeadlessSimulation(node="/nonexistent/wsorb-node")
    assert HeadlessSimulation(ROOT / "src/..", timeout_s=0.5).root == ROOT


def test_close_releases_owned_context_and_is_idempotent(engine, owned_context):
    engine.close()
    engine.close()
    assert owned_context["closed"] is True
    with pytest.raises(EngineError, match="with statement"):
        engine.observation()
    with pytest.raises(LocalApiError, match="transport unavailable"):
        owned_context["client"].status()


def test_body_exception_releases_owned_context(owned_context):
    with pytest.raises(RuntimeError, match="caller stopped"), HeadlessSimulation(timeout_s=0.5):
        assert owned_context["client"].timeout_s == 0.5
        raise RuntimeError("caller stopped")
    assert owned_context["closed"] is True
    with pytest.raises(LocalApiError, match="transport unavailable"):
        owned_context["client"].status()


def test_native_transport_failure_closes_owned_world_without_retry(engine, owned_context):
    client = owned_context["client"]
    # A real HTTP request with an unusably short socket deadline, not a fake engine.
    client.timeout_s = 1e-300
    with pytest.raises(EngineError, match="closed without retry"):
        engine.step([0, 0, 0], [0, 0, 0], 0.05)
    assert owned_context["closed"] is True
    assert owned_context["launches"] == 1
    with pytest.raises(EngineError, match="with statement"):
        engine.observation()
    client.timeout_s = 2.0
    with pytest.raises(LocalApiError, match="transport unavailable"):
        client.status()
