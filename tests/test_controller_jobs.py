"""Real local/workerd controller control-plane checks, without executing a flight."""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from test_local_api import headless_server as headless_server
from test_local_api import local_server as local_server
from test_room_integration import actual_room as actual_room
from test_room_integration import room_owner as room_owner
from test_room_integration import room_worker as room_worker
from wsorb.local_api import control_context
from wsorb.room_api import RoomSimulation

ROOT = Path(__file__).resolve().parents[1]
EXECUTION_KIND = "asynchronous_fenced_leased_API"


@pytest.fixture(params=["native_headless", "room"])
def owned_backend(request, monkeypatch):
    if request.param == "native_headless":
        return request.getfixturevalue("headless_server"), "/__local/controllers"
    parameters = request.node.callspec.params
    if "world_id" in parameters:
        # The test-owned native peer prepares its own world before connecting.
        # Hosted controller RPC remains unable to reset/configure any owner.
        monkeypatch.setenv(
            "WSORB_ROOM_INITIAL_FLIGHT",
            json.dumps(
                {
                    "profileId": "coast",
                    "worldId": parameters["world_id"],
                    "seed": parameters["seed"],
                    "duration": 0.2,
                }
            ),
        )
    _, _, room, path, _ = request.getfixturevalue("room_owner")
    return RoomSimulation.from_file(
        path, allow_loopback=True
    ), f"/api/rooms/{room['id']}/controllers"


