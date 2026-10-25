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
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import pytest

from wsorb.local_api import (
    LocalApiError,
    LocalSimulation,
    _connection,
    _seconds,
    _vector,
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


@pytest.fixture
def local_server(tmp_path):
    node = shutil.which("node")
    if node is None or not (ROOT / "worker/public/index.html").is_file():
        pytest.skip("The real local transport check needs Node and assets from just build")
    connection = tmp_path / "connection.json"
    process = subprocess.Popen(
        [node, "worker/scripts/local.mjs"],
        cwd=ROOT,
        env={
            **os.environ,
            "PORT": "0",
            "WSORB_LAB": "1",
            "WSORB_LOCAL_CONNECTION": str(connection),
            "WSORB_LAB_TOKEN_FILE": str(tmp_path / "marimo.token"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "Local server did not print its listening URL"
            line = process.stdout.readline()
        assert re.search(r"http://127\.0\.0\.1:\d+/", line), line
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
        process.stdout.close()
        process.stderr.close()
    assert not connection.exists(), "Shutdown should remove its own connection descriptor"


def _http(client, route, *, headers=None, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    return build_opener(ProxyHandler({})).open(
        Request(f"{client.url}{route}", data=data, headers=headers or {}), timeout=5
    )


def test_real_server_requires_an_actual_ready_browser(local_server):
    client = local_server
    status = client.status()
    assert status["enabled"] is True
    assert status["connected"] is False
    assert status["busy"] is False
    assert client._token not in json.dumps(status)
    for request in [client.snapshot, lambda: client.command({"op": "pause"})]:
        with pytest.raises(LocalApiError, match="[Bb]rowser|[Gg]ame|ready|engine"):
            request()
    assert client.status()["busy"] is False
    with _http(client, "/") as response:
        assert "/__local/" in response.read().decode()


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


def test_notebook_button_falls_back_to_setup_without_publishing_a_secret(local_server):
    client = local_server
    with _http(client, "/notebook.html") as response:
        assert response.headers["Cache-Control"] == "no-store"
        text = response.read().decode()
    assert "devenv up lab simulation" in text
    assert "access_token=" not in text
    with _http(
        client, "/notebook.html", headers={"Authorization": f"Bearer {client._token}"}
    ) as response:
        assert "devenv up lab simulation" in response.read().decode()


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


def test_real_server_rejects_unauthenticated_cross_origin_and_rebound_requests(local_server):
    client = local_server
    with pytest.raises(HTTPError) as denied:
        _http(client, "/__local/status")
    assert denied.value.code in (401, 403)
    denied.value.close()
    for extra in [{"Origin": "https://example.com"}, {"Host": "untrusted.example"}]:
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
