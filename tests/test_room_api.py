"""Descriptor/consent checks and genuine unavailable HTTP. No substitute engine or transport."""

from __future__ import annotations

import json
import math
import secrets
import socket
import stat
import threading
import time
import uuid
from contextlib import contextmanager

import pytest

from test_room_integration import actual_room as actual_room
from test_room_integration import room_owner as room_owner
from test_room_integration import room_worker as room_worker
from wsorb.local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalPageMismatchError,
    LocalSimulation,
    StaleContextError,
)
from wsorb.room_api import (
    DEFAULT_ORIGIN,
    MAX_ADMISSION_MS,
    MAX_TIMEOUT_MS,
    USER_AGENT,
    RoomApiError,
    RoomPageMismatchError,
    RoomSimulation,
    _descriptor,
    _http_client,
    _http_rejection,
    _origin,
    _reserve_descriptor,
    _room_call,
    _room_command,
    _rpc_envelope,
    create_room,
    main,
    parse_args,
)


def _credentials(*, url=DEFAULT_ORIGIN):
    return {
        "version": 1,
        "url": url,
        "room_id": str(uuid.uuid4()),
        "token": secrets.token_urlsafe(32),
    }


def _write_descriptor(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@contextmanager
def _unavailable_origin(scheme="https"):
    # Reserve a real loopback port without listening. No other process can
    # acquire it and there is no HTTP server or replacement engine to answer.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        yield f"{scheme}://127.0.0.1:{reserved.getsockname()[1]}"


def test_descriptor_is_private_explicit_and_does_not_connect_on_load(tmp_path):
    path = tmp_path / "room.json"
    value = _credentials()
    _write_descriptor(path, value)
    client = RoomSimulation.from_file(path)
    assert client.url == DEFAULT_ORIGIN
    assert client.room_id == value["room_id"]
    assert value["token"] not in repr(client)
    assert "key=" not in repr(client)
    assert not (tmp_path / "wsorb-room.json").exists()
    with pytest.raises(TypeError):
        RoomSimulation.from_file()


def test_creation_and_rpc_http_client_identifies_the_application_not_a_browser():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    assert USER_AGENT.startswith("wsorb-controller/")
    assert _http_client().addheaders == client._http.addheaders == [("User-Agent", USER_AGENT)]
    assert value["token"] not in str(client._http.addheaders)


def test_hosted_client_does_not_claim_to_implement_the_local_page_header_protocol():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    assert client.last_observed_page_id is client.bound_page_id is None
    assert RoomSimulation.bind_to_observed_page is RoomSimulation._unsupported
    with pytest.raises(ValueError, match="Unsupported hosted room operation"):
        client.bind_to_observed_page(require=False)


def test_controller_job_pin_requires_canonical_uuid_and_is_immutable_before_first_rpc():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    owner = str(uuid.uuid4())
    for invalid in (None, "not-a-uuid", owner.upper(), 42, "../page", str(uuid.UUID(int=0))):
        with pytest.raises(RoomApiError, match="canonical UUID"):
            client.bind_controller_page(invalid)
    assert client.bound_page_id is None
    assert client.bind_controller_page(owner) == owner
    assert client.bound_page_id == owner
    assert client.bind_controller_page(owner) == owner
    assert client.last_observed_page_id is None
    with pytest.raises(RoomApiError, match="cannot change"):
        client.bind_controller_page(str(uuid.uuid4()))
    assert client.bound_page_id == owner


def test_room_request_header_pins_explicit_jobs_only_without_changing_native_allowlist():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    envelope = _rpc_envelope({"method": "snapshot"}, 2)
    assert client._rpc_request(envelope).get_header("X-wsorb-target-page") is None
    owner = str(uuid.uuid4())
    client.bind_controller_page(owner)
    request = client._rpc_request(envelope)
    assert request.get_header("X-wsorb-target-page") == owner
    assert request.get_header("Authorization") == f"Bearer {value['token']}"
    assert request.full_url == f"{value['url']}/api/rooms/{value['room_id']}/commands"
    assert json.loads(request.data) == envelope
    for op in ("reset", "configure_experiment", "pause", "stop"):
        with pytest.raises(ValueError, match="Unsupported hosted"):
            client.command({"op": op})


def test_only_actual_http_409_and_exact_page_mismatch_code_prove_no_dispatch():
    secret = secrets.token_urlsafe(32)
    mismatch = _http_rejection(409, {"code": "page_mismatch", "error": secret})
    assert isinstance(mismatch, RoomPageMismatchError)
    assert isinstance(mismatch, LocalPageMismatchError)
    assert isinstance(mismatch, RoomApiError)
    assert not isinstance(mismatch, StaleContextError)
    assert mismatch.code == "page_mismatch" and mismatch.http_status == 409
    assert secret not in str(mismatch)
    for status, body in (
        (200, {"code": "page_mismatch", "error": secret}),
        (403, {"code": "page_mismatch", "error": secret}),
        (503, {"code": "page_mismatch", "error": secret}),
        (409, {"code": "stale_context", "error": secret}),
        (409, {"code": "page_mismatch "}),
        (409, {"code": ["page_mismatch"]}),
        (409, None),
    ):
        error = _http_rejection(status, body)
        assert type(error) is RoomApiError
        assert "outcome may be unknown" in str(error)
        assert secret not in str(error)


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o700, 0o400, 0o000])
def test_descriptor_requires_exact_private_read_write_mode(tmp_path, mode):
    path = tmp_path / "room.json"
    _write_descriptor(path, _credentials())
    path.chmod(mode)
    with pytest.raises(RoomApiError, match="private regular file"):
        RoomSimulation.from_file(path)