def _request(backend, body=None, *, headers=None, method=None, route=None):
    client, controllers = backend
    call = Request(
        client.url + (controllers if route is None else route),
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {client._token}",
            "Content-Type": "application/json",
            **(headers or {}),
        },
        method=method,
    )
    try:
        response = build_opener(ProxyHandler({})).open(call, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read()
        value = (
            json.loads(raw)
            if response.headers.get("Content-Type", "").startswith("application/json")
            else {"error": raw.decode()}
        )
        assert client._token not in json.dumps(value)
        return response.code, value, response.headers


def _get(backend):
    status, value, headers = _request(backend)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    assert set(value) == {"catalog", "service", "active", "results"}
    return value


def _poll(backend, worker_id):
    return _request(backend, {"action": "poll", "worker_id": worker_id})


def _submit(snapshot, *, profile="baseline", duration=0.2, world_id=None):
    return {
        "action": "submit",
        "profile_id": profile,
        "seed": snapshot["observation"]["seed"],
        "duration_s": duration,
        "fresh": True,
        "expected_context": control_context(snapshot),
        **({"world_id": world_id} if world_id is not None else {}),
    }


def _unchanged(client, before):
    after = client.snapshot()
    assert after["renderer"]["started"] is after["renderer"]["ready"] is False
    assert after["runtime"]["running"] is False
    assert after["observation"]["time_s"] == before["observation"]["time_s"] == 0
    for name in (
        "seed",
        "trial_revision",
        "tuning_revision",
        "manual_interventions",
        "control_authority",
        "experiment_settings",
        "applied_physics",
    ):
        assert after["observation"][name] == before["observation"][name]
    return after


def _unexecuted_summary(snapshot, *, state="stopped", error_code=None):
    """An actual paused measurement for a queue job cancelled before execution."""
    observation = snapshot["observation"]
    mission = observation["mission"]
    assert observation["time_s"] == 0 and snapshot["runtime"]["running"] is False
    assert mission["status"] == "running"
    return {
        "state": state,
        "native_success": False,
        "measurement": {
            "time_s": observation["time_s"],
            "range_m": mission["target_distance_m"],
            "relative_speed_mps": mission["relative_speed_mps"],
            "damage_j": mission["damage"],
            "fuel_used_kg": mission["fuel_used_kg"],
            "status": mission["status"],
            "score": mission["score"],
            "score_breakdown": mission.get("score_breakdown"),
        },
        "release_state": "not_claimed",
        "world_metadata_unchanged": True,
        "error_code": error_code,
        "execution_kind": EXECUTION_KIND,
        "engine_sha256": snapshot["engine_build"]["sha256"],
        "elapsed_s": 0,
        "analytics": None,
    }


def _complete(backend, worker_id, job_id, summary):
    return _request(
        backend,
        {"action": "complete", "worker_id": worker_id, "job_id": job_id, "result": summary},
    )


def test_catalog_and_service_view_are_authenticated_and_do_not_start_native_control(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    view = _get(owned_backend)
    assert view["catalog"] == json.loads((ROOT / "src/wsorb/controllers/catalog.json").read_text())
    assert {entry["owner"] for entry in view["catalog"]} == {
        "Tom",
        "Harry",
        "Bui",
        "WSO",
        "Ali",
        "Published",
    }
    assert view["service"] == {"online": False, "busy": False}
    assert view["active"] is None and view["results"] == []
    for headers, expected in (
        ({"Authorization": ""}, 401),
        ({"Authorization": f"Bearer {'x' * 43}"}, 401),
        ({"Origin": "https://other.invalid"}, 403),
        ({"Origin": "null"}, 403),
    ):
        for body in (None, {"action": "poll", "worker_id": str(uuid.uuid4())}):
            assert _request(owned_backend, body, headers=headers)[0] == expected
    if not isinstance(client, RoomSimulation):
        for extra in ({"Host": "other.invalid"}, {"Sec-Fetch-Site": "same-site"}):
            assert _request(owned_backend, headers=extra)[0] == 403
    assert _request(owned_backend, method="DELETE")[0] == 405
    assert _request(owned_backend, _submit(before))[0] == 503
    assert _get(owned_backend) == view
    _unchanged(client, before)


def test_control_plane_polls_do_not_invent_an_absent_browser_or_room_owner(
    local_server, actual_room
):
    origin, _, room, path = actual_room
    assert origin.startswith("http://127.0.0.1:")
    room_client = RoomSimulation.from_file(path, allow_loopback=True)
    # Input metadata for a request that must be rejected, not a native receipt.
    context = {
        "trial_revision": 0,
        "tuning_revision": 0,
        "manual_interventions": 0,
        "seed": 100,
        "running": False,
    }
    for backend in (
        (local_server, "/__local/controllers"),
        (room_client, f"/api/rooms/{room['id']}/controllers"),
    ):
        worker = str(uuid.uuid4())
        assert _poll(backend, worker)[:2] == (200, {"job": None, "stop_requested": False})
        assert _get(backend)["service"] == {"online": True, "busy": False}
        status, failure, _ = _request(
            backend,
            {
                "action": "submit",
                "profile_id": "baseline",
                "seed": 100,
                "duration_s": 0.2,
                "fresh": True,
                "expected_context": context,
            },
            headers={"X-WSORB-Page": str(uuid.uuid4())},
        )
        assert status == 503 and "owner" in failure["error"]
        assert _get(backend)["active"] is None
    assert local_server.status()["connected"] is False


def test_submit_is_strict_and_never_mutates_native_world(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    worker = str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    valid = _submit(before)
    malformed = [None, [], {}, {"action": "eval", "code": "not executable"}]
    malformed.extend(
        {key: value for key, value in valid.items() if key != missing} for missing in valid
    )
    malformed.extend(
        {**valid, key: value}
        for key, value in (
            ("profile_id", "not-reviewed"),
            ("profile_id", ["baseline"]),
            ("seed", 101),
            ("seed", True),
            ("seed", -1),
            ("seed", 2**32),
            ("seed", 100.5),
            ("world_id", None),
            ("world_id", "unknown"),
            ("world_id", 1),
            ("fresh", False),
            ("fresh", 1),
            ("duration_s", 0),
            ("duration_s", 1800.001),
            ("duration_s", True),
            ("duration_s", "1"),
            ("duration_s", float("inf")),
            ("owner_page_id", str(uuid.uuid4())),
            ("id", str(uuid.uuid4())),
            ("settings", {}),
        )
    )
    malformed.extend(
        {**valid, "expected_context": changed}
        for changed in (
            None,
            {**valid["expected_context"], "running": True},
            {**valid["expected_context"], "running": 0},
            {**valid["expected_context"], "seed": 101},
            {**valid["expected_context"], "seed": True},
            {**valid["expected_context"], "trial_revision": -1},
            {**valid["expected_context"], "tuning_revision": 2**32},
            {**valid["expected_context"], "manual_interventions": 0.5},
            {**valid["expected_context"], "extra": 0},
        )
    )
    original = _get(owned_backend)
    for body in malformed:
        # JSON null must be sent as a POST rather than accidentally testing GET.
        if body is None:
            body = {"action": None}
        assert _request(owned_backend, body)[0] == 400
        assert _get(owned_backend) == original
    _unchanged(client, before)


def test_single_writer_queue_stop_and_matching_completion(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    state_route = (
        f"/api/rooms/{client.room_id}/state" if isinstance(client, RoomSimulation) else None
    )
    saved_room = _request(owned_backend, route=state_route)[1] if state_route else None
    worker, contender = str(uuid.uuid4()), str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    barrier = threading.Barrier(2)

    def submit(profile):
        barrier.wait(timeout=5)
        return _request(owned_backend, _submit(before, profile=profile))

    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(submit, ("baseline", "coast")))
    assert sorted(reply[0] for reply in replies) == [200, 409]
    view = next(reply[1] for reply in replies if reply[0] == 200)
    job = view["active"]
    assert job["status"] == "queued" and job["stop_requested"] is False
    assert str(uuid.UUID(job["id"])) == job["id"]
    assert type(job["created_at"]) is int and job["created_at"] > 0
    assert job["expected_context"] == control_context(before)
    assert view["service"] == {"online": True, "busy": True}
    if isinstance(client, RoomSimulation):
        assert str(uuid.UUID(job["owner_page_id"])) == job["owner_page_id"]
    else:
        assert job["owner_page_id"] is None
    assert _request(owned_backend, _submit(before))[0] == 409
    assert _poll(owned_backend, contender)[0] == 409
    assert _get(owned_backend) == view
    assert _request(owned_backend, {"action": "stop", "job_id": str(uuid.uuid4())})[0] == 409
    summary = _unexecuted_summary(before)
    assert _complete(owned_backend, worker, job["id"], summary)[0] == 409
    assert _request(owned_backend, {"action": "stop", "job_id": job["id"]})[0] == 200
    stopped = _get(owned_backend)["active"]
    assert stopped["id"] == job["id"] and stopped["stop_requested"] is True
    assert stopped["expected_context"] == job["expected_context"]
    status, assignment, _ = _poll(owned_backend, worker)
    assert status == 200 and assignment["stop_requested"] is True
    assert assignment["job"]["id"] == job["id"]
    assert assignment["job"]["status"] == "stopping"
    assert _complete(owned_backend, contender, job["id"], summary)[0] == 409
    assert _complete(owned_backend, worker, str(uuid.uuid4()), summary)[0] == 409
    status, completed, _ = _complete(owned_backend, worker, job["id"], summary)
    assert status == 200 and completed["active"] is None
    assert completed["service"] == {"online": True, "busy": False}
    assert completed["results"] == [
        {**summary, **{key: job[key] for key in ("id", "profile_id", "seed", "duration_s")}}
    ]
    assert completed["results"][0]["measurement"]["score"] is None
    assert completed["results"][0]["measurement"]["score_breakdown"] is None
    status, replayed, _ = _complete(owned_backend, worker, job["id"], summary)
    assert status == 200 and replayed["results"] == completed["results"]
    assert _complete(owned_backend, worker, job["id"], {**summary, "elapsed_s": 1})[0] == 409
    assert _poll(owned_backend, worker)[:2] == (200, {"job": None, "stop_requested": False})
    if state_route:
        assert _request(owned_backend, route=state_route)[1] == saved_room
    _unchanged(client, before)


def test_older_clients_can_complete_without_score_fields_and_do_not_invent_zero(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    worker = str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    status, view, _ = _request(owned_backend, _submit(before))
    assert status == 200
    job_id = view["active"]["id"]
    assert _request(owned_backend, {"action": "stop", "job_id": job_id})[0] == 200
    assert _poll(owned_backend, worker)[1]["stop_requested"] is True
    summary = _unexecuted_summary(before)
    del summary["measurement"]["score"]
    del summary["measurement"]["score_breakdown"]
    status, completed, _ = _complete(owned_backend, worker, job_id, summary)
    assert status == 200
    measurement = completed["results"][-1]["measurement"]
    assert measurement == {**summary["measurement"], "score": None, "score_breakdown": None}
    assert "score" not in summary["measurement"]
    assert "world_id" not in completed["results"][-1]
    _unchanged(client, before)


@pytest.mark.parametrize(
    "world_id,seed", [("official", 0), ("team", 101), ("across_phobos", 0xFFFFFFFF)]
)
def test_explicit_world_and_uint32_seed_survive_queue_completion_without_replay(
    owned_backend, world_id, seed
):
    from wsorb.checkpoint import world_metadata
    from wsorb.controller_worlds import controller_world, world_matches

    client, _ = owned_backend
    world = controller_world(world_id)
    before = (
        client.snapshot()
        if isinstance(client, RoomSimulation)
        else client.configure_flight(world.settings, world.preset, seed)
    )
    assert world_matches(before, world_id, seed), {
        "world_id": world_id,
        "seed": seed,
        "expected_settings": world.settings,
        "native_world": world_metadata(before),
    }
    worker = str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    request = _submit(before, profile="official_basic_beeline", world_id=world_id)
    for invalid in (
        {**request, "seed": (seed + 1) % 2**32},
        {
            **request,
            "expected_context": {**request["expected_context"], "seed": (seed + 1) % 2**32},
        },
    ):
        assert _request(owned_backend, invalid)[0] == 400
    status, submitted, _ = _request(owned_backend, request)
    assert status == 200
    job = submitted["active"]
    assert job["world_id"] == world_id and job["seed"] == seed
    assigned = _poll(owned_backend, worker)[1]["job"]
    assert assigned["world_id"] == world_id
    assert assigned["expected_context"]["seed"] == seed
    assert _request(owned_backend, {"action": "stop", "job_id": job["id"]})[0] == 200
    summary = {**_unexecuted_summary(before), "world_id": world_id}
    wrong_world = "team" if world_id != "team" else "official"
    assert (
        _complete(owned_backend, worker, job["id"], {**summary, "world_id": wrong_world})[0] == 400
    )
    assert (
        _complete(owned_backend, worker, job["id"], {**summary, "seed": (seed + 1) % 2**32})[0]
        == 400
    )
    status, completed, _ = _complete(owned_backend, worker, job["id"], summary)
    assert status == 200
    result = completed["results"][-1]
    assert result["world_id"] == world_id and result["seed"] == seed
    assert result["measurement"]["time_s"] == 0 and result["release_state"] == "not_claimed"
    assert _poll(owned_backend, worker)[:2] == (200, {"job": None, "stop_requested": False})
    assert _complete(owned_backend, worker, job["id"], summary)[0] == 200
    _unchanged(client, before)


def test_poll_stop_and_complete_require_exact_envelopes_and_canonical_ids(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    worker, other = str(uuid.uuid4()), str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    status, view, _ = _request(owned_backend, _submit(before))
    assert status == 200
    job_id = view["active"]["id"]
    assert _poll(owned_backend, worker)[0] == 200
    valid_complete = {
        "action": "complete",
        "worker_id": worker,
        "job_id": job_id,
        "result": _unexecuted_summary(before),
    }
    invalid = [
        {"action": "poll"},
        {"action": "poll", "worker_id": worker, "extra": True},
        {"action": "stop"},
        {"action": "stop", "job_id": job_id, "worker_id": worker},
        {**valid_complete, "extra": True},
    ]
    for malformed in ("", "not-a-uuid", worker.upper(), f"{worker}, {other}", [], True):
        invalid.append({"action": "poll", "worker_id": malformed})
        invalid.append({"action": "stop", "job_id": malformed})
        invalid.append({**valid_complete, "worker_id": malformed})
        invalid.append({**valid_complete, "job_id": malformed})
    view = _get(owned_backend)
    for body in invalid:
        assert _request(owned_backend, body)[0] == 400
        assert _get(owned_backend) == view
    _unchanged(client, before)


def test_result_summary_rejects_raw_text_unknown_fields_and_nonfinite_numbers(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    worker = str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    status, view, _ = _request(owned_backend, _submit(before))
    assert status == 200
    job = view["active"]
    assert _poll(owned_backend, worker)[0] == 200
    valid = _unexecuted_summary(before)
    canary = "private-result-canary.invalid/secret"
    invalid = [
        {**valid, "state": canary},
        {**valid, "release_state": canary},
        {**valid, "error_code": canary},
        {**valid, "error": canary},
        {**valid, "raw": {"url": canary}},
        {**valid, "execution_kind": "mock"},
        {**valid, "native_success": 1},
        {**valid, "world_metadata_unchanged": 1},
        {**valid, "engine_sha256": canary},
        {**valid, "elapsed_s": float("inf")},
        {**valid, "elapsed_s": True},
        {**valid, "id": str(uuid.uuid4())},
        {**valid, "profile_id": "coast"},
        {**valid, "seed": 101},
        {**valid, "duration_s": 1800},
    ]
    invalid.extend(
        {**valid, "measurement": {**valid["measurement"], key: value}}
        for key, value in (
            ("time_s", float("inf")),
            ("range_m", True),
            ("relative_speed_mps", "1"),
            ("damage_j", {}),
            ("status", canary),
            ("score", True),
            ("score", "0"),
            ("score", float("nan")),
            ("score", float("inf")),
            ("score", -float("inf")),
            ("score_label", canary),
            ("score_breakdown", canary),
        )
    )
    # Rejected schema data only. None of these inputs claims a native score.
    terms = {
        "formula": "practice_normalized_time_damage_fuel_v1",
        "progress_basis": "time_ratio",
        "progress_points": -12.5,
        "collision_penalty_points": 5,
        "fuel_penalty_points": 26000.25,
        "offset_points": -100,
        "total": -26000.125,
        "nominal_time_s": 1800,
        "nominal_fuel_kg": 100,
        "impact_threshold_j": 1,
    }
    invalid_terms = [
        {**terms, "formula": canary},
        {**terms, "progress_basis": canary},
        {**terms, "url": canary},
        {**terms, "score_label": canary},
        {**terms, "collision_penalty_points": -1},
        {**terms, "fuel_penalty_points": -1},
        {**terms, "total": -26000},
    ]
    for name in terms:
        invalid_terms.append({key: value for key, value in terms.items() if key != name})
        if name not in {"formula", "progress_basis"}:
            invalid_terms.extend(
                {**terms, name: value} for value in (True, None, "1", float("nan"), float("inf"))
            )
    for name in ("nominal_time_s", "nominal_fuel_kg", "impact_threshold_j"):
        invalid_terms.extend({**terms, name: value} for value in (0, -1))
    invalid.extend(
        {
            **valid,
            "measurement": {**valid["measurement"], "score": -26000.125, "score_breakdown": value},
        }
        for value in invalid_terms
    )
    invalid.append({**valid, "measurement": {**valid["measurement"], "score_breakdown": terms}})
    original = _get(owned_backend)
    for summary in invalid:
        status, value, _ = _complete(owned_backend, worker, job["id"], summary)
        assert status == 400 and canary not in json.dumps(value)
        assert _get(owned_backend) == original
    after = _unchanged(client, before)
    assert _complete(owned_backend, worker, job["id"], _unexecuted_summary(after))[0] == 200


def test_results_are_bounded_and_all_catalog_profiles_use_the_same_queue(owned_backend):
    client, _ = owned_backend
    before = client.snapshot()
    worker = str(uuid.uuid4())
    assert _poll(owned_backend, worker)[0] == 200
    catalog = _get(owned_backend)["catalog"]
    completed_ids = []
    for index in range(34):
        assert _poll(owned_backend, worker)[0] == 200
        status, view, _ = _request(
            owned_backend, _submit(before, profile=catalog[index % len(catalog)]["id"])
        )
        assert status == 200
        job_id = view["active"]["id"]
        completed_ids.append(job_id)
        assert _request(owned_backend, {"action": "stop", "job_id": job_id})[0] == 200
        assert _poll(owned_backend, worker)[1]["stop_requested"] is True
        assert _complete(owned_backend, worker, job_id, _unexecuted_summary(before))[0] == 200
    view = _get(owned_backend)
    assert [result["id"] for result in view["results"]] == completed_ids[-32:]
    assert view["active"] is None and view["service"]["busy"] is False
    _unchanged(client, before)


def test_lost_service_heartbeat_never_reassigns_or_replays_an_active_job(
    headless_server, room_owner
):
    _, _, room, path, _ = room_owner
    backends = [
        (headless_server, "/__local/controllers"),
        (
            RoomSimulation.from_file(path, allow_loopback=True),
            f"/api/rooms/{room['id']}/controllers",
        ),
    ]
    assignments = []
    for backend in backends:
        before = backend[0].snapshot()
        worker = str(uuid.uuid4())
        assert _poll(backend, worker)[0] == 200
        status, view, _ = _request(backend, _submit(before))
        assert status == 200
        job_id = view["active"]["id"]
        assert _poll(backend, worker)[1]["job"]["id"] == job_id
        assignments.append((backend, before, worker, job_id))
    # Real heartbeat expiry, not a substituted clock or fake engine readiness.
    time.sleep(15.1)
    for backend, before, worker, job_id in assignments:
        view = _get(backend)
        assert view["service"] == {"online": False, "busy": True}
        assert view["active"]["id"] == job_id and view["active"]["status"] == "stalled"
        assert _poll(backend, str(uuid.uuid4()))[0] == 409
        assert _request(backend, _submit(before))[0] == 409
        status, abandoned, _ = _request(backend, {"action": "stop", "job_id": job_id})
        assert status == 200 and abandoned["active"] is None
        assert abandoned["results"][-1]["error_code"] == "service_stalled"
        assert abandoned["results"][-1]["measurement"]["range_m"] is None
        assert abandoned["results"][-1]["measurement"]["score"] is None
        assert abandoned["results"][-1]["measurement"]["score_breakdown"] is None
        assert abandoned["results"][-1]["native_success"] is False
        _unchanged(backend[0], before)
        summary = _unexecuted_summary(before, state="error", error_code="service_stalled")
        assert _complete(backend, worker, job_id, summary)[0] == 409
        assert _poll(backend, worker)[:2] == (200, {"job": None, "stop_requested": False})
        assert _get(backend)["active"] is None


def test_controller_body_bounds_are_enforced_before_loading_unbounded_json(owned_backend):
    client, route = owned_backend
    address = urlsplit(client.url)
    connection = HTTPConnection(address.hostname, address.port, timeout=5)
    try:
        connection.putrequest("POST", route)
        connection.putheader("Authorization", f"Bearer {client._token}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(64 * 1024 + 1))
        connection.putheader("Connection", "close")
        # workerd's local HTTP adapter waits for the declared body before
        # delivering the Request; send it rather than testing that adapter's wait.
        connection.endheaders(b" " * (64 * 1024 + 1))
        with connection.getresponse() as response:
            assert response.status == 413
            assert client._token not in response.read().decode()
    finally:
        connection.close()
    assert _get(owned_backend)["service"] == {"online": False, "busy": False}


def test_hosted_target_page_is_checked_before_dispatch_or_pending_insertion(room_owner):
    _, _, room, path, _ = room_owner
    client = RoomSimulation.from_file(path, allow_loopback=True)
    backend = client, f"/api/rooms/{room['id']}/controllers"
    before = client.snapshot()
    worker = str(uuid.uuid4())
    assert _poll(backend, worker)[0] == 200
    status, view, _ = _request(backend, _submit(before))
    assert status == 200
    owner = view["active"]["owner_page_id"]
    route = f"/api/rooms/{room['id']}/commands"
    call = {
        "id": "pin_check",
        "command": {
            "version": 1,
            "deadline_ms": int(time.time() * 1000) + 4000,
            "method": "snapshot",
        },
    }
    for invalid in ("", "not-a-uuid", owner.upper(), f"{owner}, {owner}"):
        assert (
            _request(backend, call, route=route, headers={"X-WSORB-Target-Page": invalid})[0] == 400
        )
    old_owner = str(uuid.uuid4())
    status, failure, headers = _request(
        backend, call, route=route, headers={"X-WSORB-Target-Page": old_owner}
    )
    assert status == 409
    assert failure == {"error": "Target room owner changed", "code": "page_mismatch"}
    assert headers.get("X-WSORB-Page") is None
    # Reusing the same request ID proves the refused pin did not insert pending.
    status, result, headers = _request(
        backend, call, route=route, headers={"X-WSORB-Target-Page": owner}
    )
    assert status == 200 and result["ok"] is True and result["id"] == call["id"]
    assert headers["X-WSORB-Page"] == owner
    assert control_context(result["value"]) == control_context(before)
    _unchanged(client, before)
