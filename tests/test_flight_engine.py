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
import time
from collections import Counter, deque
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

from wsorb.flight import (
    PlannerConfig,
    ResilientGuidance,
    basic_command,
    plan_route,
    predict_obstacles,
)
from wsorb.flight.control import reference_to_body
from wsorb.local_api import control_context

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


def _sensor_units(observation):
    return {unit["id"]: unit for unit in observation["sensor_health"]["units"]}


def _leased_command(engine, observation, accel, *, lease_sim_s=0.2):
    """Use only actual engine receipts for the command's source fence and clock."""
    return {
        "op": "set_leased_command",
        "accel": np.asarray(accel).tolist(),
        "angular_accel": [0, 0, 0],
        "source_time_s": observation["time_s"],
        "lease_sim_s": lease_sim_s,
        "expected_context": control_context(
            {"observation": observation, "runtime": engine.query("runtime")}
        ),
        "expected_sensor_revision": observation["sensor_health"]["revision"],
    }


def test_real_sensor_domains_freeze_original_samples_without_changing_the_physical_world(engine):
    before = engine.query("observation")
    assert before["radar"] and before["radar_backup"]
    assert before["radar"] != before["radar_backup"]
    physical = engine.query("debug_snapshot")
    primary = _sensor_units(before)["radar_primary"]
    engine.command(op="set_sensor_fault", target="radar_primary", mode="frozen", duration_sim_s=2.0)
    changed = engine.query("debug_snapshot")
    assert changed["assets"] == physical["assets"]
    assert changed["rng_state"] == physical["rng_state"]
    assert changed["time_s"] == physical["time_s"]
    engine.command(op="step", seconds=0.24)
    frozen = engine.query("observation")
    units = _sensor_units(frozen)
    assert frozen["radar"] == before["radar"]
    assert units["radar_primary"]["delivery"] == "frozen"
    assert units["radar_primary"]["sequence"] == primary["sequence"]
    assert units["radar_primary"]["sample_time_s"] == primary["sample_time_s"]
    assert units["radar_backup"]["sample_time_s"] > primary["sample_time_s"]
    assert engine.query("observation") == frozen, "reads must not renew a frozen sample"
    engine.command(op="set_sensor_fault", target="radar_bus", mode="unavailable", duration_sim_s=2)
    lost = engine.query("observation")
    assert lost["radar"] == lost["radar_backup"] == []
    assert all(
        _sensor_units(lost)[unit]["delivery"] == "unavailable"
        for unit in ("radar_primary", "radar_backup")
    )
    engine.command(op="clear_sensor_faults")
    engine.command(op="step", seconds=0.12)
    recovered = engine.query("observation")
    assert _sensor_units(recovered)["radar_primary"]["delivery"] == "ready"
    assert _sensor_units(recovered)["radar_primary"]["sample_time_s"] > primary["sample_time_s"]
    assert recovered["time_s"] > before["time_s"]
    assert recovered["seed"] == before["seed"]


def test_real_avionics_blackout_has_no_navigation_or_camera_readback_bypass(engine):
    before = engine.query("observation")
    engine.command(
        op="set_sensor_fault", target="all_avionics", mode="unavailable", duration_sim_s=0.5
    )
    lost = engine.query("observation")
    assert lost["shuttle"] is lost["target"] is lost["applied_physics"] is None
    assert lost["radar"] == lost["radar_backup"] == []
    assert lost["time_s"] == before["time_s"]
    assert lost["manual_interventions"] == before["manual_interventions"]
    assert engine.query("telemetry")["status"] == "unavailable"
    blind = engine.query("control_result", command=_leased_command(engine, lost, [0, 0, 0.5]))
    assert blind["ok"] is False and blind["code"] == "stale_context"
    assert engine.query("observation") == lost
    engine.command(**_leased_command(engine, lost, [0, 0, 0]))
    for camera in ("top", "bottom"):
        # Genuine early rejection in Node, not a successful GPU/readback test.
        capture = engine.query("capture_result", camera=camera)
        assert capture["ok"] is False
        assert capture["code"] == "sensor_unavailable"
    engine.command(op="step", seconds=0.6)
    restored = engine.query("observation")
    assert restored["shuttle"] is not None
    assert restored["time_s"] == pytest.approx(before["time_s"] + 0.6)
    assert restored["sensor_health"]["revision"] > lost["sensor_health"]["revision"]