def test_descriptor_refuses_symlinks_directories_and_oversized_files(tmp_path):
    path = tmp_path / "room.json"
    _write_descriptor(path, _credentials())
    link = tmp_path / "link.json"
    link.symlink_to(path)
    for invalid in (link, tmp_path):
        with pytest.raises(RoomApiError, match="private regular file"):
            RoomSimulation.from_file(invalid)
    path.write_bytes(b" " * 8193)
    with pytest.raises(RoomApiError, match="8192"):
        RoomSimulation.from_file(path)


def test_malformed_private_files_do_not_disclose_contents(tmp_path):
    path = tmp_path / "room.json"
    token = secrets.token_urlsafe(32)
    path.write_text(f"{{ invalid {token}")
    path.chmod(0o600)
    with pytest.raises(RoomApiError) as rejected:
        RoomSimulation.from_file(path)
    assert token not in str(rejected.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8787",
        "http://localhost:8787",
        "https://user:secret@example.com",
        "https://example.com/",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com?key=secret",
        "https://example.com#",
        "https://example.com#key=secret",
        "https://example.com:",
        "https://example.com:0",
        "https://example.com:65536",
        "https://example.com\\evil",
        "https://example.com\n",
        " https://example.com",
        "https://",
        "https://[::1",
        "//example.com",
        "file:///private/room.json",
    ],
)
def test_production_descriptor_cannot_send_a_capability_to_an_unapproved_url(url):
    value = _credentials(url=url)
    with pytest.raises(RoomApiError) as rejected:
        _descriptor(value)
    assert "secret" not in str(rejected.value)
    assert value["token"] not in str(rejected.value)


def test_https_origin_is_canonical_without_credentials_or_a_path():
    assert _origin("HTTPS://EXAMPLE.COM:443") == "https://example.com"
    assert _origin("https://example.com:8443") == "https://example.com:8443"
    assert _origin("https://[::1]:8443") == "https://[::1]:8443"


def test_only_explicit_test_consent_allows_the_exact_ipv4_wrangler_loopback(tmp_path):
    value = _credentials(url="http://127.0.0.1:8787")
    path = tmp_path / "wrangler.json"
    _write_descriptor(path, value)
    with pytest.raises(RoomApiError):
        RoomSimulation.from_file(path)
    client = RoomSimulation.from_file(path, allow_loopback=True)
    assert client.url == value["url"]
    for origin in (
        "http://localhost:8787",
        "http://[::1]:8787",
        "http://127.0.0.1",
        "http://example.com:8787",
    ):
        with pytest.raises(RoomApiError):
            _origin(origin, allow_loopback=True)
    with pytest.raises(ValueError, match="boolean test opt-in"):
        _origin(value["url"], allow_loopback=1)
    with pytest.raises(LocalApiError):
        LocalSimulation(DEFAULT_ORIGIN, value["token"])


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"room_id": "not-a-uuid"},
        {"room_id": "../other-room"},
        {"room_id": "12345678-1234-4ABC-8DEF-123456789ABC"},
        {"token": "short"},
        {"token": "x" * 42 + "\n"},
        {"token": None},
        {"extra": "not-part-of-version-1"},
    ],
)
def test_descriptor_schema_cannot_silently_fall_back_or_inject_routes(changes):
    with pytest.raises(RoomApiError):
        _descriptor({**_credentials(), **changes})


