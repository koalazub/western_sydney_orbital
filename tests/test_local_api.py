"""Input checks and real loopback transport errors, without a substitute engine."""

from __future__ import annotations

import json
import os
import re
import secrets
import selectors
import shutil
import stat
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np
import pytest

from wsorb.local_api import LocalApiError, LocalSimulation, _connection, _seconds, _vector

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