def test_native_command_lease_expires_without_a_python_writer_or_braking(engine):
    observation = engine.query("observation")
    command = _leased_command(engine, observation, [0, 0, 0.5])
    engine.command(**command)
    engine.command(op="step", seconds=0.05)
    moving = engine.query("observation")
    assert moving["shuttle"]["commanded_accel_mps2"] == [0, 0, 0.5]
    # No command writer or renewal runs during these actual physics substeps.
    engine.command(op="step", seconds=0.3)
    expired = engine.query("observation")
    assert expired["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    assert expired["shuttle"]["commanded_angular_accel_rad_s2"] == [0, 0, 0]
    assert expired["time_s"] == pytest.approx(observation["time_s"] + 0.35)
    assert np.linalg.norm(expired["shuttle"]["velocity"]) > 0
    rejected = engine.query("control_result", command=command)
    assert rejected["ok"] is False and rejected["code"] == "command_expired"
    assert engine.query("observation") == expired
    fresh = _leased_command(engine, expired, [0, 0, 0.5])
    engine.command(**fresh)
    engine.command(op="set_command", accel=[0, 0, 0.2], angular_accel=[0, 0, 0])
    engine.command(op="step", seconds=0.4)
    assert engine.query("observation")["shuttle"]["commanded_accel_mps2"] == [0, 0, 0.2]


def test_python_resilience_uses_actual_backup_then_coasts_through_shared_failure(engine):
    guidance = ResilientGuidance()
    config = PlannerConfig()

    def update():
        observation = engine.query("observation")
        ready = guidance.update(
            observation,
            wall_time_s=time.monotonic(),
            running=engine.query("runtime")["running"],
        )
        route = None
        if ready.can_command:
            route = plan_route(
                guidance.frame_for_planning(predict_obstacles(ready.observation, config)), config
            )
        command, angular = guidance.command(ready.observation, route, {})
        assert np.linalg.norm(command) <= 0.5
        assert np.linalg.norm(angular) <= 0.05
        return observation, ready, command

    observation, ready, command = update()
    for _ in range(4):
        if ready.can_command:
            break
        engine.command(op="step", seconds=0.12)
        observation, ready, command = update()
    assert ready.can_command, guidance.report
    assert np.linalg.norm(command) > 0, "the healthy route controller must actually request lift"
    engine.command(**_leased_command(engine, observation, command, lease_sim_s=ready.lease_sim_s))
    engine.command(op="step", seconds=0.05)
    applied = engine.query("observation")
    assert applied["time_s"] > observation["time_s"]
    assert (
        np.linalg.norm(applied["applied_physics"]["last_actuator_application"]["force_body_n"]) > 0
    )
    engine.command(
        op="set_sensor_fault", target="radar_primary", mode="unavailable", duration_sim_s=2
    )
    observation, ready, command = update()
    assert ready.observation["radar"] == observation["radar_backup"]
    assert ready.requires_replan
    engine.command(op="set_sensor_fault", target="radar_bus", mode="unavailable", duration_sim_s=2)
    _, ready, command = update()
    assert not ready.can_command
    np.testing.assert_array_equal(command, np.zeros(3))
    engine.command(op="clear_sensor_faults")
    _, recovering, command = update()
    assert not recovering.can_command, "held pre-recovery samples cannot rearm control"
    for _ in range(8):
        engine.command(op="step", seconds=0.12)
        _, recovering, command = update()
        if recovering.can_command:
            break
    assert recovering.can_command, guidance.report


def test_local_braking_reserve_is_below_actual_stock_actuator_deceleration(engine):
    """Measure real default-world braking, without assigning position or speed."""
    initial = engine.query("observation")
    assert initial["geometry"]["debris_count"] == 500
    assert initial["experiment_settings"]["debris"]["speed_mps"] == 20.0
    assert initial["experiment_settings"]["debris"]["speed_spread_mps"] == 4.0
    for _ in range(8):
        observation = engine.query("observation")
        position = np.asarray(observation["shuttle"]["position"])
        reference = position / np.linalg.norm(position) * 0.5
        body = reference_to_body(reference, observation["shuttle"]["rotation"], "active_xyzw")
        engine.command(**_leased_command(engine, observation, body, lease_sim_s=0.75))
        engine.command(op="step", seconds=0.25)
    before = engine.query("observation")
    velocity = np.asarray(before["shuttle"]["velocity"])
    speed = np.linalg.norm(velocity)
    assert speed > 0.75, "The real actuator must first produce measurable velocity"
    reference = -velocity / speed * 0.5
    body = reference_to_body(reference, before["shuttle"]["rotation"], "active_xyzw")
    engine.command(**_leased_command(engine, before, body, lease_sim_s=0.75))
    engine.command(op="step", seconds=0.5)
    after = engine.query("observation")
    duration = after["time_s"] - before["time_s"]
    deceleration = (speed - np.linalg.norm(after["shuttle"]["velocity"])) / duration
    assert deceleration >= ResilientGuidance().config.local_braking_accel_mps2
    # Also measure the opposing-gravity case. Produce inward motion with real
    # bounded burns, then brake outward; never assign a velocity or move a body.
    for _ in range(10):
        observation = engine.query("observation")
        position = np.asarray(observation["shuttle"]["position"])
        reference = -position / np.linalg.norm(position) * 0.5
        body = reference_to_body(reference, observation["shuttle"]["rotation"], "active_xyzw")
        engine.command(**_leased_command(engine, observation, body, lease_sim_s=0.75))
        engine.command(op="step", seconds=0.25)
    inward = engine.query("observation")
    position = np.asarray(inward["shuttle"]["position"])
    velocity = np.asarray(inward["shuttle"]["velocity"])
    speed = np.linalg.norm(velocity)
    assert np.dot(position, velocity) < 0 and speed > 0.25
    body = reference_to_body(-velocity / speed * 0.5, inward["shuttle"]["rotation"], "active_xyzw")
    engine.command(**_leased_command(engine, inward, body, lease_sim_s=0.75))
    engine.command(op="step", seconds=0.5)
    after = engine.query("observation")
    duration = after["time_s"] - inward["time_s"]
    deceleration = (speed - np.linalg.norm(after["shuttle"]["velocity"])) / duration
    assert deceleration >= ResilientGuidance().config.local_braking_accel_mps2
    assert after["mission"]["damage"] == 0
    assert after["geometry"]["debris_count"] == 500
    assert after["manual_interventions"] == initial["manual_interventions"]
    assert after["trial_revision"] == initial["trial_revision"]


def test_transit_guidance_damps_genuine_native_spin_with_the_existing_angular_cap(engine):
    """Create angular momentum through real bounded burns, never assigned state."""
    initial = engine.query("observation")
    for index in range(12):
        observation = engine.query("observation")
        position = np.asarray(observation["shuttle"]["position"])
        linear = reference_to_body(
            position / np.linalg.norm(position) * 0.5,
            observation["shuttle"]["rotation"],
            "active_xyzw",
        )
        request = _leased_command(engine, observation, linear, lease_sim_s=0.75)
        request["angular_accel"] = [0.0, 0.05 if index >= 8 else 0.0, 0.0]
        engine.command(**request)
        engine.command(op="step", seconds=0.25)
    before = engine.query("observation")
    spin = np.linalg.norm(before["shuttle"]["angular_velocity_rad_s"])
    assert spin > 0.02
    guidance = ResilientGuidance()
    ready = guidance.update(
        before, wall_time_s=time.monotonic(), running=engine.query("runtime")["running"]
    )
    linear, angular = guidance.command(ready.observation, None, {})
    assert ready.can_command
    assert np.linalg.norm(linear) <= 0.5
    assert 0.0 < np.linalg.norm(angular) <= 0.05
    request = _leased_command(engine, before, linear, lease_sim_s=ready.lease_sim_s)
    request["angular_accel"] = angular.tolist()
    engine.command(**request)
    engine.command(op="step", seconds=0.25)
    after = engine.query("observation")
    assert np.linalg.norm(after["shuttle"]["angular_velocity_rad_s"]) < spin
    assert after["mission"]["damage"] == 0
    assert after["geometry"]["debris_count"] == 500
    assert after["experiment_settings"] == initial["experiment_settings"]
    assert after["manual_interventions"] == initial["manual_interventions"]
    assert after["trial_revision"] == initial["trial_revision"]


def test_checkpoint_arrival_with_default_resilient_guidance_on_stock_wasm(engine, tmp_path):
    """Fly the real stock trial, not a preview or asynchronous-runner substitute."""
    _fly_checkpoint(
        engine,
        seed=100,
        settings=engine.baseline_settings,
        trace_path=tmp_path / "public-flight.json",
    )


@pytest.mark.parametrize(
    "seed, trajectory_adjustment",
    [(101, "off"), (102, "off"), (100, "toward_lander")],
    ids=["seed_101", "seed_102", "attraction_on"],
)
def test_transit_checkpoint_arrival_retains_real_physics_across_seeds_and_attraction(
    engine, seed, trajectory_adjustment, tmp_path
):
    settings = deepcopy(engine.baseline_settings)
    if trajectory_adjustment == "off":
        settings["debris"].pop("trajectory_adjustment", None)
    else:
        settings["debris"]["trajectory_adjustment"] = trajectory_adjustment
    engine.command(op="configure_flight", settings=settings, seed=seed, preset="far_side_l2")
    _fly_checkpoint(
        engine, seed=seed, settings=settings, trace_path=tmp_path / "public-flight.json"
    )


def _fly_checkpoint(engine, *, seed, settings, trace_path):
    """Run the unchanged native arrival assertions for one owned physical trial."""
    initial = engine.query("observation")
    assert initial["seed"] == seed
    assert initial["flight_preset"] == "far_side_l2"
    assert initial["experiment_settings"] == settings
    assert initial["geometry"]["debris_count"] == 500
    assert initial["debris_distribution"] == {"kind": "transit_stream"}
    assert initial["experiment_settings"]["debris"]["speed_mps"] == 20.0
    assert initial["experiment_settings"]["debris"]["speed_spread_mps"] == 4.0
    assert initial["max_accel_mps2"] == 0.5
    assert initial["max_angular_accel_rad_s2"] == 0.05
    assert initial["geometry"]["arrival_radius_m"] == 50.0
    guidance = ResilientGuidance()
    config = guidance.config.planner
    route = None
    started = time.monotonic()
    max_wall_s, max_sim_s = 240.0, 1800.0
    initial_range = float(
        np.linalg.norm(np.asarray(initial["target"]["position"]) - initial["shuttle"]["position"])
    )
    gates, coasts, route_results = Counter(), Counter(), Counter()
    recent = deque(maxlen=8)
    first_coasts = []
    observation = initial
    last_thrust_time = initial["time_s"]
    commands = 0
    plans = 0
    progress_times = deque((30.0, 300.0))
    stop_reason = "simulation budget exhausted"
    public_trace = []
    next_trace_s = initial["time_s"]

    def diagnostic():
        return {
            "measurement": "real WASM deterministic stepping, not async runner or rendering",
            "initial_ship_position_m": initial["shuttle"]["position"],
            "initial_target_position_m": initial["target"]["position"],
            "initial_range_m": initial_range,
            "ship_position_m": observation["shuttle"]["position"],
            "target_position_m": observation["target"]["position"],
            "navigation_sample_time_s": observation["navigation_sample_time_s"],
            "range_m": float(
                np.linalg.norm(
                    np.asarray(observation["target"]["position"])
                    - observation["shuttle"]["position"]
                )
            ),
            "time_s": observation["time_s"],
            "wall_s": time.monotonic() - started,
            "mission": observation["mission"],
            "stop_reason": stop_reason,
            "nonzero_commands": commands,
            "plans": plans,
            "readiness_reasons": dict(gates),
            "coast_reasons": dict(coasts),
            "route_results": dict(route_results),
            "first_coasts": first_coasts,
            "recent_decisions": list(recent),
            "guidance": guidance.report,
            "nearest_public_radar_rows": sorted(
                observation["radar"], key=lambda row: np.linalg.norm(row[0])
            )[:4],
            "public_trace_path": str(trace_path),
        }

    print("checkpoint initial " + json.dumps(diagnostic(), sort_keys=True), flush=True)
    while observation["time_s"] - initial["time_s"] < max_sim_s:
        if observation["mission"]["status"] == "success":
            stop_reason = "native mission success"
            break
        if observation["mission"]["damage"] > 209_200.0:
            stop_reason = "native damage threshold exceeded"
            break
        if time.monotonic() - started >= max_wall_s:
            stop_reason = "wall computation budget exhausted"
            break
        runtime = engine.query("runtime")
        readiness = guidance.update(
            observation, wall_time_s=time.monotonic(), running=runtime["running"]
        )
        gates[readiness.report["reason"]] += 1
        planning_error = None
        if readiness.requires_replan:
            route = None
        if readiness.can_command and route is None:
            try:
                frame = guidance.frame_for_planning(
                    predict_obstacles(readiness.observation, config)
                )
                route = plan_route(frame, config)
                plans += 1
                route_results[f"{route.status}: {route.detail}"] += 1
            except ValueError as error:
                planning_error = str(error)
        accel, angular = guidance.command(readiness.observation, route, {})
        report = guidance.report
        if observation["time_s"] >= next_trace_s:
            public_trace.append(
                {
                    "observation": observation,
                    "guidance": report,
                    "command_body_mps2": accel.tolist(),
                    "command_body_rad_s2": angular.tolist(),
                }
            )
            # Retain each genuine early decision so an escape failure can be
            # diagnosed without reconstructing observations between samples.
            elapsed_s = observation["time_s"] - initial["time_s"]
            next_trace_s = observation["time_s"] + (0.25 if elapsed_s < 240.0 else 5.0)
        assert np.linalg.norm(accel) <= 0.5 + 1e-12
        assert np.linalg.norm(angular) <= 0.05 + 1e-12
        if np.linalg.norm(accel) > 0 or np.linalg.norm(angular) > 0:
            commands += 1
            last_thrust_time = observation["time_s"]
            assert readiness.can_command
            assert readiness.lease_sim_s >= 0.05
        else:
            coasts[planning_error or report["reason"]] += 1
            if len(first_coasts) < 8:
                first_coasts.append(
                    {
                        "time_s": observation["time_s"],
                        "readiness_reason": readiness.report["reason"],
                        "command_reason": report["reason"],
                        "planning_error": planning_error,
                        "route_detail": route.detail if route is not None else None,
                    }
                )
        recent.append(
            {
                "time_s": observation["time_s"],
                "reason": report["reason"],
                "planning_error": planning_error,
                "route_status": route.status if route is not None else None,
                "route_detail": route.detail if route is not None else None,
                "desired_speed_mps": report.get("desired_speed_mps"),
                "accel_mps2": accel.tolist(),
                "lease_sim_s": readiness.lease_sim_s,
            }
        )
        if readiness.lease_sim_s >= 0.05:
            request = _leased_command(engine, observation, accel, lease_sim_s=readiness.lease_sim_s)
            request["angular_accel"] = angular.tolist()
            engine.command(**request)
        else:
            # The native minimum lease may carry zero thrust, not a fallback decision.
            np.testing.assert_array_equal(accel, np.zeros(3))
            np.testing.assert_array_equal(angular, np.zeros(3))
            engine.command(**_leased_command(engine, observation, accel, lease_sim_s=0.05))
        step_s = 0.25 if readiness.lease_sim_s >= 0.25 else 0.05
        before_time = observation["time_s"]
        engine.command(op="step", seconds=step_s)
        observation = engine.query("observation")
        if observation["mission"]["status"] == "running":
            assert observation["time_s"] == pytest.approx(before_time + step_s)
        else:
            # Actual native assessment freezes a terminal mission at its last
            # physical substep, not at the requested end of a longer step.
            assert before_time < observation["time_s"] <= before_time + step_s + 1e-6
        assert observation["manual_interventions"] == initial["manual_interventions"]
        assert observation["trial_revision"] == initial["trial_revision"]
        assert observation["experiment_settings"] == initial["experiment_settings"]
        assert observation["ship_tuning"] == initial["ship_tuning"]
        assert observation["geometry"]["debris_count"] == 500
        assert observation["max_accel_mps2"] == 0.5
        assert observation["max_angular_accel_rad_s2"] == 0.05
        if progress_times and observation["time_s"] - initial["time_s"] >= progress_times[0]:
            progress_times.popleft()
            print("checkpoint progress " + json.dumps(diagnostic(), sort_keys=True), flush=True)
        if commands == 0 and observation["time_s"] - last_thrust_time >= 5.0:
            stop_reason = "no thrust accepted by default guidance in the first five seconds"
            break

    result = diagnostic()
    public_trace.append({"observation": observation, "guidance": guidance.report})
    trace_path.write_text(json.dumps(public_trace, allow_nan=False), encoding="utf-8")
    print("checkpoint result " + json.dumps(result, sort_keys=True), flush=True)
    assert observation["mission"]["status"] == "success", json.dumps(result, sort_keys=True)
    assert result["range_m"] <= 50.0, result
    assert observation["mission"]["target_distance_m"] <= 50.0, result
    assert observation["mission"]["damage"] <= 209_200.0, result


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
    # This is the worldwide shell regression, not the new bounded route volume.
    settings["debris"]["distribution"] = {"kind": "directed_stream"}
    settings["ship"]["mass_kg"] *= 1.25
    settings["ship"]["inertia_scale"] = 1.5
    settings["debris"]["count"] = 64
    engine.command(op="configure_experiment", settings=settings, seed=100)
    after = engine.query("observation")
    # Native legacy serialization omits the explicit directed-stream default.
    serialized = deepcopy(settings)
    serialized["debris"].pop("distribution")
    assert after["experiment_settings"] == serialized
    assert after["debris_distribution"] == {"kind": "directed_stream"}
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
    # Capacity is measured in the broad shell. Packing 100000 default-sized
    # colliders into the transit corridor must be rejected, not overlap bodies.
    settings["debris"]["distribution"] = {"kind": "directed_stream"}
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