@pytest.mark.parametrize("timeout", [True, "8", 0, -1, 30.01, math.nan, math.inf, 2**2048])
def test_timeout_is_bounded_before_any_http(timeout):
    value = _credentials()
    with pytest.raises(ValueError, match="timeout_s"):
        RoomSimulation(value["url"], value["room_id"], value["token"], timeout_s=timeout)


def test_every_other_inherited_public_operation_is_blocked_before_even_a_snapshot():
    value = _credentials(url="https://127.0.0.1:1")
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    allowed = {
        "from_file",
        "snapshot",
        "control_transaction",
        "claim_control",
        "release_control",
        "set_leased_command",
        "play_checked",
        "command",
    }
    inherited_public = {
        name
        for name in dir(LocalSimulation)
        if not name.startswith("_") and callable(getattr(LocalSimulation, name))
    }
    for name in inherited_public - allowed:
        assert getattr(RoomSimulation, name) is RoomSimulation._unsupported, name
        with pytest.raises(ValueError, match="Unsupported hosted room operation"):
            getattr(client, name)()
    with pytest.raises(ValueError, match="Unsupported hosted room operation"):
        client._request("status")


@pytest.mark.parametrize(
    "method", ["capture", "restore", "eval", "pan_tilt", "preview", "raycast", "status", "reset"]
)
def test_rpc_allowlist_rejects_nonpublic_operations_before_http(method):
    value = _credentials(url="https://127.0.0.1:1")
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    with pytest.raises(ValueError, match="Unsupported hosted room RPC"):
        client._call({"method": method})
    with pytest.raises(ValueError):
        client._call({"method": "snapshot", "extra": None})


def _checked_metadata():
    # Pure request metadata, not a fabricated engine or HTTP observation.
    context = {
        "trial_revision": 3,
        "tuning_revision": 4,
        "manual_interventions": 2,
        "seed": 100,
        "running": False,
    }
    fence = {"epoch": 5, "term": 8, "run_id": "run_1", "controller_id": "controller-A"}
    return context, fence


def test_commands_require_native_fences_exact_checks_and_rate_one_before_http():
    context, fence = _checked_metadata()
    lease = {
        "op": "set_leased_command",
        "accel": [0, 0, 0.1],
        "angular_accel": [0, 0, 0],
        "lease_sim_s": 0.75,
        "source_time_s": 12.5,
        "expected_context": context,
        "expected_sensor_revision": 7,
        "controller_fence": fence,
    }
    parsed = _room_command(lease)
    assert parsed == lease
    parsed["controller_fence"]["term"] = 99
    assert fence["term"] == 8
    invalid = [
        {key: value for key, value in lease.items() if key != "controller_fence"},
        {**lease, "controller_fence": None},
        {**lease, "accel": [0.5, 0.5, 0]},
        {"op": "set_command", "accel": [0, 0, 0], "angular_accel": [0, 0, 0]},
        {"op": "reset"},
        {"op": "play", "rate": 1},
        {"op": "play", "rate": 5, "expected_context": context},
        {"op": "play", "rate": True, "expected_context": context},
        {"op": "play", "rate": 1.0, "expected_context": context},
        {"op": "play", "rate": 1, "expected_context": {**context, "running": 0}},
        {"op": "play", "rate": 1, "expected_context": context, "extra": None},
    ]
    value = _credentials(url="https://127.0.0.1:1")
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    for command in invalid:
        with pytest.raises(ValueError):
            client.command(command)
    assert _room_command({"op": "play", "rate": 1, "expected_context": context})["rate"] == 1


def test_native_ownership_and_original_context_are_copied_without_changing_source_time():
    context, _ = _checked_metadata()
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
    assert _room_command(claim) == claim
    assert _room_command(release) == release
    parsed = _room_call({"method": "command", "command": claim})
    parsed["command"]["expected_context"]["trial_revision"] = 10
    assert context["trial_revision"] == 3
    assert parsed["command"]["source_time_s"] == 12.5


