"""Python flight algorithms against the actual built WASM physics API.

Node loads the same engine binary as the browser without starting a renderer.
No DOM, browser, SDK, sensor result or physics response is substituted here.
These tests do not verify GPU rendering or the licensed STP runtime.
"""

from __future__ import annotations

import json
import selectors
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from wsorb.flight import PlannerConfig, basic_command, plan_route, predict_obstacles

ROOT = Path(__file__).resolve().parents[1]


class WasmPhysics:
    def __init__(self, process):
        self.process = process

    def query(self, method, **payload):
        self.process.stdin.write(json.dumps({"method": method, **payload}, allow_nan=False) + "\n")
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "The real WASM engine did not respond"
        line = self.process.stdout.readline()
        assert line, "The real WASM engine stopped"
        result = json.loads(line)
        assert "error" not in result, result.get("error")
        return result["value"]

    def command(self, **request):
        return self.query("command", command=request)


@pytest.fixture(scope="module")
def wasm():
    node = shutil.which("node")
    if node is None or not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Run just build to exercise Python against the real WASM engine")
    process = subprocess.Popen(
        [node, "worker/scripts/test-engine.mjs"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    engine = WasmPhysics(process)
    try:
        observation = engine.query("observation")
        if "geometry" not in observation:
            pytest.skip("Rebuild the engine to publish external-planner geometry")
        yield engine
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.fixture
def engine(wasm):
    wasm.command(op="reset", seed=100)
    return wasm


def test_python_plans_from_genuine_public_sensor_data_without_changing_the_world(engine):
    observation = engine.query("observation")
    before = json.dumps(observation, sort_keys=True)
    config = PlannerConfig()
    frame = predict_obstacles(observation, config)
    route = plan_route(frame, config)
    report = route.to_preview()
    assert report["status"] == "candidate", report["detail"]
    assert report["source_seed"] == observation["seed"]
    assert report["source_time_s"] == observation["time_s"]
    assert len(report["points_world_m"]) >= 2
    np.testing.assert_allclose(report["points_world_m"][0], observation["shuttle"]["position"])
    assert json.dumps(observation, sort_keys=True) == before
    assert json.dumps(engine.query("observation"), sort_keys=True) == before


def test_python_preview_publication_is_not_a_flight_command(engine):
    observation = engine.query("observation")
    config = PlannerConfig()
    report = plan_route(predict_obstacles(observation, config), config).to_preview()
    result = engine.query("preview", preview=report)
    assert result["ok"] is True
    displayed = engine.query("navigation")
    assert displayed["algorithm_owner"] == "python_notebook"
    assert displayed["plan"]["points_world_m"] == report["points_world_m"]
    assert engine.query("observation") == observation


def test_python_controller_commands_the_actual_reference_actuator_and_stop_preserves_momentum(
    engine,
):
    before = engine.query("observation")
    accel, angular = basic_command(before)
    assert np.asarray(accel).shape == np.asarray(angular).shape == (3,)
    assert np.isfinite(accel).all() and np.isfinite(angular).all()
    assert np.linalg.norm(accel) <= before["max_accel_mps2"] + 1e-12
    assert np.linalg.norm(angular) <= before["max_angular_accel_rad_s2"] + 1e-12
    engine.command(
        op="set_command",
        accel=np.asarray(accel).tolist(),
        angular_accel=np.asarray(angular).tolist(),
    )
    engine.command(op="step", seconds=0.05)
    after = engine.query("observation")
    assert after["time_s"] == pytest.approx(before["time_s"] + 0.05)
    assert after["shuttle"]["thrust_application"] == "center_of_mass"
    np.testing.assert_allclose(after["shuttle"]["commanded_accel_mps2"], accel, atol=1e-12)
    assert after["shuttle"]["velocity"] != before["shuttle"]["velocity"]
    engine.command(op="stop")
    stopped = engine.query("observation")
    assert stopped["shuttle"]["commanded_accel_mps2"] == [0.0, 0.0, 0.0]
    assert stopped["shuttle"]["velocity"] == after["shuttle"]["velocity"]


def test_actual_upright_reset_records_the_intervention_without_teleporting_or_braking(engine):
    initial = engine.query("observation")
    engine.command(op="set_command", accel=[0, 0, 0.5], angular_accel=[0, 0, 0])
    engine.command(op="step", seconds=2.0)
    engine.command(op="set_command", accel=[0, 0, 0], angular_accel=[0.05, 0, 0])
    engine.command(op="step", seconds=0.5)
    before = engine.query("observation")
    assert np.linalg.norm(before["shuttle"]["angular_velocity_rad_s"]) > 1e-5
    engine.command(op="debug_upright")
    after = engine.query("observation")
    assert after["manual_interventions"] == before["manual_interventions"] + 1
    assert after["time_s"] == before["time_s"]
    assert after["mission"]["fuel_used_kg"] == before["mission"]["fuel_used_kg"]
    assert after["mission"]["damage"] == before["mission"]["damage"]
    assert after["shuttle"]["position"] == before["shuttle"]["position"]
    assert after["shuttle"]["velocity"] == before["shuttle"]["velocity"]
    assert after["shuttle"]["angular_velocity_rad_s"] == [0, 0, 0]
    assert abs(
        np.dot(after["shuttle"]["rotation"], initial["shuttle"]["rotation"])
    ) == pytest.approx(1.0)


def test_actual_world_configuration_changes_mass_inertia_and_worldwide_sensor_population(engine):
    before = engine.query("observation")
    settings = deepcopy(before["experiment_settings"])
    settings["ship"]["mass_kg"] *= 1.25
    settings["ship"]["inertia_scale"] = 1.5
    settings["debris"]["count"] = 64
    engine.command(op="configure_experiment", settings=settings, seed=100)
    after = engine.query("observation")
    assert after["experiment_settings"] == settings
    assert after["shuttle"]["mass_kg"] == settings["ship"]["mass_kg"]
    np.testing.assert_allclose(
        after["shuttle"]["inertia_kg_m2"], np.asarray(before["shuttle"]["inertia_kg_m2"]) * 1.875
    )
    assert after["geometry"]["debris_count"] == len(after["radar"]) == 64
    assert after["max_accel_mps2"] == before["max_accel_mps2"] == 0.5
    assert after["max_angular_accel_rad_s2"] == 0.05
    positions = np.asarray([row[0] for row in after["radar"]]) + np.asarray(
        after["shuttle"]["position"]
    )
    assert np.all(positions.min(axis=0) < 0) and np.all(positions.max(axis=0) > 0)


def test_actual_camera_aims_are_independent_clipped_and_do_not_change_ship_physics(engine):
    before = engine.query("observation")
    assert engine.query("pan_tilt", camera="top", yaw=1000, pitch=-1000)["ok"] is True
    assert engine.query("pan_tilt", camera="bottom", yaw=-1000, pitch=1000)["ok"] is True
    cameras = {camera["camera"]: camera for camera in engine.query("cameras")["cameras"]}
    assert (cameras["top"]["yaw_deg"], cameras["top"]["pitch_deg"]) == (89, -50)
    assert (cameras["bottom"]["yaw_deg"], cameras["bottom"]["pitch_deg"]) == (-89, 50)
    for camera in cameras.values():
        assert camera["field_of_view_locked"] is True
        assert camera["horizontal_field_of_view_deg"] == camera["vertical_field_of_view_deg"] == 60
        assert (camera["width"], camera["height"]) == (512, 512)
        assert camera["debug_overlays"] is False
    assert engine.query("observation") == before


def test_actual_key_configuration_cannot_replace_an_external_flight_command(engine):
    engine.command(op="set_command", accel=[0.1, 0, 0], angular_accel=[0, 0, 0])
    before = engine.query("observation")
    bindings = engine.query("bindings")
    settings = {"version": 1, "bindings": dict(bindings["bindings"])}
    settings["bindings"]["front"] = "KeyJ"
    assert engine.query("set_bindings", settings=settings)["ok"] is True
    assert engine.query("bindings")["bindings"]["front"] == "KeyJ"
    assert engine.query("observation") == before
