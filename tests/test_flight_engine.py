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

    def query(self, method, *, wait_s=15, **payload):
        self.process.stdin.write(json.dumps({"method": method, **payload}, allow_nan=False) + "\n")
        self.process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=wait_s), "The real WASM engine did not respond"
        line = self.process.stdout.readline()
        assert line, "The real WASM engine stopped"
        result = json.loads(line)
        assert "error" not in result, result.get("error")
        return result["value"]

    def command(self, *, wait_s=15, **request):
        return self.query("command", command=request, wait_s=wait_s)


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
        engine.baseline_settings = deepcopy(observation["experiment_settings"])
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
    wasm.command(
        op="configure_flight",
        settings=deepcopy(wasm.baseline_settings),
        seed=100,
        preset="far_side_l2",
    )
    return wasm


def test_renderer_rejects_the_actual_no_dom_runtime_without_panicking_or_starting_physics(engine):
    # Node really has no browser document or GPU surface. None is installed or
    # replaced here. This is a negative-path test, not proof of GPU rendering.
    before = engine.query("debug_snapshot")
    runtime = engine.query("runtime")
    for _ in range(2):
        result = engine.query("renderer_start_result", selector="#simulation-canvas")
        assert result["ok"] is False
        assert result["code"] == "document_unavailable"
        assert engine.query("renderer_status")["started"] is False
        assert engine.query("renderer_status")["ready"] is False
        assert engine.query("debug_snapshot") == before
        assert engine.query("runtime") == runtime
    # A rejected renderer start must not poison the real WASM instance.
    engine.command(op="step", seconds=0.05)
    assert engine.query("observation")["time_s"] == pytest.approx(before["time_s"] + 0.05)


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
    assert after["shuttle"]["mass_kg"] == pytest.approx(settings["ship"]["mass_kg"])
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


def test_live_ship_tuning_updates_real_hardware_without_resetting_any_flight_state(engine):
    engine.command(op="set_command", accel=[0.12, 0.03, 0], angular_accel=[0.01, 0, 0])
    engine.command(op="step", seconds=0.1)
    before = engine.query("debug_snapshot")
    observation = engine.query("observation")
    tuning = {
        "mass_kg": observation["shuttle"]["mass_kg"] * 1.1,
        "inertia_scale": 1.4,
        "thrust_scale": 1.75,
    }
    engine.command(op="tune_ship", settings=tuning)
    after = engine.query("debug_snapshot")
    observed = engine.query("observation")
    assert observed["ship_tuning"] == tuning
    assert observed["tuning_revision"] == observation["tuning_revision"] + 1
    for field in (
        "time_s",
        "seed",
        "rng_state",
        "active_contacts",
        "collisions",
        "manual_interventions",
        "mission",
        "observations",
    ):
        assert after[field] == before[field], field
    for previous, current in zip(before["assets"], after["assets"], strict=True):
        for field in (
            "id",
            "position",
            "rotation",
            "velocity",
            "angular_velocity_rad_s",
            "fuel_kg",
        ):
            assert current[field] == previous[field], field
    ship = observed["shuttle"]
    assert ship["mass_kg"] == pytest.approx(tuning["mass_kg"])
    assert ship["commanded_accel_mps2"] == [0.12, 0.03, 0]
    np.testing.assert_allclose(
        ship["thrust_n"], np.array([0.12, 0.03, 0]) * tuning["mass_kg"] * tuning["thrust_scale"]
    )
    assert observed["max_accel_mps2"] == 0.5
    assert observed["max_angular_accel_rad_s2"] == 0.05
    physical = observed["applied_physics"]
    assert physical["mass_kg"] == pytest.approx(tuning["mass_kg"])
    assert physical["source_time_s"] == before["time_s"]
    assert physical["tuning_revision"] == observed["tuning_revision"]
    assert physical["actuation_state"] == "actuating"
    application = physical["last_actuator_application"]
    assert application["application_time_s"] == before["time_s"]
    assert application["tuning_revision"] == physical["tuning_revision"]
    np.testing.assert_allclose(application["force_body_n"], ship["thrust_n"], atol=1e-9)
    np.testing.assert_allclose(physical["inertia_body_kg_m2"], ship["inertia_kg_m2"])
    assert physical["external_force_provenance"] == "rapier_user_forces_excluding_solver_impulses"
    engine.command(op="step", seconds=0.05)
    advanced = engine.query("observation")
    assert advanced["applied_physics"]["actuation_state"] == "actuating"
    assert advanced["shuttle"]["velocity"] != ship["velocity"]
    np.testing.assert_allclose(
        advanced["applied_physics"]["last_actuator_application"]["force_body_n"],
        application["force_body_n"],
        atol=1e-8,
    )


