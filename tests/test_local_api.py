"""Input checks and real loopback transport errors, without a substitute engine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import pytest

from wsorb.local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalBusyError,
    LocalSimulation,
    SensorUnavailableError,
    StaleContextError,
    _connection,
    _leased_command_request,
    _ownership_command,
    _resilience_command,
    _seconds,
    _vector,
    controller_fence,
    validate_surface_direction,
)

ROOT = Path(__file__).resolve().parents[1]


def test_connection_file_is_local_private_and_does_not_connect_on_load(tmp_path):
    path = tmp_path / "local.json"
    token = secrets.token_urlsafe(32)
    path.write_text(json.dumps({"version": 1, "url": "http://127.0.0.1:1/", "token": token}))
    path.chmod(0o600)
    client = LocalSimulation.from_file(path)
    assert client.url == "http://127.0.0.1:1"
    assert token not in repr(client)
    path.chmod(0o644)
    with pytest.raises(LocalApiError, match="private regular file"):
        LocalSimulation.from_file(path)
    path.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(LocalApiError, match="private regular file"):
        LocalSimulation.from_file(link)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://example.com:8000",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8000/other",
        "http://127.0.0.1:8000?secret=value",
    ],
)
def test_descriptor_cannot_send_credentials_outside_loopback(url):
    with pytest.raises(LocalApiError, match="127.0.0.1"):
        _connection({"version": 1, "url": url, "token": secrets.token_urlsafe(32)})


@pytest.mark.parametrize(
    "value",
    [
        [0, 0],
        [[0, 0, 0]],
        [0, np.nan, 0],
        [0, np.inf, 0],
        [0, 0, 1j],
        [True, False, True],
        ["0", "0", "0"],
    ],
)
def test_command_vectors_are_finite_real_three_vectors(value):
    with pytest.raises(ValueError, match="finite real vector"):
        _vector(value, "accel")


def test_command_vector_conversion_does_not_modify_the_input():
    value = np.array([0.0, 0.0, 0.5])
    result = _vector(value, "accel")
    result[0] = 1.0
    np.testing.assert_array_equal(value, [0.0, 0.0, 0.5])


def test_surface_direction_validation_is_not_spherical_position_or_a_network_request():
    values = np.array([1e308, -1e308, 1e308])
    direction = validate_surface_direction(values)
    np.testing.assert_allclose(direction, np.array([1.0, -1.0, 1.0]) / np.sqrt(3))
    np.testing.assert_array_equal(values, [1e308, -1e308, 1e308])
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    for invalid in ([0, 0, 0], [np.nan, 0, 0], [1, 0], [True, False, False]):
        with pytest.raises(ValueError):
            client.move_launchpad(invalid)


@pytest.mark.parametrize("value", [-1, 1.01, np.nan, np.inf, True, "1"])
def test_notebook_steps_are_bounded_before_connecting(value):
    with pytest.raises(ValueError, match="between 0 and 1"):
        _seconds(value)


def test_invalid_policy_inputs_cannot_send_a_command():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(ValueError, match="finite real vector"):
        client.run_step([np.nan, 0, 0])
    with pytest.raises(ValueError, match="between 0 and 1"):
        client.run_step([0, 0, 0], seconds=2)
    for request in [{"op": "observe"}, {"op": "restore"}, {"op": "eval"}, {"op": []}]:
        with pytest.raises(ValueError, match="Unsupported local lab command"):
            client.command(request)


def test_leased_command_construction_keeps_the_original_input_clock_and_context():
    """Pure request metadata only, not a substituted engine or network response."""
    observation = {
        "time_s": 12.5,
        "trial_revision": 3,
        "tuning_revision": 4,
        "manual_interventions": 2,
        "seed": 100,
        "sensor_health": {"schema_version": 1, "revision": 7},
    }
    metadata = {"observation": observation, "runtime": {"running": True}}
    accel = np.array([0.3, 0.4, 0.0])
    request = _leased_command_request(accel, [0, 0, 0.05], metadata, lease_sim_s=0.75)
    assert request == {
        "op": "set_leased_command",
        "accel": [0.3, 0.4, 0.0],
        "angular_accel": [0.0, 0.0, 0.05],
        "lease_sim_s": 0.75,
        "source_time_s": 12.5,
        "expected_context": {
            "trial_revision": 3,
            "tuning_revision": 4,
            "manual_interventions": 2,
            "seed": 100,
            "running": True,
        },
        "expected_sensor_revision": 7,
    }
    request["accel"][0] = 0
    request["expected_context"]["trial_revision"] = 99
    np.testing.assert_array_equal(accel, [0.3, 0.4, 0])
    assert observation["trial_revision"] == 3
    assert observation["time_s"] == 12.5
    assert observation["sensor_health"]["revision"] == 7


def test_lease_metadata_and_bounds_are_required_before_connecting():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    context = {
        "trial_revision": 3,
        "tuning_revision": 4,
        "manual_interventions": 2,
        "seed": 100,
        "running": True,
    }
    request = {
        "op": "set_leased_command",
        "accel": [0, 0, 0],
        "angular_accel": [0, 0, 0],
        "lease_sim_s": 0.75,
        "source_time_s": 12.5,
        "expected_context": context,
        "expected_sensor_revision": 7,
    }
    invalid = [
        {key: value for key, value in request.items() if key != missing} for missing in request
    ]
    invalid.extend(
        {**request, key: value}
        for key, value in [
            ("extra", 0),
            ("accel", [0.4, 0.4, 0]),
            ("accel", [False, 0, 0]),
            ("accel", [np.nan, 0, 0]),
            ("angular_accel", [0.04, 0.04, 0]),
            ("angular_accel", [0, 0]),
            ("lease_sim_s", 0.049),
            ("lease_sim_s", 1.001),
            ("lease_sim_s", True),
            ("lease_sim_s", np.inf),
            ("source_time_s", None),
            ("source_time_s", -0.1),
            ("source_time_s", np.nan),
            ("source_time_s", np.inf),
            ("source_time_s", 2**2048),
            ("source_time_s", True),
            ("source_time_s", "12.5"),
            ("expected_sensor_revision", None),
            ("expected_sensor_revision", -1),
            ("expected_sensor_revision", 2**32),
            ("expected_sensor_revision", 1.5),
            ("expected_sensor_revision", True),
            ("expected_context", None),
            ("expected_context", {**context, "extra": 0}),
            ("expected_context", {**context, "seed": True}),
            ("expected_context", {**context, "running": 1}),
        ]
    )
    for command in invalid:
        with pytest.raises(ValueError):
            client.command(command)
    observation = {
        **{key: value for key, value in context.items() if key != "running"},
        "time_s": 12.5,
        "sensor_health": {"schema_version": 1, "revision": 7},
    }
    for changed in [
        {key: value for key, value in observation.items() if key != "time_s"},
        {**observation, "time_s": None},
        {**observation, "sensor_health": None},
        {**observation, "sensor_health": {"schema_version": 1}},
        {**observation, "sensor_health": {"schema_version": True, "revision": 7}},
        {**observation, "sensor_health": {"schema_version": 2, "revision": 7}},
        {**observation, "sensor_health": {"schema_version": 1, "revision": True}},
        {**observation, "sensor_health": {"schema_version": 1, "revision": 2**32}},
    ]:
        with pytest.raises(ValueError):
            client.set_leased_command(
                [0, 0, 0], [0, 0, 0], {"observation": changed, "runtime": {"running": True}}
            )
    for metadata in ({}, {"observation": observation}, {"observation": observation, "runtime": {}}):
        with pytest.raises(ValueError):
            client.set_leased_command([0, 0, 0], [0, 0, 0], metadata)


def test_fenced_command_metadata_is_strict_and_copied_without_transport_io():
    context = {
        "trial_revision": 3,
        "tuning_revision": 4,
        "manual_interventions": 2,
        "seed": 100,
        "running": True,
    }
    metadata = {
        "observation": {
            **{key: value for key, value in context.items() if key != "running"},
            "time_s": 12.5,
            "sensor_health": {"schema_version": 1, "revision": 7},
        },
        "runtime": {"running": True},
    }
    fence = {"epoch": 5, "term": 8, "run_id": "run_1", "controller_id": "controller-A"}
    request = _leased_command_request(
        [0, 0, 0.1], [0, 0, 0], metadata, lease_sim_s=0.75, controller_fence=fence
    )
    assert request["controller_fence"] == fence
    assert request["source_time_s"] == 12.5
    request["controller_fence"]["term"] = 99
    assert fence["term"] == 8
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    invalid = [None, {}, {**fence, "extra": 0}]
    invalid.extend(
        {key: value for key, value in fence.items() if key != missing} for missing in fence
    )
    invalid.extend(
        {**fence, key: value}
        for key, value in [
            ("epoch", True),
            ("epoch", -1),
            ("epoch", 2**32),
            ("term", 1.5),
            ("term", None),
            ("run_id", ""),
            ("run_id", "run.id"),
            ("run_id", "run_1\n"),
            ("run_id", "a" * 65),
            ("controller_id", "é"),
            ("controller_id", []),
        ]
    )
    for value in invalid:
        with pytest.raises(ValueError):
            client.command({**request, "controller_fence": value})


def test_ownership_envelopes_validate_exact_fields_without_connecting():
    context = {
        "trial_revision": 3,
        "tuning_revision": 4,
        "manual_interventions": 2,
        "seed": 100,
        "running": True,
    }
    claim = {
        "op": "claim_control",
        "run_id": "run_1",
        "controller_id": "controller-A",
        "expected_epoch": 5,
        "expected_term": 8,
        "source_time_s": 12.5,
        "lease_sim_s": 0.75,
        "expected_context": context,
    }
    release = {
        "op": "release_control",
        "run_id": "run_1",
        "expected_epoch": 5,
        "expected_context": context,
    }
    assert _ownership_command(claim) == claim
    assert _ownership_command(release) == release
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    for valid in (claim, release):
        invalid = [{**valid, "extra": 0}]
        invalid.extend(
            {key: value for key, value in valid.items() if key != missing} for missing in valid
        )
        invalid.extend(
            {**valid, key: value}
            for key, value in [
                ("run_id", ""),
                ("run_id", "run/1"),
                ("run_id", "run_1\n"),
                ("run_id", "a" * 65),
                ("expected_epoch", True),
                ("expected_epoch", -1),
                ("expected_epoch", 2**32),
                ("expected_context", None),
                ("expected_context", {**context, "running": 1}),
            ]
        )
        if valid is claim:
            invalid.extend(
                {**valid, key: value}
                for key, value in [
                    ("controller_id", []),
                    ("controller_id", "c.1"),
                    ("expected_term", True),
                    ("expected_term", 2**32),
                    ("source_time_s", 2**2048),
                    ("source_time_s", -1),
                    ("lease_sim_s", 0.049),
                    ("lease_sim_s", 1.001),
                ]
            )
        for command in invalid:
            with pytest.raises(ValueError):
                client.command(command)
    for metadata in ({}, {"observation": {}}, {"observation": {"control_authority": {}}}):
        with pytest.raises(ValueError):
            client.claim_control("run_1", "controller-A", metadata)
        with pytest.raises(ValueError):
            client.release_control("run_1", 5, metadata)
        with pytest.raises(ValueError):
            controller_fence(metadata)


def test_sensor_fault_input_checks_do_not_touch_the_network():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    for target, mode, duration in [
        ("radar", "unavailable", 5),
        ([], "unavailable", 5),
        ("radar_bus", "broken", 5),
        ("all_avionics", [], 5),
        ("camera_top", "frozen", 0.049),
        ("camera_bottom", "unavailable", 300.001),
        ("navigation", "nominal", np.nan),
        ("radar_backup", "frozen", np.inf),
        ("radar_primary", "unavailable", True),
        ("radar_primary", "unavailable", None),
    ]:
        with pytest.raises(ValueError):
            client.set_sensor_fault(target, mode, duration_sim_s=duration)
    fault = {"op": "set_sensor_fault", "target": "radar_bus", "mode": "unavailable"}
    for request in [
        fault,
        {**fault, "mode": "frozen"},
        {**fault, "mode": "nominal", "duration_sim_s": None},
        {**fault, "duration_sim_s": 5, "extra": True},
        {"op": "set_sensor_fault", "mode": "nominal"},
        {"op": "clear_sensor_faults", "target": "radar_bus"},
    ]:
        with pytest.raises(ValueError):
            client.command(request)
    nominal = {**fault, "mode": "nominal"}
    assert _resilience_command(nominal) == nominal
    assert _resilience_command({"op": "clear_sensor_faults"}) == {"op": "clear_sensor_faults"}


def test_native_rejection_error_types_keep_stable_codes():
    for error, code in [
        (LocalBusyError("A control call is already in flight"), "local_busy"),
        (StaleContextError("Flight context changed"), "stale_context"),
        (CommandExpiredError("Decision lease expired"), "command_expired"),
        (SensorUnavailableError("Camera delivery is unavailable"), "sensor_unavailable"),
        (
            SensorUnavailableError("No frame has been acquired", code="sensor_no_sample"),
            "sensor_no_sample",
        ),
    ]:
        assert isinstance(error, LocalApiError)
        assert error.code == code
        assert str(error)
    with pytest.raises(ValueError, match="sensor rejection code"):
        SensorUnavailableError("Unknown rejection", code="invented_sample")


def test_error_parsing_only_classifies_explicit_predispatch_contention_as_busy():
    """Pure error-envelope parsing, without substituting a transport or engine."""
    from wsorb.standby import known_busy

    token = secrets.token_urlsafe(32)
    client = LocalSimulation("http://127.0.0.1:1", token)
    rejected = client._rejection({"code": "local_busy", "error": f"Contention {token}"}, "HTTP 409")
    assert isinstance(rejected, LocalBusyError)
    assert known_busy(rejected)
    assert token not in str(rejected)
    for failure in (
        None,
        {"error": "A local control call is already in flight"},
        {"code": "busy", "error": "Engine failure"},
        {"code": "unknown", "error": "Outcome may be unknown"},
        {"code": "stale_context", "error": "An old command was rejected"},
    ):
        assert not known_busy(client._rejection(failure, "HTTP 409"))


@contextmanager
def _running_local_server(tmp_path, *, headless=False, diagnostic_output=None, diagnostics=None):
    node = shutil.which("node")
    if node is None or not (ROOT / "worker/public/index.html").is_file():
        pytest.skip("The real local transport check needs Node and assets from just build")
    connection = tmp_path / "connection.json"
    env = {
        **os.environ,
        "PORT": "0",
        "WSORB_LAB": "1",
        "WSORB_HEADLESS": "1" if headless else "0",
        "WSORB_LOCAL_CONNECTION": str(connection),
        "WSORB_LAB_TOKEN_FILE": str(tmp_path / "marimo.token"),
    }
    if diagnostics is None:
        env.pop("WSORB_DIAGNOSTICS", None)
    else:
        env["WSORB_DIAGNOSTICS"] = "1" if diagnostics else "0"
    process = subprocess.Popen(
        [node, "worker/scripts/local.mjs"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = ""
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 30
            while not re.search(r"http://127\.0\.0\.1:\d+/", output):
                assert selector.select(timeout=max(0, deadline - time.monotonic())), (
                    "Local server did not print its listening URL"
                )
                chunk = os.read(process.stdout.fileno(), 4096)
                assert chunk, process.stderr.read()
                output += chunk.decode("utf-8", errors="replace")
                assert len(output) < 65536, "Local server produced too much startup output"
        client = LocalSimulation.from_file(connection)
        assert stat.S_IMODE(connection.stat().st_mode) == 0o600
        yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if diagnostic_output is not None:
            diagnostic_output.append(output + process.stdout.read() + process.stderr.read())
        process.stdout.close()
        process.stderr.close()
    assert not connection.exists(), "Shutdown should remove its own connection descriptor"


@pytest.fixture
def local_server(tmp_path):
    with _running_local_server(tmp_path) as client:
        yield client


@pytest.fixture
def headless_server(tmp_path):
    with _running_local_server(tmp_path, headless=True) as client:
        yield client


def _http(client, route, *, headers=None, payload=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    return build_opener(ProxyHandler({})).open(
        Request(f"{client.url}{route}", data=data, headers=headers or {}, method=method), timeout=5
    )


def _wait_for_actual_snapshot(client, predicate, *, timeout_s=8):
    deadline = time.monotonic() + timeout_s
    while True:
        snapshot = client.snapshot()
        if predicate(snapshot):
            return snapshot
        assert time.monotonic() < deadline, "The actual headless runtime did not reach this state"
        time.sleep(0.025)


def test_headless_requires_both_explicit_private_server_opt_ins(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("The opt-in check needs Node")
    connection = tmp_path / "connection.json"
    result = subprocess.run(
        [node, "worker/scripts/local.mjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "PORT": "0",
            "WSORB_LAB": "0",
            "WSORB_HEADLESS": "1",
            "WSORB_LOCAL_CONNECTION": str(connection),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "requires the private WSORB_LAB=1" in result.stderr
    assert not connection.exists()


def test_headless_reports_actual_physics_without_a_browser_or_renderer(headless_server):
    client = headless_server
    status = client.status()
    assert status["enabled"] is True
    assert status["backend"] == "native_headless"
    assert status["connected"] is False
    assert status["page_id"] is None
    assert status["page_binding_supported"] is False
    assert "page_visible" not in status
    assert status["busy"] is False
    assert status["physics"]["started"] is True
    assert status["physics"]["phase"] == "idle"
    assert status["physics_ready"] is True
    assert status["physics_status_cached"] is False
    assert status["headless_error"] is None
    manifest = json.loads((ROOT / "worker/public/sim/manifest.json").read_text())
    assert status["engine_build"] == manifest
    actual = client.snapshot()
    assert actual["engine_build"] == manifest
    assert actual["renderer"]["started"] is False
    assert actual["renderer"]["ready"] is False
    assert actual["runtime"]["running"] is False
    assert actual["observation"]["time_s"] == 0
    assert actual["observation"]["applied_physics"]["mass_kg"] > 0
    assert actual["observation"]["experiment_settings"]["debris"]["count"] == 500
    assert actual["telemetry"]["source_time_s"] == 0
    assert actual["telemetry"]["applied_physics"]["mass_kg"] > 0
    assert actual["cameras"]["configured"] is False
    assert actual["cameras"]["views"] == []
    cameras = [
        unit
        for unit in actual["observation"]["sensor_health"]["units"]
        if unit["id"] in ("camera_top", "camera_bottom")
    ]
    assert len(cameras) == 2
    assert all(unit["sample_time_s"] is None and unit["sequence"] is None for unit in cameras)
    assert client._token not in json.dumps({"status": status, "snapshot": actual})


def test_headless_real_timer_advances_playback_and_pause_is_applied_natively(headless_server):
    client = headless_server
    before = client.snapshot()
    assert client.play_checked(before) == {"ok": True}
    advanced = _wait_for_actual_snapshot(
        client, lambda value: value["observation"]["time_s"] >= 0.12
    )
    assert advanced["runtime"]["running"] is True
    assert advanced["runtime"]["rate"] == 1
    assert 0 <= advanced["telemetry"]["source_time_s"] <= advanced["observation"]["time_s"]
    client.pause()
    paused = client.snapshot()
    time.sleep(0.08)
    held = client.snapshot()
    assert held["runtime"]["running"] is False
    assert held["observation"]["time_s"] == paused["observation"]["time_s"]
    assert held["renderer"]["ready"] is False


def test_headless_manual_step_reads_actual_rapier_application_and_cleanup(headless_server):
    client = headless_server
    before = client.place_lander([25000, 0, 0])
    actual = client.run_step([0, 0, 0.1], seconds=0.2)
    physics = actual["observation"]["applied_physics"]
    assert actual["observation"]["time_s"] == pytest.approx(before["observation"]["time_s"] + 0.2)
    assert actual["runtime"]["running"] is False
    assert (
        physics["velocity_world_mps"]
        != before["observation"]["applied_physics"]["velocity_world_mps"]
    )
    np.testing.assert_allclose(
        physics["last_actuator_application"]["force_body_n"],
        [0, 0, physics["mass_kg"] * 0.1],
        atol=1e-7,
    )
    cleanup = client.snapshot()
    assert cleanup["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
    assert cleanup["renderer"]["started"] is False


def test_headless_camera_failures_and_sensor_delivery_come_from_the_real_engine(headless_server):
    client = headless_server
    before = client.snapshot()
    for camera in (client.topCamera, client.bottomCamera):
        with pytest.raises(SensorUnavailableError) as failure:
            client.capture_image(camera)
        assert failure.value.code == "sensor_no_sample"
    frozen = client.set_sensor_fault("camera_top", "frozen", duration_sim_s=1)
    assert frozen["cameras"]["views"] == []
    with pytest.raises(SensorUnavailableError) as failure:
        client.capture_image(client.topCamera)
    assert failure.value.code == "sensor_no_sample"
    lost = client.set_sensor_fault("navigation", "unavailable", duration_sim_s=1)
    assert lost["observation"]["shuttle"] is None
    assert lost["observation"]["applied_physics"] is None
    assert lost["telemetry"]["status"] == "unavailable"
    assert lost["runtime"]["running"] is before["runtime"]["running"]
    cleared = client.clear_sensor_faults()
    assert cleared["observation"]["time_s"] == before["observation"]["time_s"]
    recovered = client.step(0.2)
    assert recovered["observation"]["applied_physics"]["mass_kg"] > 0


def test_headless_native_fences_exclude_standby_until_actual_sim_expiry(headless_server):
    client = headless_server
    before = client.place_lander([25000, 0, 0])
    client.play_checked(before)
    running = _wait_for_actual_snapshot(client, lambda value: value["runtime"]["running"])
    fresh_running = _wait_for_actual_snapshot(
        client,
        lambda value: value["observation"]["time_s"] > running["observation"]["time_s"] + 0.1,
    )
    with pytest.raises(CommandExpiredError):
        client.claim_control("run_1", "leader", running, lease_sim_s=0.05)
    running = fresh_running
    consent_epoch = running["observation"]["control_authority"]["epoch"]
    leader = client.claim_control("run_1", "leader", running, lease_sim_s=0.75)
    first = controller_fence(leader)
    assert first == {
        key: leader["observation"]["control_authority"][key]
        for key in ("epoch", "term", "run_id", "controller_id")
    }
    assert first["epoch"] == consent_epoch
    assert first["term"] > running["observation"]["control_authority"]["term"]
    with pytest.raises(StaleContextError):
        client.claim_control("run_1", "standby", leader, expected_epoch=consent_epoch)
    fresh = client.snapshot()
    assert client.set_leased_command(
        [0, 0, 0.1], [0, 0, 0], fresh, lease_sim_s=0.75, controller_fence=first
    ) == {"ok": True}
    expired = _wait_for_actual_snapshot(
        client, lambda value: value["observation"]["control_authority"]["status"] == "expired"
    )
    assert expired["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
    standby = client.claim_control(
        "run_1", "standby", expired, expected_epoch=consent_epoch, lease_sim_s=1
    )
    second = controller_fence(standby)
    assert second["term"] > first["term"]
    assert second["epoch"] == first["epoch"]
    with pytest.raises(StaleContextError):
        client.set_leased_command([0, 0, 0.1], [0, 0, 0], standby, controller_fence=first)
    assert client.release_control("run_1", consent_epoch, client.snapshot()) == {"ok": True}
    released = client.snapshot()
    assert released["runtime"]["running"] is True
    assert released["observation"]["control_authority"]["status"] != "active"
    assert released["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]


def test_headless_manual_takeover_rejects_old_claim_write_and_release_without_cleanup_fallback(
    headless_server,
):
    client = headless_server
    leader = client.claim_control("run_1", "leader", client.snapshot(), lease_sim_s=1)
    fence = controller_fence(leader)
    client.SetCommand([0.1, 0, 0], [0, 0, 0])
    manual = client.snapshot()
    assert manual["observation"]["control_authority"]["epoch"] != fence["epoch"]
    assert manual["runtime"]["running"] is False
    with pytest.raises(StaleContextError):
        client.claim_control("run_1", "standby", manual, expected_epoch=fence["epoch"])
    with pytest.raises(StaleContextError):
        client.set_leased_command([0, 0, 0], [0, 0, 0], manual, controller_fence=fence)
    with pytest.raises(StaleContextError):
        client.release_control("run_1", fence["epoch"], manual)
    after = client.snapshot()
    assert after["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0.1, 0, 0]
    assert after["observation"]["time_s"] == manual["observation"]["time_s"]
    assert after["runtime"]["running"] is False


def test_headless_never_accepts_browser_page_leases_or_browser_results(headless_server):
    client = headless_server
    headers = {"Authorization": f"Bearer {client._token}", "Content-Type": "application/json"}
    for route, payload in [("/__local/poll", None), ("/__local/result", {})]:
        with pytest.raises(HTTPError) as rejected:
            _http(client, route, headers=headers, payload=payload)
        try:
            assert rejected.value.code == 409
        finally:
            rejected.value.close()
    with _http(client, "/__local/bootstrap.js", headers=headers) as response:
        assert client._token not in response.read().decode()
    with _http(client, "/") as response:
        page = response.read().decode()
        assert "Headless simulation" in page
        assert 'href="/notebook.html"' in page
        assert "/__local/bootstrap.js" not in page
        assert "<canvas" not in page
        assert client._token not in page
    assert client.status()["connected"] is False


def test_real_server_requires_an_actual_ready_browser(local_server):
    client = local_server
    status = client.status()
    assert status["enabled"] is True
    assert status["connected"] is False
    assert status["page_id"] is None
    assert status["page_binding_supported"] is True
    assert "page_visible" not in status
    assert status["busy"] is False
    assert client._token not in json.dumps(status)
    for request in [client.snapshot, lambda: client.command({"op": "pause"})]:
        with pytest.raises(LocalApiError, match="[Bb]rowser|[Gg]ame|ready|engine"):
            request()
    assert client.status()["busy"] is False
    with _http(client, "/") as response:
        assert "/__local/" in response.read().decode()


@pytest.mark.parametrize("headless", [False, True], ids=["absent_browser", "native_headless"])
def test_target_page_guard_rejects_before_browser_or_native_dispatch(tmp_path, headless):
    """Real negative transport requests, without claiming a page or starting a flight."""
    with _running_local_server(tmp_path, headless=headless) as client:
        headers = {
            "Authorization": f"Bearer {client._token}",
            "Content-Type": "application/json",
        }
        target = "12345678-abcd-4abc-8def-123456789abc"
        before = client.snapshot() if headless else None
        scenario = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())
        # These are request metadata, not a substituted native state or result.
        context = {
            "trial_revision": 0,
            "tuning_revision": 0,
            "manual_interventions": 0,
            "seed": 100,
            "running": False,
        }
        calls = [
            {"method": "snapshot"},
            {"method": "capture", "camera": "top"},
            {"method": "preview", "preview": None},
            {"method": "command", "command": {"op": "reset", "seed": 101}},
            {
                "method": "command",
                "command": {
                    "op": "configure_flight",
                    "settings": scenario["experiment_settings"],
                    "preset": "mars_facing_l1",
                    "seed": 101,
                },
            },
            {"method": "command", "command": {"op": "play", "rate": 1}},
            {
                "method": "command",
                "command": {"op": "set_command", "accel": [0.1, 0, 0], "angular_accel": [0, 0, 0]},
            },
            {
                "method": "command",
                "command": {
                    "op": "claim_control",
                    "run_id": "guard_test",
                    "controller_id": "guard_test",
                    "expected_epoch": 0,
                    "expected_term": 0,
                    "source_time_s": 0,
                    "lease_sim_s": 1,
                    "expected_context": context,
                },
            },
        ]
        for call in calls:
            with pytest.raises(HTTPError) as rejected:
                _http(
                    client,
                    "/__local/call",
                    headers={**headers, "X-WSORB-Target-Page": target, "X-WSORB-Page": target},
                    payload=call,
                )
            with rejected.value as response:
                assert response.code == 409
                assert response.headers.get("X-WSORB-Page") is None
                assert json.load(response) == {
                    "ok": False,
                    "error": "The target browser page is not connected to this local simulation",
                    "code": "page_mismatch",
                }
            status = client.status()
            assert status["connected"] is status["busy"] is False
            assert status["busy_operation"] is None
            assert status["page_id"] is None
            assert status["page_binding_supported"] is not headless
            assert "page_visible" not in status

        if headless:
            # Unbound legacy reads still use the real engine and never invent a page receipt.
            with _http(
                client, "/__local/call", headers=headers, payload={"method": "snapshot"}
            ) as response:
                assert response.status == 200
                assert response.headers.get("X-WSORB-Page") is None
                envelope = json.load(response)
            assert envelope["ok"] is True
            after = envelope["value"]
            assert set(after) == {
                "observation",
                "telemetry",
                "navigation",
                "runtime",
                "renderer",
                "engine_build",
                "cameras",
            }
            assert after["renderer"]["started"] is after["renderer"]["ready"] is False
            assert after["runtime"]["running"] is False
            for field in (
                "time_s",
                "seed",
                "trial_revision",
                "tuning_revision",
                "manual_interventions",
                "control_authority",
                "applied_physics",
                "experiment_settings",
            ):
                assert after["observation"][field] == before["observation"][field]
        else:
            with pytest.raises(HTTPError) as unavailable:
                _http(client, "/__local/call", headers=headers, payload={"method": "snapshot"})
            with unavailable.value as response:
                assert response.code == 503
                assert response.headers.get("X-WSORB-Page") is None
                assert "code" not in json.load(response)


@pytest.mark.parametrize("headless", [False, True], ids=["absent_browser", "native_headless"])
def test_malformed_target_page_never_falls_through_to_an_unbound_call(tmp_path, headless):
    with _running_local_server(tmp_path, headless=headless) as client:
        headers = {
            "Authorization": f"Bearer {client._token}",
            "Content-Type": "application/json",
        }
        target = "12345678-abcd-4abc-8def-123456789abc"
        before = client.snapshot() if headless else None
        payload = {"method": "command", "command": {"op": "reset", "seed": 101}}
        for malformed in ("", "not-a-uuid", target.upper(), f"{target}, {target}", "0" * 36):
            with pytest.raises(HTTPError) as rejected:
                _http(
                    client,
                    "/__local/call",
                    headers={**headers, "X-WSORB-Target-Page": malformed},
                    payload=payload,
                )
            with rejected.value as response:
                assert response.code == 400
                assert response.headers.get("X-WSORB-Page") is None
                error = json.load(response)
                assert error == {
                    "ok": False,
                    "error": "X-WSORB-Target-Page must be a canonical UUID",
                }

        address = urlsplit(client.url)
        connection = HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            body = json.dumps(payload).encode()
            connection.putrequest("POST", "/__local/call")
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader("X-WSORB-Target-Page", target)
            connection.putheader("X-WSORB-Target-Page", target)
            connection.putheader("Content-Length", str(len(body)))
            connection.putheader("Connection", "close")
            connection.endheaders(body)
            with connection.getresponse() as response:
                assert response.status == 400
                assert response.headers.get("X-WSORB-Page") is None
                assert json.load(response)["ok"] is False
        finally:
            connection.close()
        status = client.status()
        assert status["connected"] is status["busy"] is False
        assert status["busy_operation"] is status["page_id"] is None
        if headless:
            after = client.snapshot()
            for field in ("time_s", "seed", "trial_revision", "control_authority"):
                assert after["observation"][field] == before["observation"][field]


@pytest.mark.parametrize("headless", [False, True], ids=["absent_browser", "native_headless"])
def test_malformed_visibility_cannot_claim_or_renew_a_browser_page(tmp_path, headless):
    with _running_local_server(tmp_path, headless=headless) as client:
        status = client.status()
        headers = {
            "Authorization": f"Bearer {client._token}",
            "Content-Type": "application/json",
            "X-WSORB-Page": "12345678-abcd-4abc-8def-123456789abc",
            "X-WSORB-Build": status["engine_build"]["sha256"],
        }
        for malformed in ("", "true", "false", "2", "1, 0"):
            for route, payload in (("/__local/poll", None), ("/__local/result", {})):
                with pytest.raises(HTTPError) as rejected:
                    _http(
                        client,
                        route,
                        headers={**headers, "X-WSORB-Page-Visible": malformed},
                        payload=payload,
                    )
                with rejected.value as response:
                    assert response.code == 400
                    assert json.load(response) == {
                        "ok": False,
                        "error": "X-WSORB-Page-Visible must be 1 or 0",
                    }
        status = client.status()
        assert status["connected"] is status["busy"] is False
        assert status["page_id"] is None
        assert "page_visible" not in status


@pytest.mark.parametrize("diagnostics", [None, True, False], ids=["default_off", "on", "off"])
def test_startup_asset_logs_require_the_server_flag_and_only_report_delivery(tmp_path, diagnostics):
    output = []
    canary = secrets.token_urlsafe(32)
    with _running_local_server(
        tmp_path, diagnostic_output=output, diagnostics=diagnostics
    ) as client:
        flags = {"X-WSORB-Diagnostics": "1", "X-WSORB-Request-ID": canary}
        expected = []
        app_etag = None
        for path, label, method in (
            ("/", "index", "GET"),
            ("/app.js", "app", "GET"),
            ("/styles.css", "styles", "HEAD"),
            ("/sim/manifest.json", "engine_manifest", "GET"),
            ("/sim/wsorb_bevy.js", "engine_glue", "GET"),
            ("/sim/wsorb_bevy_bg.wasm", "engine_wasm", "HEAD"),
            ("/__local/bootstrap.js", "local_bootstrap", "GET"),
            ("/__local/client.js", "local_client", "GET"),
        ):
            headers = dict(flags)
            if path.startswith("/__local/"):
                headers["Authorization"] = f"Bearer {client._token}"
                route = path
            else:
                route = f"{path}?private={canary}#private={canary}"
            with _http(client, route, headers=headers, method=method) as response:
                assert response.status == 200
                data = response.read()
                if path == "/app.js":
                    app_etag = response.headers["ETag"]
            expected.append((label, method, 200, len(data)))
        with pytest.raises(HTTPError) as unchanged:
            _http(client, "/app.js", headers={**flags, "If-None-Match": app_etag})
        with unchanged.value as response:
            assert response.code == 304
            assert response.read() == b""
        expected.append(("app", "GET", 304, 0))

        # Other assets, failed Host/auth checks and request flags cannot add startup records.
        with _http(client, f"/asset-notices.txt?private={canary}", headers=flags) as response:
            response.read()
        for path, headers, status in (
            ("/", {**flags, "Host": f"{canary}.invalid"}, 403),
            ("/__local/bootstrap.js", flags, 403),
            ("/__local/status", flags, 401),
        ):
            with pytest.raises(HTTPError) as denied:
                _http(client, path, headers=headers)
            with denied.value as response:
                assert response.code == status
                assert client._token not in response.read().decode()
        status = client.status()
        assert status["connected"] is status["busy"] is False
        assert status["page_id"] is None
        token = client._token
    text = "".join(output)
    assert canary not in text and token not in text
    events = [
        event
        for line in text.splitlines()
        if line.startswith("{")
        and (event := json.loads(line)).get("schema") == "wsorb.diagnostics.v1"
    ]
    assets = [event for event in events if event["event"] == "asset_finished"]
    if diagnostics is not True:
        assert events == []
        return
    assert [
        (event["asset"], event["http_method"], event["http_status"], event["bytes"])
        for event in assets
    ] == expected
    for event in assets:
        assert set(event) <= {
            "schema",
            "component",
            "event",
            "wall_time_ms",
            "asset",
            "http_method",
            "http_status",
            "bytes",
            "omitted_events",
        }
        assert event["component"] == "local_relay"
        assert type(event["bytes"]) is int and event["bytes"] >= 0
    for second in {event["wall_time_ms"] // 1000 for event in events}:
        assert sum(event["wall_time_ms"] // 1000 == second for event in events) <= 10


def test_local_game_has_an_accessible_connection_status_region(local_server):
    from html.parser import HTMLParser

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if "id" in attributes:
                self.ids[attributes["id"]] = (tag, attributes)

    with _http(local_server, "/") as response:
        page = response.read().decode()
    elements = Elements()
    elements.feed(page)
    assert elements.ids["local-python-connection"][1]["aria-label"] == "Local Python connection"
    assert elements.ids["local-python-status"][1]["role"] == "status"
    assert elements.ids["python-permission"][0] == "dialog"
    assert local_server.status()["connected"] is False


def test_separate_game_does_not_redirect_notebook_to_another_backend(local_server):
    # The fixture deliberately uses its own explicit descriptor, not the owner's
    # default one. Its notebook route must not silently attach another flight.
    with _http(
        local_server,
        "/notebook.html",
        headers={"Authorization": f"Bearer {local_server._token}"},
    ) as response:
        assert response.status == 200
        page = response.read().decode()
    assert "Separate game connection" in page
    assert "will not redirect you to that editor" in page
    assert "--connection-file" in page
    assert local_server._token not in page


def test_new_world_placement_and_checked_commands_validate_before_requiring_a_real_browser(
    local_server,
):
    headers = {"Authorization": f"Bearer {local_server._token}", "Content-Type": "application/json"}
    scenario = json.loads((ROOT / "simulation/scenarios/phobos_escape.json").read_text())
    settings = scenario["experiment_settings"]
    valid = [
        {"op": "configure_flight", "settings": settings, "seed": 100, "preset": "mars_facing_l1"},
        {"op": "configure_flight", "settings": settings, "preset": "far_side_l2"},
        {"op": "place_lander", "position_m": [15000.0, 200.0, 100.0]},
        {"op": "view", "focus": "Debris100000"},
        {
            "op": "set_command",
            "accel": [0.1, 0, 0],
            "angular_accel": [0, 0, 0],
            "expected_context": {
                "trial_revision": 0,
                "tuning_revision": 0,
                "manual_interventions": 0,
                "seed": 100,
                "running": False,
            },
        },
    ]
    checked_command = valid[-1]
    valid.append(
        {
            "op": "move_launchpad",
            "surface_direction": [1, 0.3, 0.2],
            "expected_context": checked_command["expected_context"],
        }
    )
    valid.append({"op": "play", "rate": 1, "expected_context": checked_command["expected_context"]})
    invalid = [
        {**valid[0], "preset": "moon_facing_mars_l2"},
        {"op": "place_lander", "position_m": [15000, 0]},
        {"op": "place_lander", "position_m": [10000001, 0, 0]},
        {"op": "view", "focus": "Debris100001"},
        {"op": "move_launchpad", "surface_direction": [0, 0, 0]},
        {"op": "move_launchpad", "surface_direction": [1, 0]},
        {
            **checked_command,
            "expected_context": {**checked_command["expected_context"], "running": 1},
        },
        {
            **checked_command,
            "expected_context": {**checked_command["expected_context"], "trial_revision": -1},
        },
        {**valid[-1], "expected_context": {**valid[-1]["expected_context"], "running": 1}},
        {**valid[-1], "expected_context": {**valid[-1]["expected_context"], "trial_revision": -1}},
    ]
    for requests, expected in [(valid, 503), (invalid, 400)]:
        for command in requests:
            with pytest.raises(HTTPError) as rejected:
                _http(
                    local_server,
                    "/__local/call",
                    headers=headers,
                    payload={"method": "command", "command": command},
                )
            try:
                assert rejected.value.code == expected
            finally:
                rejected.value.close()
    assert local_server.status()["busy"] is False
    assert local_server.status()["busy_operation"] is None


def test_fault_and_lease_server_validation_requires_an_actual_browser_for_valid_inputs(
    local_server,
):
    headers = {"Authorization": f"Bearer {local_server._token}", "Content-Type": "application/json"}
    lease = {
        "op": "set_leased_command",
        "accel": [0.3, 0.4, 0],
        "angular_accel": [0, 0, 0.05],
        "lease_sim_s": 0.75,
        "source_time_s": 12.5,
        "expected_context": {
            "trial_revision": 3,
            "tuning_revision": 4,
            "manual_interventions": 2,
            "seed": 100,
            "running": True,
        },
        "expected_sensor_revision": 7,
    }
    valid = [{"op": "clear_sensor_faults"}]
    for target in (
        "radar_primary",
        "radar_backup",
        "camera_top",
        "camera_bottom",
        "radar_bus",
        "navigation",
        "all_avionics",
    ):
        valid.extend(
            [
                {"op": "set_sensor_fault", "target": target, "mode": "nominal"},
                {
                    "op": "set_sensor_fault",
                    "target": target,
                    "mode": "frozen",
                    "duration_sim_s": 0.05,
                },
                {
                    "op": "set_sensor_fault",
                    "target": target,
                    "mode": "unavailable",
                    "duration_sim_s": 300,
                },
            ]
        )
    valid.extend(
        [lease, {**lease, "lease_sim_s": 0.05, "source_time_s": 0}, {**lease, "lease_sim_s": 1.0}]
    )
    fence = {"epoch": 5, "term": 8, "run_id": "run_1", "controller_id": "controller-A"}
    claim = {
        "op": "claim_control",
        "run_id": fence["run_id"],
        "controller_id": fence["controller_id"],
        "expected_epoch": fence["epoch"],
        "expected_term": fence["term"],
        "source_time_s": lease["source_time_s"],
        "lease_sim_s": lease["lease_sim_s"],
        "expected_context": lease["expected_context"],
    }
    release = {
        "op": "release_control",
        "run_id": fence["run_id"],
        "expected_epoch": fence["epoch"],
        "expected_context": lease["expected_context"],
    }
    valid.extend(
        [
            {**lease, "controller_fence": fence},
            claim,
            release,
            {**claim, "run_id": "a" * 64, "controller_id": "_-", "expected_term": 2**32 - 1},
        ]
    )
    fault = {"op": "set_sensor_fault", "target": "radar_bus", "mode": "unavailable"}
    invalid = [
        fault,
        {**fault, "mode": "frozen"},
        {**fault, "mode": "nominal", "duration_sim_s": None},
        {**fault, "duration_sim_s": 0.049},
        {**fault, "duration_sim_s": 300.001},
        {**fault, "duration_sim_s": True},
        {**fault, "duration_sim_s": "5"},
        {**fault, "duration_sim_s": 5, "target": "radar"},
        {**fault, "duration_sim_s": 5, "mode": "noisy"},
        {**fault, "duration_sim_s": 5, "extra": 0},
        {"op": "clear_sensor_faults", "duration_sim_s": 5},
    ]
    invalid.extend(
        {key: value for key, value in lease.items() if key != missing} for missing in lease
    )
    invalid.extend(
        {**lease, key: value}
        for key, value in [
            ("extra", 0),
            ("accel", [0.4, 0.4, 0]),
            ("angular_accel", [0.04, 0.04, 0]),
            ("accel", [0, 0]),
            ("angular_accel", [False, 0, 0]),
            ("lease_sim_s", 0.049),
            ("lease_sim_s", 1.001),
            ("lease_sim_s", True),
            ("source_time_s", -1),
            ("source_time_s", None),
            ("source_time_s", True),
            ("source_time_s", "12.5"),
            ("expected_sensor_revision", -1),
            ("expected_sensor_revision", 2**32),
            ("expected_sensor_revision", 1.5),
            ("expected_sensor_revision", True),
            ("expected_context", None),
            ("expected_context", {**lease["expected_context"], "running": 1}),
            ("expected_context", {**lease["expected_context"], "seed": -1}),
            ("expected_context", {**lease["expected_context"], "extra": 0}),
        ]
    )
    invalid.extend(
        {**lease, "controller_fence": value}
        for value in [
            None,
            {},
            {**fence, "extra": 0},
            {**fence, "epoch": True},
            {**fence, "epoch": -1},
            {**fence, "term": 2**32},
            {**fence, "run_id": ""},
            {**fence, "run_id": "a" * 65},
            {**fence, "run_id": "run_1\n"},
            {**fence, "controller_id": "c.1"},
        ]
    )
    for valid_ownership in (claim, release):
        invalid.extend(
            {key: value for key, value in valid_ownership.items() if key != missing}
            for missing in valid_ownership
        )
        invalid.extend(
            {**valid_ownership, key: value}
            for key, value in [
                ("extra", 0),
                ("run_id", ""),
                ("run_id", "a" * 65),
                ("run_id", "r/1"),
                ("run_id", "run_1\n"),
                ("expected_epoch", -1),
                ("expected_epoch", 2**32),
                ("expected_epoch", True),
                ("expected_context", None),
                ("expected_context", {**lease["expected_context"], "seed": True}),
            ]
        )
    invalid.extend(
        {**claim, key: value}
        for key, value in [
            ("controller_id", None),
            ("controller_id", "c.1"),
            ("expected_term", True),
            ("expected_term", -1),
            ("expected_term", 2**32),
            ("source_time_s", -1),
            ("source_time_s", True),
            ("lease_sim_s", 0.049),
            ("lease_sim_s", 1.001),
        ]
    )
    for commands, expected in [(valid, 503), (invalid, 400)]:
        for command in commands:
            with pytest.raises(HTTPError) as rejected:
                _http(
                    local_server,
                    "/__local/call",
                    headers=headers,
                    payload={"method": "command", "command": command},
                )
            try:
                assert rejected.value.code == expected
                failure = json.load(rejected.value)
                assert failure["ok"] is False
                assert local_server._token not in json.dumps(failure)
            finally:
                rejected.value.close()
    assert local_server.status()["connected"] is False
    assert local_server.status()["busy"] is False


def test_real_planetary_maps_and_attribution_are_served_intact(local_server):
    manifest = json.loads((ROOT / "worker/web/assets/asset-manifest.json").read_text())
    with _http(local_server, "/assets/asset-manifest.json") as response:
        assert json.load(response) == manifest
    for asset in manifest["textures"]:
        with _http(local_server, f"/assets/{asset['path']}") as response:
            data = response.read()
            assert response.headers["Content-Type"] == "image/png"
            assert int(response.headers["Content-Length"]) == asset["bytes"] == len(data)
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert hashlib.sha256(data).hexdigest() == asset["sha256"]
        assert data == (ROOT / "worker/web/assets" / asset["path"]).read_bytes()
    with _http(local_server, "/asset-notices.txt") as response:
        notice = response.read().decode()
        assert notice == (ROOT / "worker/web/assets/ATTRIBUTION.md").read_text()
        assert "USGS" in notice and "NASA" in notice and "public domain" in notice
    for source in manifest["sources"]:
        with pytest.raises(HTTPError) as failure:
            _http(local_server, f"/assets/{source['path']}")
        try:
            assert failure.value.code == 404, "Ordinary builds must not publish source JPEGs"
        finally:
            failure.value.close()


def test_streaming_loader_compiles_real_http_wasm_and_enforces_its_integrity(local_server):
    """Production loader, actual HTTP bytes and actual WebAssembly. No substituted APIs."""
    script = """