def test_rpc_envelopes_use_fresh_uuids_and_bounded_integer_wall_deadlines():
    start = int(time.time() * 1000)
    envelopes = [_rpc_envelope({"method": "snapshot"}, 0.125) for _ in range(16)]
    end = int(time.time() * 1000)
    assert len({item["id"] for item in envelopes}) == len(envelopes)
    for item in envelopes:
        assert str(uuid.UUID(item["id"])) == item["id"]
        assert set(item) == {"id", "command"}
        call = item["command"]
        assert call["version"] == 1 and call["method"] == "snapshot"
        assert type(call["deadline_ms"]) is int
        assert start + 125 <= call["deadline_ms"] <= end + 125
    longest = _rpc_envelope({"method": "snapshot"}, 30)
    assert longest["command"]["deadline_ms"] <= int(time.time() * 1000) + MAX_TIMEOUT_MS
    assert longest["command"]["deadline_ms"] <= int(time.time() * 1000) + MAX_ADMISSION_MS


def test_result_parser_accepts_only_an_exact_matching_result_envelope():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    request_id = str(uuid.uuid4())
    # Envelope validation only. This ACK is not evidence of an engine invocation.
    result = {"type": "result", "id": request_id, "ok": True, "value": {"ok": True}}
    assert client._result(result, request_id) == {"ok": True}
    invalid = [
        None,
        [],
        {**result, "id": str(uuid.uuid4())},
        {**result, "type": "state"},
        {**result, "ok": 1},
        {**result, "value": []},
        {**result, "extra": True},
        {**result, "ok": False},
        {"type": "result", "id": request_id, "ok": False, "error": 1},
        {"type": "result", "id": request_id, "ok": False, "error": "Rejected", "code": None},
    ]
    for envelope in invalid:
        with pytest.raises(RoomApiError, match="outcome may be unknown"):
            client._result(envelope, request_id)


def test_only_matching_native_known_unapplied_results_are_classified_and_never_echo_text():
    value = _credentials()
    client = RoomSimulation(value["url"], value["room_id"], value["token"])
    request_id = str(uuid.uuid4())
    result = {
        "type": "result",
        "id": request_id,
        "ok": False,
        "error": f"Do not print {value['token']}",
    }
    for code, expected in (
        ("stale_context", StaleContextError),
        ("command_expired", CommandExpiredError),
        ("local_busy", RoomApiError),
        ("page_mismatch", RoomApiError),
        ("unknown", RoomApiError),
    ):
        with pytest.raises(expected) as rejected:
            client._result({**result, "code": code}, request_id)
        assert value["token"] not in str(rejected.value)
        assert "Do not print" not in str(rejected.value)
    with pytest.raises(RoomApiError):
        client._result({**result, "id": str(uuid.uuid4()), "code": "stale_context"}, request_id)


def test_real_unavailable_http_never_becomes_a_saved_state_snapshot_or_a_retry():
    with _unavailable_origin("http") as origin:
        value = _credentials(url=origin)
        client = RoomSimulation(
            origin, value["room_id"], value["token"], allow_loopback=True, timeout_s=0.2
        )
        with pytest.raises(RoomApiError, match="outcome may be unknown") as rejected:
            client.snapshot()
        assert value["token"] not in str(rejected.value)


def test_controller_pin_cannot_be_added_after_even_an_unavailable_first_rpc():
    with _unavailable_origin("http") as origin:
        value = _credentials(url=origin)
        client = RoomSimulation(
            origin, value["room_id"], value["token"], allow_loopback=True, timeout_s=0.2
        )
        with pytest.raises(RoomApiError):
            client.snapshot()
        with pytest.raises(RoomApiError, match="before the first room RPC"):
            client.bind_controller_page(str(uuid.uuid4()))


def test_real_owned_room_http_rejects_wrong_job_owner_before_no_owner_dispatch(actual_room):
    _, _, _, path = actual_room
    client = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    client.bind_controller_page(str(uuid.uuid4()))
    with pytest.raises(RoomPageMismatchError) as mismatch:
        client.snapshot()
    assert mismatch.value.code == "page_mismatch" and mismatch.value.http_status == 409
    assert not isinstance(mismatch.value, StaleContextError)
    assert client._token not in str(mismatch.value)
    legacy = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    with pytest.raises(RoomApiError) as unavailable:
        legacy.snapshot()
    assert unavailable.value.code == "owner_unavailable"