def test_live_gain_edit_with_no_request_does_not_start_flight_or_invent_force(engine):
    engine.command(op="stop")
    before = engine.query("observation")
    runtime = engine.query("runtime")
    tuning = {**before["ship_tuning"], "thrust_scale": 3.2}
    if tuning == before["ship_tuning"]:
        tuning["thrust_scale"] = 2.7
    engine.command(op="tune_ship", settings=tuning)
    after = engine.query("observation")
    physical = after["applied_physics"]
    assert after["time_s"] == before["time_s"]
    assert after["mission"] == before["mission"]
    assert after["shuttle"]["position"] == before["shuttle"]["position"]
    assert after["shuttle"]["velocity"] == before["shuttle"]["velocity"]
    assert after["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    assert physical["actuation_state"] == "coasting"
    assert physical["last_actuator_application"]["force_world_n"] == [0, 0, 0]
    assert physical["last_actuator_application"]["torque_world_nm"] == [0, 0, 0]
    assert engine.query("runtime") == runtime


def test_live_flow_changes_existing_physical_velocities_without_regenerating_the_field(engine):
    before = engine.query("debug_snapshot")
    observation = engine.query("observation")
    original = observation["debris_flow"]
    speed_delta = 7 if original["speed_mps"] <= 193 else -7
    flow = {**original, "speed_mps": original["speed_mps"] + speed_delta}
    engine.command(op="tune_debris_flow", settings=flow)
    after = engine.query("debug_snapshot")
    assert engine.query("observation")["debris_flow"] == flow
    for field in (
        "time_s",
        "seed",
        "rng_state",
        "active_contacts",
        "collisions",
        "manual_interventions",
        "mission",
        "observations",
    ):
        assert after[field] == before[field], field
    delta = np.zeros(3)
    delta["xyz".index(flow["stream_axis"])] = speed_delta * flow["stream_sign"]
    for previous, current in zip(before["assets"], after["assets"], strict=True):
        for field in (
            "id",
            "position",
            "rotation",
            "angular_velocity_rad_s",
            "mass_kg",
            "radius_m",
        ):
            assert current[field] == previous[field], field
        expected = np.asarray(previous["velocity"]) + (
            delta if current["kind"] == "asteroid" else 0
        )
        np.testing.assert_allclose(current["velocity"], expected, rtol=0, atol=1e-12)


def test_real_python_astar_preview_and_physical_lookahead_are_connected_without_actuation(engine):
    before = engine.query("debug_snapshot")
    observation = engine.query("observation")
    settings = PlannerConfig()
    route = plan_route(predict_obstacles(observation, settings), settings)
    preview = route.to_preview()
    target = np.asarray(observation["shuttle"]["position"]) + np.array([0, 0, 100])
    if len(route.points_world_m) > 1:
        target = route.points_world_m[1]
    preview["execution"] = {
        "state": "watching",
        "controller": "planned_waypoint_pd",
        "source_time_s": observation["time_s"],
        "source_seed": observation["seed"],
        "next_waypoint_world_m": np.asarray(target).tolist(),
        "tuning_revision": observation["tuning_revision"],
    }
    assert engine.query("preview", preview=preview)["ok"] is True
    ray = engine.query(
        "raycast", query={"target_world_m": np.asarray(target).tolist(), "max_distance_m": 250}
    )
    assert ray["ok"] is True
    report = ray["report"]
    assert report["origin_world_m"] == observation["shuttle"]["position"]
    assert report["source_time_s"] == observation["time_s"]
    navigation = engine.query("navigation")
    assert navigation["plan"]["points_world_m"] == preview["points_world_m"]
    assert navigation["plan"]["execution"]["state"] == "watching"
    assert navigation["raycast"] == report
    assert engine.query("preview", preview=preview)["ok"] is True
    assert engine.query("navigation")["raycast"] == report
    assert engine.query("debug_snapshot") == before


def test_lookahead_hits_real_terrain_or_pad_and_excludes_the_lander(engine):
    before = engine.query("debug_snapshot")
    result = engine.query("raycast", query={"target_world_m": [0, 0, 0], "max_distance_m": 250})
    assert result["ok"] is True
    hit = result["report"]["hit"]
    assert hit is not None
    assert hit["entity"] in {"Phobos", "LandingPad"}
    assert 0 <= hit["distance_m"] <= 250
    assert engine.query("debug_snapshot") == before


def _context(observation, runtime):
    return {
        "trial_revision": observation["trial_revision"],
        "tuning_revision": observation["tuning_revision"],
        "manual_interventions": observation["manual_interventions"],
        "seed": observation["seed"],
        "running": runtime["running"],
    }


def test_real_native_style_tuning_invalidates_atomic_python_commands_without_mutation(engine):
    observation = engine.query("observation")
    context = _context(observation, engine.query("runtime"))
    tuning = {**observation["ship_tuning"], "thrust_scale": 2.0}
    engine.command(op="tune_ship", settings=tuning)
    before = engine.query("debug_snapshot")
    rejected = engine.query(
        "control_result",
        command={
            "op": "set_command",
            "accel": [0.1, 0, 0],
            "angular_accel": [0, 0, 0],
            "expected_context": context,
        },
    )
    assert rejected["ok"] is False
    assert rejected["code"] == "stale_context"
    assert engine.query("debug_snapshot") == before
    current = engine.query("observation")
    applied = engine.query(
        "control_result",
        command={
            "op": "set_command",
            "accel": [0.1, 0, 0],
            "angular_accel": [0, 0, 0],
            "expected_context": _context(current, engine.query("runtime")),
        },
    )
    assert applied == {"ok": True}
    assert engine.query("observation")["shuttle"]["commanded_accel_mps2"] == [0.1, 0, 0]


def test_checked_play_rejects_old_context_and_only_acknowledges_queueing_for_current_context(
    engine,
):
    observation = engine.query("observation")
    old = _context(observation, engine.query("runtime"))
    engine.command(op="tune_ship", settings={**observation["ship_tuning"], "thrust_scale": 2.0})
    before = engine.query("debug_snapshot")
    rejection = engine.query(
        "control_result", command={"op": "play", "rate": 1, "expected_context": old}
    )
    assert rejection["ok"] is False
    assert rejection["code"] == "stale_context"
    assert engine.query("debug_snapshot") == before
    assert engine.query("runtime")["running"] is False
    current = _context(engine.query("observation"), engine.query("runtime"))
    ack = engine.query(
        "control_result", command={"op": "play", "rate": 1, "expected_context": current}
    )
    assert ack == {"ok": True}
    # This Node driver does not run Bevy frames. It cannot prove applied playback.
    assert engine.query("runtime")["running"] is False
    assert engine.query("debug_snapshot") == before


def test_same_seed_same_time_preview_rejects_old_trial_without_erasing_current_guidance(engine):
    observation = engine.query("observation")
    settings = PlannerConfig()
    old = plan_route(predict_obstacles(observation, settings), settings).to_preview()
    old["execution"] = {
        "state": "watching",
        "controller": "planned_waypoint_pd",
        "source_time_s": observation["time_s"],
        "source_seed": observation["seed"],
        "next_waypoint_world_m": None,
        "tuning_revision": observation["tuning_revision"],
        "trial_revision": observation["trial_revision"],
    }
    assert engine.query("preview", preview=old)["ok"] is True
    engine.command(
        op="configure_flight",
        settings=deepcopy(engine.baseline_settings),
        seed=observation["seed"],
        preset="far_side_l2",
    )
    current = engine.query("observation")
    assert current["seed"] == observation["seed"]
    assert current["time_s"] == observation["time_s"]
    fresh = deepcopy(old)
    fresh["execution"]["trial_revision"] = current["trial_revision"]
    assert engine.query("preview", preview=fresh)["ok"] is True
    navigation = engine.query("navigation")
    rejection = engine.query("preview", preview=old)
    assert rejection["ok"] is False
    assert rejection["code"] == "stale_context"
    assert engine.query("navigation") == navigation
    assert engine.query("observation") == current


@pytest.mark.parametrize(
    ("preset", "region", "sign"),
    [("mars_facing_l1", "L1", 1), ("far_side_l2", "L2", -1)],
)
def test_real_launch_presets_use_the_correct_physical_side_and_outward_lift(
    engine, preset, region, sign
):
    settings = deepcopy(engine.baseline_settings)
    settings["debris"]["count"] = 0
    before = engine.query("observation")
    engine.command(op="configure_flight", settings=settings, seed=100, preset=preset)
    observation = engine.query("observation")
    assert observation["flight_preset"] == preset
    assert observation["mission"]["target_region"] == region
    assert observation["trial_revision"] > before["trial_revision"]
    assert observation["shuttle"]["position"][0] * sign > 0
    np.testing.assert_allclose(
        observation["target"]["position"],
        observation["environment"][f"{region.lower()}_position_m"],
    )
    x, y, z, w = observation["applied_physics"]["rotation_body_to_world_xyzw"]
    up = np.array([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)])
    assert up[0] * sign > 0.95
    assert engine.query("runtime")["running"] is False
    engine.command(op="set_command", accel=[0, 0, 0.5], angular_accel=[0, 0, 0])
    engine.command(op="step", seconds=0.1)
    after = engine.query("observation")
    assert after["shuttle"]["velocity"][0] * sign > 0


