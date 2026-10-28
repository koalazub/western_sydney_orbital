"""Short, real workerd/HTTP/WebSocket/WASM checks. No browser or fake readiness."""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from wsorb.local_api import CommandExpiredError, LocalApiError, StaleContextError, controller_fence

ROOT = Path(__file__).resolve().parents[1]


def test_browser_room_executor_against_actual_wasm():
    node = shutil.which("node")
    if not node or not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Use the project Node environment and existing real WASM build")
    result = subprocess.run(
        [node, "--test", "tests/room-control.test.mjs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def room_worker(tmp_path_factory):
    node = shutil.which("node")
    if not node or not (ROOT / "worker/node_modules/.bin/wrangler").is_file():
        pytest.skip("Use the existing project Node/Wrangler dependencies")
    if not (ROOT / "worker/public/sim/wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Build the existing real WASM game first")
    directory = tmp_path_factory.mktemp("room-worker")
    with (directory / "wrangler.log").open("w+") as log:
        process = subprocess.Popen(
            [
                node,
                "worker/scripts/cloudflare.mjs",
                "wrangler",
                "dev",
                "--config",
                "worker/wrangler.jsonc",
                "--local",
                "--ip",
                "127.0.0.1",
                "--port",
                "0",
                "--inspector-port",
                "0",
                "--persist-to",
                str(directory / "storage"),
                "--show-interactive-dev-session=false",
            ],
            cwd=ROOT,
            env={**os.environ, "CI": "1", "WRANGLER_SEND_METRICS": "false"},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 45
            while True:
                log.seek(0)
                output = log.read()
                ready = re.search(r"Ready on http://127\.0\.0\.1:(\d+)", output)
                if ready:
                    yield f"http://127.0.0.1:{ready[1]}", node
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail(f"The owned local Worker did not start: {output[-6000:]}")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def request(origin, route, *, token=None, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    fields = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        fields["Authorization"] = f"Bearer {token}"
    call = Request(origin + route, data=data, headers=fields)
    try:
        response = build_opener(ProxyHandler({})).open(call, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.code, json.load(response)


@pytest.fixture
def actual_room(room_worker, tmp_path):
    origin, node = room_worker
    status, room = request(origin, "/api/rooms", body={}, headers={"Origin": origin})
    assert status == 201
    path = tmp_path / "room.json"
    path.write_text(
        json.dumps({"version": 1, "url": origin, "room_id": room["id"], "token": room["key"]})
    )
    path.chmod(0o600)
    return origin, node, room, path


def await_owner(process):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=10), "The real WASM/Room peer did not report its lifecycle"
        return json.loads(process.stdout.readline())


@pytest.fixture
def room_owner(actual_room):
    origin, node, room, path = actual_room
    process = subprocess.Popen(
        [node, "tests/room-owner.mjs", str(path)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert await_owner(process) == {
            "state": "owner_connected",
            "backend": "native_headless",
            "browser_gpu_verified": False,
        }
        yield origin, node, room, path, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_room_requires_real_auth_origin_and_connected_owner(actual_room):
    from wsorb.room_api import RoomApiError, RoomSimulation

    origin, _, room, path = actual_room
    route = f"/api/rooms/{room['id']}"
    assert request(origin, route + "/state")[0] == 401
    assert request(origin, route + "/state", token="x" * 43)[0] == 401
    assert request(origin, route + "/state", token=room["key"])[1]["state"] is None
    assert (
        request(
            origin, route + "/state", token=room["key"], headers={"Origin": "https://other.invalid"}
        )[0]
        == 403
    )
    status, result = request(
        origin,
        route + "/commands",
        token=room["key"],
        body={"id": "no_owner", "command": {"method": "snapshot"}},
    )
    assert status == 503 and "No connected room owner" in result["error"]
    client = RoomSimulation.from_file(path, allow_loopback=True)
    with pytest.raises(RoomApiError) as unavailable:
        client.snapshot()
    assert unavailable.value.code == "owner_unavailable"
    assert room["key"] not in str(unavailable.value)


def test_real_public_rpc_preserves_fences_expiry_and_native_watchdog(room_owner):
    from wsorb.room_api import RoomSimulation

    origin, _, room, path, _ = room_owner
    client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    initial = client.snapshot()
    assert initial["renderer"]["ready"] is initial["renderer"]["started"] is False
    assert initial["renderer"]["rendered_frames"] == 0
    assert initial["cameras"]["configured"] is False
    assert initial["observation"]["geometry"]["debris_count"] == 500
    assert initial["observation"]["time_s"] == 0
    assert "debris" not in initial["observation"]
    assert initial["runtime"]["running"] is False
    grant = client.claim_control("room_test", "python_test", initial)
    fence = controller_fence(grant)
    assert fence["run_id"] == "room_test"
    stale = json.loads(json.dumps(initial))
    stale["observation"]["trial_revision"] += 1
    with pytest.raises(StaleContextError):
        client.set_leased_command([0, 0, 0.1], [0, 0, 0], stale, controller_fence=fence)
    client.set_leased_command([0, 0, 0.1], [0, 0, 0], initial, controller_fence=fence)
    client.play_checked(initial)
    deadline = time.monotonic() + 4
    while True:
        actual = client.snapshot()
        if actual["observation"]["time_s"] > 0.8:
            break
        assert time.monotonic() < deadline
        time.sleep(0.025)
    assert actual["runtime"]["running"] is True
    assert actual["observation"]["control_authority"]["status"] == "expired"
    assert actual["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
    with pytest.raises((CommandExpiredError, StaleContextError)):
        client.set_leased_command([0, 0, 0.1], [0, 0, 0], initial, controller_fence=fence)
    client.release_control(fence["run_id"], fence["epoch"], actual)
    after = client.snapshot()
    assert after["observation"]["control_authority"]["status"] == "revoked"
    assert after["observation"]["seed"] == 100
    assert after["observation"]["manual_interventions"] == 0
    assert after["engine_build"] == initial["engine_build"]
    # Fresh RPC replies are never mislabeled as saved room observations.
    assert request(origin, f"/api/rooms/{room['id']}/state", token=room["key"])[1]["state"] is None
    for command in ({"op": "reset"}, {"op": "set_command", "accel": [0, 0, 0]}):
        with pytest.raises((LocalApiError, ValueError)):
            client.command(command)


def test_browser_executor_rejects_unsafe_and_expired_http_calls_before_native_write(room_owner):
    from wsorb.room_api import RoomSimulation

    origin, _, room, path, _ = room_owner
    client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    initial = client.snapshot()
    calls = [
        {"version": 1, "method": "command", "command": {"op": op}}
        for op in ("reset", "pause", "step", "set_command", "configure_experiment", "stop")
    ]
    calls.append({"version": 1, "method": "snapshot", "deadline_ms": 1})
    for index, call in enumerate(calls):
        call.setdefault("deadline_ms", int(time.time() * 1000) + 2000)
        status, result = request(
            origin,
            f"/api/rooms/{room['id']}/commands",
            token=room["key"],
            body={"id": f"denied_{index}", "command": call},
        )
        assert status == 200 and result["ok"] is False
        assert result["id"] == f"denied_{index}"
        assert "value" not in result
    actual = client.snapshot()
    for field in (
        "time_s",
        "seed",
        "trial_revision",
        "tuning_revision",
        "manual_interventions",
        "control_authority",
        "applied_physics",
    ):
        assert actual["observation"][field] == initial["observation"][field]
    assert actual["runtime"]["running"] is False


def test_room_host_cannot_be_replaced_and_disconnect_fails_closed(room_owner):
    from wsorb.room_api import RoomSimulation

    origin, node, room, path, original = room_owner
    contender = subprocess.Popen(
        [node, "tests/room-owner.mjs", str(path)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert await_owner(contender)["state"] == "host_conflict"
        # Node may retain its WebSocket transport through the close handshake.
        # Stop only this owned peer after verifying the live refusal to take over.
        contender.terminate()
        contender.communicate(timeout=5)
        client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
        assert client.snapshot()["observation"]["time_s"] == 0
        original.terminate()
        original.communicate(timeout=5)
        deadline = time.monotonic() + 3
        while True:
            status, _ = request(
                origin,
                f"/api/rooms/{room['id']}/commands",
                token=room["key"],
                body={"id": "after_close", "command": {"method": "snapshot"}},
            )
            if status == 503:
                break
            assert time.monotonic() < deadline
            time.sleep(0.025)
    finally:
        if contender.poll() is None:
            contender.terminate()
            contender.communicate(timeout=5)


def test_shared_python_policy_uses_real_room_calls_and_guarded_release(room_owner):
    from wsorb.checkpoint import inspect_trial, parse_args, run_trial
    from wsorb.room_api import RoomSimulation

    _, _, _, path, _ = room_owner
    client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    inspection = inspect_trial(client)
    assert inspection["state"] == "read_only" and inspection["actuation_permitted"] is False
    assert client.snapshot()["observation"]["control_authority"]["status"] == "idle"
    options = parse_args(
        ["--room-file", str(path), "--run", "--duration-s", "0.2", "--wall-budget-s", "8"]
    )
    result = run_trial(client, options, policy="baseline")
    assert "error" not in result, result.get("error")
    assert result["state"] == "duration_limit"
    assert result["release_state"] == "acknowledged"
    assert result["command_usage"]["acknowledged_command_samples"] > 0
    assert result["final"]["time_s"] >= 0.2
    assert result["final"]["damage_j"] == 0
    assert result["world_metadata_unchanged"] is True
    assert result["final_world"]["debris_count"] == 500
    assert result["native_success"] is result["ok"] is False