import assert from 'node:assert/strict';
import { loadVerifiedEngine } from './worker/web/engine-loader.ts';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const { url } = JSON.parse(input);
const manifest = await (await fetch(new URL('/sim/manifest.json', url))).json();
const wasmUrl = new URL(`/sim/wsorb_bevy_bg.wasm?v=${manifest.sha256}`, url).href;
const samples = [];
const loaded = await loadVerifiedEngine(wasmUrl, manifest, (bytes, total) => {
  assert.equal(total, manifest.bytes);
  assert.ok(bytes >= (samples.at(-1) ?? 0) && bytes <= total);
  samples.push(bytes);
});
assert.ok(loaded.module instanceof WebAssembly.Module);
assert.equal(samples.at(-1), manifest.bytes);
assert.equal(loaded.timings.streaming_compile, true);
assert.equal(loaded.timings.decoded_bytes, manifest.bytes);
for (const [name, value] of Object.entries(loaded.timings)) {
  if (name.endsWith('_ms')) assert.ok(Number.isFinite(value) && value >= 0, name);
}
assert.ok(loaded.timings.verified_module_ms >= loaded.timings.integrity_ready_ms);
assert.ok(loaded.timings.verified_module_ms >= loaded.timings.module_ready_ms);
const api = await import(`./worker/public/sim/wsorb_bevy.js?v=${manifest.sha256}`);
await api.default({ module_or_path: loaded.module });
const observation = JSON.parse(api.observation());
assert.equal(observation.max_accel_mps2, 0.5);
assert.equal(observation.max_angular_accel_rad_s2, 0.05);
assert.ok(observation.applied_physics.mass_kg > 0);
assert.equal(JSON.parse(api.renderer_status()).ready, false);
await assert.rejects(
  loadVerifiedEngine(wasmUrl, { ...manifest, sha256: '0'.repeat(64) }), /integrity check/);