def test_real_owned_room_job_stamp_pins_the_actual_host_before_first_snapshot(room_owner):
    from wsorb.checkpoint import CheckpointOptions
    from wsorb.controller_service import ControllerQueue, JobMonitor, execute_job
    from wsorb.local_api import control_context

    _, _, _, path, _ = room_owner
    source = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    initial = source.snapshot()
    queue, worker_id = ControllerQueue(source), str(uuid.uuid4())
    assert queue.poll(worker_id).job is None
    submitted = queue._send(
        {
            "action": "submit",
            "profile_id": "coast",
            "seed": 100,
            "duration_s": 0.2,
            "fresh": True,
            "expected_context": control_context(initial),
        }
    )
    owner = submitted["active"]["owner_page_id"]
    assert str(uuid.UUID(owner)) == owner
    claimed = queue.poll(worker_id)
    assert claimed.job.owner_page_id == owner
    pinned = RoomSimulation.from_file(path, allow_loopback=True, timeout_s=2)
    pinned.bind_controller_page(owner)
    actual = pinned.snapshot()
    assert control_context(actual) == control_context(initial)
    assert actual["observation"]["time_s"] == 0
    assert actual["observation"]["control_authority"] == initial["observation"]["control_authority"]
    summary = execute_job(
        source,
        claimed.job,
        CheckpointOptions(room_file=path, run=True, wall_budget_s=15),
        JobMonitor(claimed.job, threading.Event()),
    )
    assert summary["state"] == "duration_limit"
    assert summary["release_state"] == "acknowledged"
    assert summary["world_metadata_unchanged"] is True
    assert summary["measurement"]["time_s"] >= 0.2
    completed = queue.complete(worker_id, claimed.job.id, summary)
    assert completed["active"] is None and len(completed["results"]) == 1
    assert pinned.snapshot()["observation"]["control_authority"]["status"] == "revoked"


def test_output_is_reserved_private_exclusive_and_cleanup_removes_only_its_own_file(tmp_path):
    path = tmp_path / "room.json"
    with pytest.raises(RuntimeError), _reserve_descriptor(path) as (target, _):
        assert path.is_file()
        assert stat.S_IMODE(path.lstat().st_mode) == 0o600
        target.write("private")
        raise RuntimeError("Pure reservation failure")
    assert not path.exists()

    moved = tmp_path / "original.json"
    with pytest.raises(RuntimeError), _reserve_descriptor(path):
        path.rename(moved)
        path.write_text("another writer's file")
        path.chmod(0o600)
        raise RuntimeError("Pure replacement check")
    assert path.read_text() == "another writer's file"
    assert moved.is_file()


def test_create_failure_on_genuine_unavailable_https_removes_its_empty_reservation(tmp_path):
    path = tmp_path / "room.json"
    with (
        _unavailable_origin() as origin,
        pytest.raises(RoomApiError, match="transport unavailable"),
    ):
        create_room(path, url=origin, timeout_s=0.2)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_create_never_overwrites_existing_files_or_symlinks_before_network(tmp_path):
    path = tmp_path / "existing.json"
    path.write_text("owner data")
    path.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    broken = tmp_path / "broken.json"
    broken.symlink_to(tmp_path / "missing.json")
    with _unavailable_origin() as origin:
        for reserved in (path, link, broken):
            with pytest.raises(RoomApiError, match="never overwritten"):
                create_room(reserved, url=origin, timeout_s=0.2)
    assert path.read_text() == "owner data"
    assert link.is_symlink() and broken.is_symlink()


def test_create_and_production_cli_never_offer_loopback_or_implicit_output(tmp_path, capsys):
    path = tmp_path / "room.json"
    args = parse_args(["create", "--connection-file", str(path)])
    assert args.connection_file == path and args.url == DEFAULT_ORIGIN
    with pytest.raises(RoomApiError):
        parse_args(["create"])
    assert main(["create", "--connection-file", str(path), "--url", "http://127.0.0.1:8787"]) == 1
    assert not path.exists()
    token = secrets.token_urlsafe(32)
    assert main(["create", "--connection-file", str(path), "--allow-loopback", token]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert token not in captured.err
    assert "key=" not in captured.err