def test_real_xyz_placement_is_paused_recorded_and_does_not_retime_or_erase_results(engine):
    engine.command(op="set_command", accel=[0, 0, 0.3], angular_accel=[0.01, 0, 0])
    engine.command(op="step", seconds=0.2)
    before = engine.query("debug_snapshot")
    observation = engine.query("observation")
    position = [observation["geometry"]["phobos_enclosing_radius_m"] + 1500, 100, 200]
    engine.command(op="place_lander", position_m=position)
    after = engine.query("debug_snapshot")
    placed = engine.query("observation")
    actual = placed["applied_physics"]
    np.testing.assert_array_equal(actual["position_world_m"], position)
    assert actual["velocity_world_mps"] == actual["angular_velocity_world_rad_s"] == [0, 0, 0]
    assert (
        actual["total_external_force_world_n"]
        == actual["total_external_torque_world_nm"]
        == [0, 0, 0]
    )
    assert placed["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    assert placed["manual_interventions"] == observation["manual_interventions"] + 1
    assert placed["trial_revision"] == observation["trial_revision"] + 1
    assert placed["radar"] == []
    for field in ("time_s", "seed", "rng_state", "ship_tuning_override", "debris_flow_override"):
        assert after[field] == before[field], field
    for field in ("status", "fuel_used_kg", "damage", "score"):
        assert after["mission"][field] == before["mission"][field], field
    assert engine.query("runtime")["running"] is False
    rejected = engine.query(
        "control_result", command={"op": "place_lander", "position_m": [0, 0, 0]}
    )
    assert rejected["ok"] is False
    assert engine.query("debug_snapshot") == after


def test_surface_launchpad_move_updates_both_real_bodies_without_restarting_the_world(engine):
    before = engine.query("debug_snapshot")
    observation = engine.query("observation")
    direction = np.array([1.0, 0.3, 0.2])
    result = engine.query(
        "control_result",
        command={
            "op": "move_launchpad",
            "surface_direction": direction.tolist(),
            "expected_context": _context(observation, engine.query("runtime")),
        },
    )
    assert result == {"ok": True}
    after = engine.query("debug_snapshot")
    moved = engine.query("observation")
    np.testing.assert_allclose(
        moved["launch_site_direction"], direction / np.linalg.norm(direction)
    )
    physics = moved["applied_physics"]
    pad = next(actor for actor in after["assets"] if actor["id"] == "LandingPad")
    np.testing.assert_array_equal(moved["launchpad"]["position"], pad["position"])
    np.testing.assert_allclose(pad["rotation"], physics["rotation_body_to_world_xyzw"], atol=1e-12)
    assert physics["velocity_world_mps"] == physics["angular_velocity_world_rad_s"] == [0, 0, 0]
    assert physics["total_external_force_world_n"] == [0, 0, 0]
    assert moved["manual_interventions"] == observation["manual_interventions"] + 1
    assert moved["trial_revision"] == observation["trial_revision"] + 1
    assert engine.query("runtime")["running"] is False
    for key in ("time_s", "seed", "rng_state", "ship_tuning_override", "debris_flow_override"):
        assert after[key] == before[key], key
    for key in ("status", "score", "fuel_used_kg", "damage", "initial_distance_m", "target_region"):
        assert after["mission"][key] == before["mission"][key], key
    original = {actor["id"]: actor for actor in before["assets"]}
    for actor in after["assets"]:
        if actor["id"] not in ("Shuttle", "LandingPad"):
            assert actor == original[actor["id"]]


def test_launchpad_context_and_invalid_surface_rejections_do_not_change_real_flight(engine):
    before = engine.query("observation")
    context = _context(before, engine.query("runtime"))
    engine.command(op="tune_ship", settings={**before["ship_tuning"], "inertia_scale": 2})
    actual = engine.query("debug_snapshot")
    result = engine.query(
        "control_result",
        command={
            "op": "move_launchpad",
            "surface_direction": [1, 0.3, 0.2],
            "expected_context": context,
        },
    )
    assert result["ok"] is False
    assert result["code"] == "stale_context"
    assert engine.query("debug_snapshot") == actual
    result = engine.query(
        "control_result",
        command={
            "op": "move_launchpad",
            "surface_direction": [0, 0, 0],
        },
    )
    assert result["ok"] is False
    assert engine.query("debug_snapshot") == actual


def test_custom_surface_site_survives_archive_and_reset_but_not_explicit_new_world(engine):
    engine.command(op="move_launchpad", surface_direction=[1, 0.3, 0.2])
    moved = engine.query("debug_snapshot")
    site = engine.query("observation")["launch_site_direction"]
    engine.command(op="set_command", accel=[0, 0, 0.5], angular_accel=[0, 0, 0])
    engine.command(op="step", seconds=0.05)
    archived = engine.query("debug_snapshot")
    engine.command(op="reset", seed=100)
    reset = engine.query("debug_snapshot")
    assert reset["time_s"] == 0
    assert engine.query("observation")["launch_site_direction"] == site
    for actor_id in ("Shuttle", "LandingPad"):
        actual = next(actor for actor in reset["assets"] if actor["id"] == actor_id)
        initial = next(actor for actor in moved["assets"] if actor["id"] == actor_id)
        np.testing.assert_array_equal(actual["position"], initial["position"])
        np.testing.assert_allclose(actual["rotation"], initial["rotation"], atol=1e-12)
    engine.command(op="restore", state=archived)
    assert engine.query("debug_snapshot") == archived
    engine.command(
        op="configure_flight",
        settings=deepcopy(engine.baseline_settings),
        seed=100,
        preset="far_side_l2",
    )
    assert engine.query("observation")["launch_site_direction"] is None


def test_impact_cloud_has_real_motion_spin_and_angular_momentum_without_an_orbit_track(engine):
    settings = deepcopy(engine.baseline_settings)
    settings["debris"]["count"] = 64
    defaults = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())
    settings["debris"]["distribution"] = {
        "kind": "impact_ejecta",
        "settings": defaults["impact_ejecta_defaults"],
    }
    engine.command(op="configure_flight", settings=settings, seed=100, preset="far_side_l2")
    before = engine.query("debug_snapshot")
    physical = engine.query("field_diagnostics")
    assert physical["ok"] is True
    assert physical["report"]["physical_count"] == 64
    assert physical["report"]["moving_count"] > 0
    assert physical["report"]["mean_spin_rad_s"] > 0
    rocks = [actor for actor in before["assets"] if actor["kind"] == "asteroid"]
    assert len(rocks) == 64
    assert max(np.linalg.norm(np.cross(row["position"], row["velocity"])) for row in rocks) > 1
    flow = engine.query("observation")["debris_flow"]
    rejected = engine.query("control_result", command={"op": "tune_debris_flow", "settings": flow})
    assert rejected["ok"] is False
    assert engine.query("debug_snapshot") == before
    engine.command(op="step", seconds=0.5)
    after = engine.query("debug_snapshot")
    evolved = [actor for actor in after["assets"] if actor["kind"] == "asteroid"]
    assert any(a["position"] != b["position"] for a, b in zip(rocks, evolved, strict=True))
    assert any(a["velocity"] != b["velocity"] for a, b in zip(rocks, evolved, strict=True))