await assert.rejects(
  loadVerifiedEngine(wasmUrl, { ...manifest, bytes: manifest.bytes - 1 }), /declared length/);
console.log(JSON.stringify(loaded.timings));
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"url": local_server.url}),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    timings = json.loads(result.stdout)
    assert timings["decoded_bytes"] > 0


def test_real_static_assets_revalidate_without_retransferring_and_private_index_stays_uncached(
    local_server,
):
    for path in ("/sim/wsorb_bevy_bg.wasm", "/assets/textures/phobos-viking-albedo.png"):
        with _http(local_server, path) as response:
            assert response.headers["Cache-Control"] == "public, max-age=0, must-revalidate"
            etag = response.headers["ETag"]
            assert etag.startswith('W/"')
            assert response.headers["Last-Modified"]
            response.read()
        with pytest.raises(HTTPError) as unchanged:
            _http(local_server, path, headers={"If-None-Match": etag})
        try:
            assert unchanged.value.code == 304
            assert unchanged.value.headers["ETag"] == etag
            assert unchanged.value.read() == b""
        finally:
            unchanged.value.close()
    with _http(local_server, "/") as response:
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["ETag"] is None


def test_actual_browser_request_options_keep_post_origin_and_private_transport_guards(local_server):
    """Use production options and real HTTP, without faking a browser/engine reply."""
    script = """
import assert from 'node:assert/strict';
import { bridgeRequestOptions } from './worker/web/local-api.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const { url, token } = JSON.parse(input);
const page = crypto.randomUUID();
const options = bridgeRequestOptions(token, page, 'call',
  JSON.stringify({ method: 'snapshot' }), {}, undefined);
const request = new Request(new URL('/__local/call', url), options);
assert.equal(request.referrerPolicy, 'same-origin');
assert.equal(request.mode, 'same-origin');
assert.equal(request.credentials, 'omit');
assert.equal(request.redirect, 'error');
assert.equal(request.method, 'POST');
assert.ok(request.headers.get('authorization') === `Bearer ${token}`);
const response = await fetch(request);
assert.equal(response.status, 503);
const failure = await response.json();
assert.equal(failure.ok, false);
assert.match(failure.error, /browser/i);
console.log('PASS');
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        cwd=ROOT,
        input=json.dumps({"url": local_server.url, "token": local_server._token}),
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


def test_notebook_button_falls_back_to_explicit_setup_without_publishing_a_secret(local_server):
    client = local_server
    with _http(client, "/notebook.html") as response:
        assert response.headers["Cache-Control"] == "no-store"
        text = response.read().decode()
    assert "Separate game connection" in text
    assert "--connection-file" in text
    assert "access_token=" not in text
    with _http(
        client, "/notebook.html", headers={"Authorization": f"Bearer {client._token}"}
    ) as response:
        assert "--connection-file" in response.read().decode()


def test_camera_and_experiment_inputs_are_validated_before_reaching_a_browser():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    for camera in ("unknown", "TopCamera"):
        with pytest.raises(ValueError, match="camera"):
            client.PanTiltCamera(camera, 0, 0)
        with pytest.raises(ValueError, match="camera"):
            client.capture_image(camera)
    for angle in (np.nan, np.inf, True, "30"):
        with pytest.raises(ValueError, match="finite"):
            client.PanTiltCamera(client.topCamera, angle, 0)
    with pytest.raises(ValueError):
        client.configure_experiment({"ship": {"mass_kg": np.nan}})


def test_live_tuning_and_rays_validate_without_touching_the_network():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    ship = {"mass_kg": 3388.7, "inertia_scale": 1.0, "thrust_scale": 1.0}
    for key, value in [
        ("mass_kg", 99),
        ("mass_kg", np.nan),
        ("mass_kg", 2**2048),
        ("inertia_scale", 11),
        ("thrust_scale", True),
        ("thrust_scale", 0),
    ]:
        with pytest.raises(ValueError):
            client.tune_ship({**ship, key: value})
    with pytest.raises(ValueError):
        client.tune_ship({**ship, "extra": 1})
    flow = {"speed_mps": 20, "speed_spread_mps": 5, "stream_axis": "y", "stream_sign": -1}
    for key, value in [
        ("speed_mps", 0),
        ("speed_spread_mps", 11),
        ("stream_axis", "north"),
        ("stream_sign", True),
    ]:
        with pytest.raises(ValueError):
            client.tune_debris_flow({**flow, key: value})
    for target, distance in [
        ([0, 0], 250),
        ([0, np.nan, 0], 250),
        ([0, 0, 0], 0),
        ([0, 0, 0], True),
    ]:
        with pytest.raises(ValueError):
            client.raycast(target, distance)


def test_real_python_clients_share_control_serialization_but_not_the_capture_channel():
    first = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    second = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    assert first._channels is second._channels
    assert first._channels.control is not first._channels.capture
    entered = threading.Event()
    waiting = threading.Event()

    def contend():
        waiting.set()
        with second._channels.control:
            entered.set()

    with first._channels.control:
        worker = threading.Thread(target=contend)
        worker.start()
        assert waiting.wait(1)
        assert not entered.is_set()
        assert second._channels.capture.acquire(blocking=False)
        second._channels.capture.release()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert entered.is_set()


@pytest.mark.parametrize("server_fixture", ["local_server", "headless_server"])
def test_real_server_rejects_unauthenticated_cross_origin_and_rebound_requests(
    request, server_fixture
):
    client = request.getfixturevalue(server_fixture)
    with pytest.raises(HTTPError) as denied:
        _http(client, "/__local/status")
    assert denied.value.code in (401, 403)
    denied.value.close()
    for extra in [
        {"Origin": "https://example.com"},
        {"Origin": "null"},
        {"Host": "untrusted.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ]:
        with pytest.raises(HTTPError) as denied:
            _http(
                client,
                "/__local/status",
                headers={"Authorization": f"Bearer {client._token}", **extra},
            )
        assert denied.value.code == 403
        denied.value.close()
    with pytest.raises(HTTPError) as denied:
        _http(client, "/", headers={"Host": "untrusted.example"})
    assert denied.value.code == 403
    denied.value.close()
    with pytest.raises(HTTPError) as denied:
        _http(
            client,
            "/__local/call",
            headers={
                "Authorization": f"Bearer {client._token}",
                "Content-Type": "application/json",
            },
            payload={"method": "eval", "code": "not executable"},
        )
    assert denied.value.code == 400
    denied.value.close()
    for module in ("bootstrap.js", "client.js"):
        with pytest.raises(HTTPError) as denied:
            _http(client, f"/__local/{module}")
        try:
            assert denied.value.code == 403
            assert client._token not in denied.value.read().decode()
        finally:
            denied.value.close()
    headers = {"Authorization": f"Bearer {client._token}", "Content-Type": "application/json"}
    address = urlsplit(client.url)
    connection = HTTPConnection(address.hostname, address.port, timeout=5)
    try:
        # The real server rejects oversized Content-Length before reading a
        # body. Read that response before uploading, avoiding a socket-close race.
        connection.putrequest("POST", "/__local/call")
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(1024 * 1024 + 1))
        connection.putheader("Connection", "close")
        connection.endheaders()
        with connection.getresponse() as oversized:
            assert oversized.status == 413
            assert client._token not in oversized.read().decode()
    finally:
        connection.close()
    with pytest.raises(HTTPError) as query:
        _http(client, "/__local/status?token=not-accepted", headers=headers)
    try:
        assert query.value.code == 400
    finally:
        query.value.close()