@pytest.mark.parametrize("count", [10_000, 100_000])
def test_large_counts_construct_and_advance_actual_wasm_bodies_with_small_acknowledgements(
    engine, count
):
    settings = deepcopy(engine.baseline_settings)
    settings["debris"]["count"] = count
    try:
        acknowledgement = engine.query(
            "control_result",
            command={
                "op": "configure_flight",
                "settings": settings,
                "seed": 100,
                "preset": "far_side_l2",
            },
            wait_s=180,
        )
        assert acknowledgement == {"ok": True}
        actual = engine.query("field_diagnostics", wait_s=180)
        assert actual["ok"] is True
        assert actual["report"]["physical_count"] == count
        assert actual["report"]["moving_count"] == count
        before = engine.query("observation", wait_s=180)
        assert before["geometry"]["debris_count"] == count
        moved = engine.query(
            "control_result",
            command={
                "op": "move_launchpad",
                "surface_direction": [1, 0.3, 0.2],
                "expected_context": _context(before, engine.query("runtime")),
            },
            wait_s=180,
        )
        assert moved == {"ok": True}
        assert engine.query("field_diagnostics", wait_s=180)["report"]["physical_count"] == count
        engine.command(op="step", seconds=0.05, wait_s=180)
        after = engine.query("observation", wait_s=180)
        assert after["time_s"] == pytest.approx(before["time_s"] + 0.05)
        physical = engine.query("field_diagnostics", wait_s=180)["report"]
        assert physical["physical_count"] == count
        assert physical["source_time_s"] == after["time_s"]
        assert physical["max_speed_mps"] > 0
    finally:
        engine.command(
            op="configure_flight",
            settings=deepcopy(engine.baseline_settings),
            seed=100,
            preset="far_side_l2",
            wait_s=180,
        )
