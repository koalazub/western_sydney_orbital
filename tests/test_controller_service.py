"""Pure job/consent/request checks. No replacement network, SDK or runtime."""

from __future__ import annotations

import ast
import inspect
import json
import math
import secrets
import shutil
import signal
import socket
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from urllib.request import ProxyHandler

import numpy as np
import pytest

from wsorb import cli
from wsorb.checkpoint import CheckpointError, CheckpointOptions
from wsorb.controller_service import (
    EXECUTION_KIND,
    MAX_QUEUE_RESPONSE_BYTES,
    PROFILE_IDS,
    ControllerQueue,
    ControllerServiceError,
    JobMonitor,
    PollReply,
    SeenJobs,
    _heartbeat,
    _interrupt_stops,
    _job_client,
    _queue_view,
    _validate_job_snapshot,
    completion_summary,
    execute_job,
    main,
    parse_args,
    parse_job,
    parse_poll,
    serve,
    validate_job_context,
    validate_summary,
)
from wsorb.local_api import LocalSimulation, _NoRedirects
from wsorb.room_api import RoomSimulation


def _job_metadata(**changes):
    # This is a queue document only, never a fabricated native observation.
    return {
        "id": str(uuid.uuid4()),
        "profile_id": "baseline",
        "seed": 100,
        "duration_s": 1800,
        "expected_context": {
            "trial_revision": 2,
            "tuning_revision": 1,
            "manual_interventions": 0,
            "seed": 100,
            "running": False,
        },
        "owner_page_id": str(uuid.uuid4()),
        "status": "running",
        "stop_requested": False,
        **changes,
    }


def _score_terms(**changes):
    """Pure schema data, not a claimed native observation or SDK result."""
    return {
        "formula": "practice_normalized_time_damage_fuel_v1",
        "progress_basis": "time_ratio",
        "progress_points": -12.5,
        "collision_penalty_points": 5.0,
        "fuel_penalty_points": 26000.25,
        "offset_points": -100.0,
        "total": -26000.125,
        "nominal_time_s": 1800.0,
        "nominal_fuel_kg": 100.0,
        "impact_threshold_j": 1.0,
        **changes,
    }


def test_jobs_are_exact_bounded_metadata_not_arbitrary_python_or_commands():
    metadata = _job_metadata()
    job = parse_job(metadata)
    assert job.id == metadata["id"]
    assert job.profile_id == "baseline"
    metadata["expected_context"]["trial_revision"] += 1
    assert job.expected_context["trial_revision"] == 2
    assert parse_job(_job_metadata(created_at=1000)).profile_id == "baseline"
    for changes in (
        {"profile_id": "examples/tom/Controller.py"},
        {"profile_id": "Tom"},  # Friendly names belong at the CLI, never in jobs.
        {"profile_id": "wso_official_profile"},  # Comparison aliases resolve at the UI boundary.
        {"profile_id": {"script": "print('no')"}},
        {"id": "not-a-uuid"},
        {"id": str(uuid.UUID(int=0))},
        {"owner_page_id": "../another-page"},
        {"owner_page_id": str(uuid.uuid4()).upper()},
        {"seed": True},
        {"seed": 101},  # A different seed without matching consent is still invalid.
        {"seed": -1},
        {"seed": 2**32},
        {"seed": 100.0},
        {"world_id": None},
        {"world_id": "unreviewed"},
        {"duration_s": 1800.001},
        {"duration_s": True},
        {"duration_s": 0},
        {"duration_s": math.inf},
        {"duration_s": 2**2048},
        {"stop_requested": "false"},
        {"status": "complete"},
        {"created_at": math.inf},
        {"script": "never accepted"},
    ):
        with pytest.raises(ControllerServiceError):
            parse_job(_job_metadata(**changes))
    for context in (
        {**job.expected_context, "running": True},
        {**job.expected_context, "seed": 101},
        {**job.expected_context, "trial_revision": True},
        {key: value for key, value in job.expected_context.items() if key != "running"},
    ):
        with pytest.raises(ControllerServiceError):
            parse_job(_job_metadata(expected_context=context))


def test_all_fixed_ids_are_accepted_without_loading_example_scripts():
    from wsorb.controllers import PROFILE_IDS as catalog_ids

    assert len(PROFILE_IDS) == 9
    assert catalog_ids == PROFILE_IDS
    for profile in PROFILE_IDS:
        assert parse_job(_job_metadata(profile_id=profile)).profile_id == profile


@pytest.mark.parametrize("seed", [0, 100, 101, 0xFFFFFFFF])
@pytest.mark.parametrize("world_id", [None, "official", "team", "across_phobos"])
def test_uint32_seed_and_optional_world_are_explicit_consistent_job_identity(seed, world_id):
    metadata = _job_metadata(seed=seed)
    metadata["expected_context"]["seed"] = seed
    if world_id is not None:
        metadata["world_id"] = world_id
    job = parse_job(metadata)
    assert job.seed == job.expected_context["seed"] == seed
    assert job.world_id == world_id
    assert replace(
        job, world_id="team" if world_id != "team" else "official"
    ).execution_identity != (job.execution_identity)


def test_poll_envelope_requires_real_boolean_stop_and_exact_fields():
    assert parse_poll({"job": None, "stop_requested": False}) == PollReply(None, False)
    for invalid in (
        None,
        [],
        {},
        {"job": None, "stop_requested": 0},
        {"job": None, "stop_requested": False, "command": {"op": "reset"}},
    ):
        with pytest.raises(ControllerServiceError):
            parse_poll(invalid)


def test_seen_job_ids_are_consumed_before_execution_and_never_replayed():
    seen = SeenJobs()
    job = parse_job(_job_metadata())
    assert seen.take(PollReply(None, False)) is None
    assert seen.take(PollReply(job, False)) is job
    assert seen.take(PollReply(job, False)) is None
    assert seen.take(PollReply(replace(job, status="stopping", stop_requested=True), True)) is None
    assert seen.take(PollReply(replace(job, profile_id="coast"), False)) is None
    assert seen.take(PollReply(replace(job, world_id="official"), False)) is None
    next_job = replace(job, id=str(uuid.uuid4()))
    assert seen.take(PollReply(next_job, False)) is next_job


def test_stop_callback_is_sticky_and_service_interrupt_also_cancels():
    job, stopped = parse_job(_job_metadata()), threading.Event()
    monitor = JobMonitor(job, stopped)
    callback = monitor.cancelled
    assert callback() is False
    monitor.observe(PollReply(job, True))
    assert callback() is True
    monitor.observe(PollReply(job, False))
    assert callback() is True
    assert monitor.heartbeat_lost is False
    other = JobMonitor(job, stopped)
    assert other.cancelled() is False
    stopped.set()
    assert other.cancelled() is True


def test_missing_changed_or_failed_heartbeat_cancels_without_adopting_new_consent():
    job = parse_job(_job_metadata())
    for reply in (
        PollReply(None, False),
        PollReply(replace(job, id=str(uuid.uuid4())), False),
        PollReply(replace(job, duration_s=1), False),
        PollReply(replace(job, world_id="official"), False),
        PollReply(replace(job, owner_page_id=str(uuid.uuid4())), False),
        PollReply(
            replace(job, expected_context={**job.expected_context, "trial_revision": 3}), False
        ),
    ):
        monitor = JobMonitor(job, threading.Event())
        monitor.observe(reply)
        assert monitor.cancelled() is True and monitor.heartbeat_lost is True
    monitor = JobMonitor(job, threading.Event())
    monitor.fail_closed()
    assert monitor.cancelled() is True and monitor.heartbeat_lost is True


def test_sigint_requests_event_cancellation_and_restores_the_previous_handler():
    stopped = threading.Event()
    previous = signal.getsignal(signal.SIGINT)
    with _interrupt_stops(stopped):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        assert stopped.is_set()
    assert signal.getsignal(signal.SIGINT) is previous


def test_the_whole_original_context_and_fresh_paused_time_zero_are_required():
    job = parse_job(_job_metadata())
    validate_job_context(job, job.expected_context, time_s=0)
    for name in ("trial_revision", "tuning_revision", "manual_interventions", "seed", "running"):
        context = dict(job.expected_context)
        context[name] = True if name == "running" else context[name] + 1
        with pytest.raises(ControllerServiceError, match="context changed"):
            validate_job_context(job, context, time_s=0)
    for time_s in (True, None, -0.1, 0.1, math.nan, math.inf):
        with pytest.raises(ControllerServiceError, match="fresh paused"):
            validate_job_context(job, job.expected_context, time_s=time_s)


def test_job_snapshot_rechecks_reviewed_world_and_all_original_claim_consent():
    from test_checkpoint import _world_receipt

    for world_id in ("official", "team", "across_phobos"):
        metadata = _job_metadata(seed=101, world_id=world_id)
        metadata["expected_context"]["seed"] = 101
        job = parse_job(metadata)
        _validate_job_snapshot(job, _world_receipt(world_id, 101))
        for other in ("official", "team", "across_phobos"):
            if other != world_id:
                with pytest.raises(CheckpointError) as changed:
                    _validate_job_snapshot(job, _world_receipt(other, 101))
                assert changed.value.code == "comparison_mismatch"
        with pytest.raises(ControllerServiceError, match="context changed"):
            _validate_job_snapshot(job, _world_receipt(world_id, 100))
        changed = _world_receipt(world_id, 101)
        changed["observation"]["control_authority"].update(
            status="active", run_id="existing", controller_id="existing", expires_at_sim_time_s=0.75
        )
        with pytest.raises(ControllerServiceError, match="no takeover"):
            _validate_job_snapshot(job, changed)
    legacy = parse_job(_job_metadata())
    with pytest.raises(CheckpointError) as unchanged_legacy_guard:
        _validate_job_snapshot(legacy, _world_receipt("official"))
    assert unchanged_legacy_guard.value.code == "stock_world_required"


def test_native_omitted_directed_distribution_is_not_a_different_world_or_seed():
    from test_checkpoint import _world_receipt
    from wsorb.controller_worlds import world_matches

    receipt = _world_receipt("team", 0)
    del receipt["observation"]["experiment_settings"]["debris"]["distribution"]
    assert world_matches(receipt, "team", 0)
    assert not world_matches(receipt, "team", 1)
    assert not world_matches(receipt, "official", 0)
    assert not world_matches(receipt, "across_phobos", 0)
    metadata = _job_metadata(seed=0, world_id="team")
    metadata["expected_context"]["seed"] = 0
    _validate_job_snapshot(parse_job(metadata), receipt)
    for field, value in (
        ("speed_mps", 2.0),
        ("stream_axis", "y"),
        ("stream_sign", -1),
        ("count", 499),
        ("distribution", None),
        ("distribution", {"kind": "transit_stream"}),
        ("trajectory_adjustment", "toward_lander"),
    ):
        changed = deepcopy(receipt)
        changed["observation"]["experiment_settings"]["debris"][field] = value
        assert not world_matches(changed, "team", 0), field
    missing_required = deepcopy(receipt)
    del missing_required["observation"]["experiment_settings"]["debris"]["speed_mps"]
    assert not world_matches(missing_required, "team", 0)


def test_completion_summary_is_allowlisted_finite_and_cannot_disclose_credentials():
    secret = secrets.token_urlsafe(32)
    summary = completion_summary(
        {
            "state": "error",
            "native_success": False,
            "final": {"time_s": 2, "range_m": math.inf, "status": secret, "radar": [secret]},
            "error": {"code": secret, "message": secret},
            "release_state": secret,
            "initial_world": {"engine_build": {"sha256": "a" * 64, "url": secret}},
            "policy_report": {"raw": secret},
            "body": secret,
            "url": f"https://example.invalid/#{secret}",
            "wall_elapsed_s": 3.5,
        }
    )
    assert set(summary) == {
        "state",
        "native_success",
        "measurement",
        "release_state",
        "world_metadata_unchanged",
        "error_code",
        "execution_kind",
        "engine_sha256",
        "elapsed_s",
        "analytics",
    }
    assert set(summary["measurement"]) == {
        "time_s",
        "range_m",
        "relative_speed_mps",
        "damage_j",
        "fuel_used_kg",
        "status",
        "score",
        "score_breakdown",
    }
    assert summary["measurement"]["time_s"] == 2
    assert summary["measurement"]["range_m"] is None
    assert summary["measurement"]["status"] == "unknown"
    assert summary["measurement"]["score"] is None
    assert summary["measurement"]["score_breakdown"] is None
    assert summary["analytics"] is None
    assert summary["error_code"] == "controller_failed"
    assert summary["release_state"] == "unconfirmed_native_watchdog_no_fallback"
    assert summary["execution_kind"] == EXECUTION_KIND
    assert summary["engine_sha256"] == "a" * 64
    assert secret not in json.dumps(summary, allow_nan=False)
    for changes in (
        {"raw_report": secret},
        {"state": secret},
        {"release_state": secret},
        {"native_success": 1},
        {"elapsed_s": math.nan},
        {"engine_sha256": secret},
        {"error_code": secret},
        {"execution_kind": "SDK"},
        {"measurement": {**summary["measurement"], "status": secret}},
        {"analytics": {"raw_payload": secret}},
    ):
        with pytest.raises(ControllerServiceError):
            validate_summary({**summary, **changes})


@pytest.mark.parametrize("score", [-26000.125, 0, 35.5])
@pytest.mark.parametrize("basis", ["time_ratio", "remaining_distance_ratio"])
def test_score_and_native_terms_are_forwarded_signed_without_formula_recalculation(score, basis):
    terms = _score_terms(total=score, progress_basis=basis)
    trial = {"state": "native_terminal", "final": {"score": score, "score_breakdown": terms}}
    summary = completion_summary(trial)
    assert summary["measurement"]["score"] == score
    assert summary["measurement"]["score_breakdown"] == terms
    normalized = validate_summary(summary)
    terms["fuel_penalty_points"] = 1
    assert summary["measurement"]["score_breakdown"]["fuel_penalty_points"] == 26000.25
    summary["measurement"]["score_breakdown"]["offset_points"] = 0
    assert normalized["measurement"]["score_breakdown"]["offset_points"] == -100


def test_score_from_a_replacement_world_is_not_attributed_to_the_original_profile():
    summary = completion_summary(
        {
            "state": "error",
            "world_metadata_unchanged": False,
            "final": {"score": -26000.125, "score_breakdown": _score_terms()},
        }
    )
    assert summary["measurement"]["score"] is None
    assert summary["measurement"]["score_breakdown"] is None


def test_legacy_completion_measurements_stay_unscored_and_partial_score_fields_normalize():
    summary = completion_summary({"state": "duration_limit"})
    legacy = {
        **summary,
        "measurement": {
            key: value
            for key, value in summary["measurement"].items()
            if key not in {"score", "score_breakdown"}
        },
    }
    normalized = validate_summary(legacy)
    assert normalized["measurement"]["score"] is None
    assert normalized["measurement"]["score_breakdown"] is None
    assert "score" not in legacy["measurement"]
    score_only = validate_summary(
        {**legacy, "measurement": {**legacy["measurement"], "score": -26000.125}}
    )
    assert score_only["measurement"]["score"] == -26000.125
    assert score_only["measurement"]["score_breakdown"] is None
    assert (
        validate_summary(
            {**legacy, "measurement": {**legacy["measurement"], "score_breakdown": None}}
        )
        == normalized
    )


@pytest.mark.parametrize(
    "status,damage,native_success",
    [("success", 0.0, True), ("failed_damage", 313800.0, False)],
)
def test_terminal_counters_do_not_turn_a_browser_estimate_into_a_native_score_receipt(
    status, damage, native_success
):
    # Contract inputs only. The browser can estimate from these counters, but the
    # queue must not manufacture a native score or a new initial-range field.
    final = {
        "time_s": 80.0,
        "range_m": 50.0,
        "relative_speed_mps": 1.0,
        "damage_j": damage,
        "fuel_used_kg": 18.75,
        "status": status,
        "initial_range_m": 10000.0,
    }
    before = json.dumps(final, sort_keys=True)
    summary = completion_summary(
        {
            "state": "native_terminal",
            "native_success": native_success,
            "world_metadata_unchanged": True,
            "release_state": "acknowledged",
            "final": final,
        }
    )
    assert summary["native_success"] is native_success
    assert summary["world_metadata_unchanged"] is True
    assert summary["measurement"] == {
        **{key: value for key, value in final.items() if key != "initial_range_m"},
        "score": None,
        "score_breakdown": None,
    }
    assert json.dumps(final, sort_keys=True) == before
    with pytest.raises(ControllerServiceError):
        validate_summary(
            {
                **summary,
                "measurement": {**summary["measurement"], "initial_range_m": 10000.0},
            }
        )


def test_score_schema_rejects_nonfinite_terms_wrong_signs_labels_and_inconsistent_totals():
    summary = completion_summary({"state": "native_terminal"})
    measurement = {**summary["measurement"], "score": -26000.125, "score_breakdown": _score_terms()}
    canary = "https://private-result-canary.invalid/secret"
    invalid_measurements = [
        {**measurement, "score": value}
        for value in (True, "0", math.nan, math.inf, -math.inf, 2**2048, None, -26000)
    ]
    invalid_measurements.extend(
        {**measurement, "score_breakdown": value} for value in ([], canary, True)
    )
    invalid_measurements.append({**measurement, "score_label": canary})
    invalid_terms = [
        _score_terms(formula=canary),
        _score_terms(progress_basis=canary),
        _score_terms(score_label=canary),
        _score_terms(url=canary),
        _score_terms(collision_penalty_points=-1),
        _score_terms(fuel_penalty_points=-1),
    ]
    for name in _score_terms():
        invalid_terms.append({key: value for key, value in _score_terms().items() if key != name})
        if name not in {"formula", "progress_basis"}:
            invalid_terms.extend(
                _score_terms(**{name: value})
                for value in (True, math.nan, math.inf, -math.inf, "1", 2**2048, None)
            )
    for name in ("nominal_time_s", "nominal_fuel_kg", "impact_threshold_j"):
        invalid_terms.extend(_score_terms(**{name: value}) for value in (0, -1))
    invalid_measurements.extend(
        {**measurement, "score_breakdown": terms} for terms in invalid_terms
    )
    for invalid in invalid_measurements:
        with pytest.raises(ControllerServiceError):
            validate_summary({**summary, "measurement": invalid})
    for invalid_score in (True, math.nan, math.inf, canary):
        assert (
            completion_summary({"state": "error", "final": {"score": invalid_score}})[
                "measurement"
            ]["score"]
            is None
        )
    sanitized = completion_summary(
        {
            "state": "native_terminal",
            "final": {
                **measurement,
                "score_breakdown": _score_terms(url=canary),
                "score_label": canary,
            },
        }
    )
    assert sanitized["measurement"]["score"] == -26000.125
    assert sanitized["measurement"]["score_breakdown"] is None
    assert canary not in json.dumps(sanitized, allow_nan=False)


def test_queue_view_accepts_legacy_and_scored_history_without_rewriting_either():
    from wsorb.controllers import catalog

    summary = completion_summary(
        {
            "state": "native_terminal",
            "final": {"score": -26000.125, "score_breakdown": _score_terms()},
        }
    )
    scored = {
        **summary,
        "id": str(uuid.uuid4()),
        "profile_id": "coast",
        "seed": 100,
        "duration_s": 1800,
    }
    legacy = {
        **scored,
        "id": str(uuid.uuid4()),
        "measurement": {
            key: value
            for key, value in scored["measurement"].items()
            if key not in {"score", "score_breakdown"}
        },
    }
    view = {
        "catalog": catalog(),
        "service": {"online": False, "busy": False},
        "active": None,
        "results": [
            legacy,
            scored,
            {**scored, "id": str(uuid.uuid4()), "seed": 101, "world_id": "team"},
        ],
    }
    serialized = json.dumps(view, allow_nan=False)
    assert _queue_view(view) == view
    assert json.dumps(view, allow_nan=False) == serialized
    assert "score" not in legacy["measurement"]
    assert scored["measurement"]["score"] == -26000.125
    assert "world_id" not in legacy
    assert view["results"][-1]["world_id"] == "team"
    for invalid_world in (None, "unknown", 1):
        with pytest.raises(ControllerServiceError):
            _queue_view({**view, "results": [{**scored, "world_id": invalid_world}]})


def test_optional_world_is_retained_in_stopped_failed_and_successful_summaries():
    for state in ("stopped", "error", "native_terminal"):
        summary = completion_summary({"state": state, "world_id": "official"})
        assert summary["world_id"] == "official"
        assert validate_summary(summary)["world_id"] == "official"
    assert "world_id" not in completion_summary({"state": "stopped"})


def test_queue_requests_use_only_existing_bearer_origin_and_independent_no_proxy_clients():
    token = secrets.token_urlsafe(32)
    source = LocalSimulation("http://127.0.0.1:1", token)
    queue = ControllerQueue(source)
    other = queue.fork()
    assert queue._http is not other._http and queue._http is not source._http
    assert any(isinstance(handler, _NoRedirects) for handler in queue._http.handlers)
    assert all(
        not handler.proxies for handler in queue._http.handlers if isinstance(handler, ProxyHandler)
    )
    worker_id = str(uuid.uuid4())
    request = queue._request({"action": "poll", "worker_id": worker_id})
    assert request.full_url == "http://127.0.0.1:1/__local/controllers"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {token}"
    assert json.loads(request.data) == {"action": "poll", "worker_id": worker_id}
    assert queue._request().get_method() == "GET"
    assert MAX_QUEUE_RESPONSE_BYTES == 512 * 1024
    room = RoomSimulation("https://example.invalid", str(uuid.uuid4()), token)
    assert (
        ControllerQueue(room)._request().full_url
        == f"{room.url}/api/rooms/{room.room_id}/controllers"
    )
    assert token not in repr(queue)


def test_new_job_transports_keep_local_headless_honest_and_hosted_pin_immutable_before_rpc():
    token = secrets.token_urlsafe(32)
    local = LocalSimulation("http://127.0.0.1:1", token)
    job = parse_job(_job_metadata(owner_page_id=None))
    one, two = _job_client(local, job), _job_client(local, job)
    assert one is not two and one._http is not two._http
    assert one.bound_page_id is one.last_observed_page_id is None
    room = RoomSimulation("https://example.invalid", str(uuid.uuid4()), token)
    with pytest.raises(ControllerServiceError, match="actual owner UUID"):
        _job_client(room, job)
    room_job = replace(job, owner_page_id=str(uuid.uuid4()))
    pinned = _job_client(room, room_job)
    assert pinned.bound_page_id == room_job.owner_page_id
    assert pinned._rpc_started is False
    assert room.bound_page_id is None


def test_read_only_and_stopped_invocations_cannot_send_any_native_call():
    source = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    job = parse_job(_job_metadata())
    monitor = JobMonitor(job, threading.Event())
    with pytest.raises(ControllerServiceError, match="--run"):
        execute_job(source, job, CheckpointOptions(connection_file="unused.json"), monitor)
    with pytest.raises(ControllerServiceError, match="--run"):
        serve(CheckpointOptions(connection_file="unused.json"))
    monitor.observe(PollReply(job, True))
    summary = execute_job(
        source, job, CheckpointOptions(connection_file="unused.json", run=True), monitor
    )
    assert summary["state"] == "stopped" and summary["release_state"] == "not_claimed"
    explicit_world = replace(job, world_id="official")
    summary = execute_job(
        source, explicit_world, CheckpointOptions(connection_file="unused.json", run=True), monitor
    )
    assert summary["world_id"] == "official" and summary["state"] == "stopped"


def test_genuinely_unavailable_queue_never_discloses_capability_or_remote_exception():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        token = secrets.token_urlsafe(32)
        queue = ControllerQueue(
            LocalSimulation(f"http://127.0.0.1:{reserved.getsockname()[1]}", token)
        )
        with pytest.raises(ControllerServiceError) as unavailable:
            queue.poll(str(uuid.uuid4()))
        assert unavailable.value.code == "queue_unavailable"
        assert unavailable.value.retryable is True
        assert token not in str(unavailable.value)
        assert "127.0.0.1" not in str(unavailable.value)


def test_invalid_queue_views_fail_closed_instead_of_ignoring_unknown_shapes():
    for invalid in (None, {}, {"catalog": [], "service": {}, "active": None, "results": []}):
        with pytest.raises(ControllerServiceError):
            _queue_view(invalid)


def test_abandoned_stalled_job_history_accepts_unknown_measurements_without_inventing_outcomes():
    from wsorb.controllers import catalog

    job = parse_job(_job_metadata())
    record = {
        **completion_summary(
            {
                "state": "interrupted",
                "release_state": "unconfirmed_native_watchdog_no_fallback",
                "error": {"code": "service_stalled"},
            }
        ),
        "id": job.id,
        "profile_id": job.profile_id,
        "seed": 100,
        "duration_s": job.duration_s,
    }
    view = {
        "catalog": catalog(),
        "service": {"online": False, "busy": False},
        "active": None,
        "results": [record],
    }
    assert _queue_view(view)["active"] is None
    assert record["measurement"]["status"] == "unknown"
    assert all(
        record["measurement"][name] is None
        for name in (
            "time_s",
            "range_m",
            "relative_speed_mps",
            "damage_j",
            "fuel_used_kg",
            "score",
            "score_breakdown",
        )
    )
    assert record["native_success"] is record["world_metadata_unchanged"] is False
    assert record["error_code"] == "service_stalled"
    assert record["release_state"] == "unconfirmed_native_watchdog_no_fallback"


def test_cli_requires_explicit_consent_scope_and_bounded_budgets():
    args = parse_args(["run", "--isolated", "--profile", "baseline", "--run"])
    assert args.options.run and args.options.isolated
    assert args.options.duration_s == 1800 and args.options.wall_budget_s == 3600
    assert parse_args(["compare", "--isolated", "--run"]).profiles == PROFILE_IDS
    service = parse_args(["serve", "--room-file", "private.json", "--run"])
    assert service.options.room_file.name == "private.json"
    assert service.options.policy == "baseline" and service.options.playback_rate == 1
    for arguments in (
        ["run", "--isolated", "--profile", "baseline"],
        ["compare", "--isolated"],
        ["compare", "--run"],
        ["serve", "--room-file", "private.json"],
        ["serve", "--run"],
        ["serve", "--isolated", "--run"],
        ["serve", "--room-file", "private.json", "--run", "--playback-rate", "5"],
        ["run", "--isolated", "--profile", "baseline", "--run", "--await-owner"],
        ["run", "--isolated", "--profile", "baseline", "--run", "--duration-s", "nan"],
        [
            "run",
            "--room-file",
            "private.json",
            "--profile",
            "baseline",
            "--run",
            "--playback-rate",
            "5",
        ],
        ["compare", "--isolated", "--run", "--profiles", "baseline,baseline"],
        ["compare", "--isolated", "--run", "--profiles", ""],
    ):
        with pytest.raises((ControllerServiceError, CheckpointError)):
            parse_args(arguments)


@pytest.mark.parametrize(
    "name,profile",
    [
        ("Harry", "basic_pd_radar"),
        ("Tom", "tom_kalman_velocity_mvp"),
        ("Bui", "bui_alpha_beta_mvp"),
        ("Ali", "ali_official_profile"),
    ],
)
def test_cli_named_teammate_shortcuts_resolve_to_reviewed_profile_ids(name, profile):
    args = parse_args(["run", "--isolated", "--run", "--profile", name])
    assert args.profile == args.options.policy == profile


def test_list_profiles_groups_the_same_reviewed_metadata_by_teammate(capsys):
    from wsorb.controllers import catalog

    assert cli.main(["controllers", "list"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result["owners"]) == {"WSO", "Harry", "Tom", "Bui", "Ali", "Published"}
    assert {entry["id"] for entries in result["owners"].values() for entry in entries} == set(
        PROFILE_IDS
    )
    assert all(entry in result["owners"][entry["owner"]] for entry in catalog())


def test_early_cli_dispatch_and_parser_failures_never_echo_private_arguments(capsys):
    secret = secrets.token_urlsafe(32)
    assert cli.main(["controllers", "serve", "--unknown", secret]) == 1
    assert main(["run", "--isolated", "--profile", secret, "--run"]) == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert all(json.loads(line)["ok"] is False for line in captured.out.splitlines())


def test_service_has_no_native_configuration_retry_or_policy_worker_thread():
    source = inspect.getsource(execute_job)
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    runner = next(
        node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "run_trial"
    )
    assert (
        ast.unparse(next(item.value for item in runner.keywords if item.arg == "cancelled"))
        == "monitor.cancelled"
    )
    assert not any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(tree))
    forbidden = {
        "reset",
        "configure_experiment",
        "configure_flight",
        "pause",
        "stop",
        "set_command",
        "SetCommand",
    }
    for function in (execute_job, serve, _heartbeat):
        tree = ast.parse(inspect.getsource(function))
        assert not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden
            for node in ast.walk(tree)
        )
    assert "target=_heartbeat" in inspect.getsource(serve)
    assert "queue.fork()" in inspect.getsource(serve)


@pytest.fixture
def owned_native_local():
    from wsorb.checkpoint import isolated_simulation

    root = Path(__file__).resolve().parents[1]
    if not shutil.which("node") or not (root / "worker/public/sim/wsorb_bevy_bg.wasm").is_file():
        pytest.skip("Use the existing project Node environment and real WASM build")
    with isolated_simulation() as client:
        yield client


@pytest.mark.parametrize(
    "world_id,seed", [("official", 101), ("team", 0), ("across_phobos", 0xFFFFFFFF)]
)
def test_explicit_reviewed_native_world_runs_through_existing_fenced_runner(
    owned_native_local, world_id, seed
):
    from wsorb.checkpoint import world_metadata
    from wsorb.controller_worlds import controller_world, world_matches
    from wsorb.local_api import control_context, validate_experiment_settings

    source = owned_native_local
    world = controller_world(world_id)
    initial = source.configure_flight(world.settings, world.preset, seed)
    assert world_matches(initial, world_id, seed), {
        "world_id": world_id,
        "seed": seed,
        "expected_settings": world.settings,
        "native_world": world_metadata(initial),
    }
    metadata = _job_metadata(
        profile_id="coast",
        seed=seed,
        world_id=world_id,
        duration_s=0.2,
        expected_context=control_context(initial),
        owner_page_id=None,
    )
    job = parse_job(metadata)
    wrong_world = replace(job, world_id="team" if world_id != "team" else "official")
    rejected = execute_job(
        source,
        wrong_world,
        CheckpointOptions(connection_file="fixture.json", run=True),
        JobMonitor(wrong_world, threading.Event()),
    )
    assert (
        rejected["error_code"] == "comparison_mismatch"
        and rejected["release_state"] == "not_claimed"
    )
    assert source.snapshot()["observation"]["time_s"] == 0
    summary = execute_job(
        source,
        job,
        CheckpointOptions(connection_file="fixture.json", run=True, wall_budget_s=15),
        JobMonitor(job, threading.Event()),
    )
    assert summary["world_id"] == world_id
    assert summary["error_code"] is None and summary["state"] == "duration_limit", {
        "summary": summary,
        "initial_world": world_metadata(initial),
        "current_world": world_metadata(source.snapshot()),
    }
    assert (
        summary["release_state"] == "acknowledged" and summary["world_metadata_unchanged"] is True
    )
    actual = source.snapshot()
    assert actual["observation"]["seed"] == seed
    assert (
        validate_experiment_settings(actual["observation"]["experiment_settings"]) == world.settings
    )
    assert actual["observation"]["control_authority"]["status"] == "revoked"


def test_ali_adapter_calls_the_original_policy_on_real_public_observations(owned_native_local):
    from wsorb.controller_worlds import controller_world
    from wsorb.controllers import create_controller, sample_from_observation
    from wsorb.controllers.official_profile import official_profile_policy

    source = owned_native_local
    world = controller_world("official")
    initial = source.configure_flight(world.settings, world.preset, 101)
    observation = initial["observation"]
    adapter, original = create_controller("ali_official_profile"), official_profile_policy()
    produced = sample_from_observation(
        observation, source_kind="local_public_snapshot", retain_observation=True
    )
    expected = original(observation)
    actual = adapter.command(
        produced, observation["radar"], now_elapsed_time_s=observation["time_s"]
    )
    for left, right in zip(actual, expected, strict=True):
        np.testing.assert_array_equal(left, right)
    assert adapter.report["eligible"] is True
    assert adapter.report["guidance"]["can_command"] == original.report["can_command"]
    assert (
        adapter.report["decision_valid_until_elapsed_s"]
        <= original.report["decision_valid_until_s"]
    )
    # Invalid delivery is not an eligible zero command and cannot start Play.
    invalid = deepcopy(observation)
    invalid["radar"] = []
    invalid["radar_backup"] = []
    failed = create_controller("ali_official_profile")
    failed_sample = sample_from_observation(
        invalid, source_kind="local_public_snapshot", retain_observation=True
    )
    failed.command(failed_sample, [], now_elapsed_time_s=invalid["time_s"])
    assert failed.report["eligible"] is False
    assert failed.report["guidance"]["can_command"] is False
    assert source.snapshot()["runtime"]["running"] is False


@pytest.mark.parametrize("profile", ["ali_official_profile", "official_basic_beeline"])
def test_official_profiles_really_run_with_native_fences_and_bounded_leases(
    owned_native_local, profile
):
    from wsorb.controller_worlds import controller_world
    from wsorb.local_api import control_context

    source = owned_native_local
    world = controller_world("official")
    initial = source.configure_flight(world.settings, world.preset, 100)
    job = parse_job(
        _job_metadata(
            profile_id=profile,
            world_id="official",
            duration_s=0.5,
            expected_context=control_context(initial),
            owner_page_id=None,
        )
    )
    summary = execute_job(
        source,
        job,
        CheckpointOptions(connection_file="fixture.json", run=True, wall_budget_s=15),
        JobMonitor(job, threading.Event()),
    )
    assert summary["world_id"] == "official"
    assert summary["state"] == "duration_limit" and summary["error_code"] is None
    assert (
        summary["release_state"] == "acknowledged" and summary["world_metadata_unchanged"] is True
    )
    after = source.snapshot()
    assert (
        after["observation"]["control_authority"]["term"]
        > initial["observation"]["control_authority"]["term"]
    )
    assert after["observation"]["control_authority"]["status"] == "revoked"
    assert after["observation"]["mission"]["flight_analytics"]["burn_duration_s"] > 0


@pytest.mark.parametrize("finish", ["duration", "stop", "context_mismatch"])
def test_real_owned_service_waits_for_consent_then_runs_or_yields_once(
    owned_native_local, tmp_path, finish
):
    """Existing native HTTP/WASM fixture only. No browser readiness is claimed."""
    source = owned_native_local
    initial = source.snapshot()
    path = tmp_path / "service.json"
    path.write_text(json.dumps({"version": 1, "url": source.url, "token": source._token}))
    path.chmod(0o600)
    options = CheckpointOptions(connection_file=path, run=True, wall_budget_s=15)
    stopped, errors = threading.Event(), []

    def run_service():
        try:
            serve(options, stopped=stopped)
        except BaseException as error:
            errors.append(type(error).__name__)

    worker = threading.Thread(target=run_service, daemon=True)
    queue = ControllerQueue(source)
    assert queue.get()["service"] == {"online": False, "busy": False}
    worker.start()
    try:
        end = time.monotonic() + 20
        while not queue.get()["service"]["online"]:
            assert not errors and time.monotonic() < end
            time.sleep(0.025)
        idle = source.snapshot()
        assert idle["observation"]["time_s"] == initial["observation"]["time_s"] == 0
        assert idle["runtime"]["running"] is False
        assert (
            idle["observation"]["control_authority"] == initial["observation"]["control_authority"]
        )
        from wsorb.local_api import control_context

        expected = control_context(idle)
        if finish == "context_mismatch":
            expected["trial_revision"] += 1
        submitted = _queue_view(
            queue._send(
                {
                    "action": "submit",
                    "profile_id": "coast",
                    "seed": 100,
                    "duration_s": 1800 if finish == "stop" else 0.2,
                    "fresh": True,
                    "expected_context": expected,
                }
            )
        )
        job_id = submitted["active"]["id"]
        assert submitted["active"]["owner_page_id"] is None
        if finish == "stop":
            while source.snapshot()["observation"]["time_s"] <= 0.1:
                assert not errors and time.monotonic() < end
                time.sleep(0.025)
            _queue_view(queue._send({"action": "stop", "job_id": job_id}))
        while True:
            view = queue.get()
            if view["active"] is None and view["results"]:
                break
            assert not errors and time.monotonic() < end
            time.sleep(0.025)
        assert len(view["results"]) == 1
        result = view["results"][0]
        assert result["id"] == job_id and result["profile_id"] == "coast"
        assert result["execution_kind"] == EXECUTION_KIND
        assert result["measurement"]["score"] is None
        assert result["measurement"]["score_breakdown"] is None
        assert source._token not in json.dumps(result, allow_nan=False)
        actual = source.snapshot()
        assert actual["observation"]["mission"]["score"] is None
        if finish == "context_mismatch":
            assert result["analytics"] is None
            assert result["state"] == "error" and result["error_code"] == "context_mismatch"
            assert result["release_state"] == "not_claimed"
            assert actual["runtime"]["running"] is False
            assert actual["observation"]["time_s"] == 0
            assert (
                actual["observation"]["control_authority"]
                == initial["observation"]["control_authority"]
            )
        else:
            assert (
                result["analytics"]["totals"]["observed_sim_s"] == result["measurement"]["time_s"]
            )
            assert result["analytics"]["totals"]["burn_count"] == 0
            assert result["analytics"]["totals"]["angular_burn_count"] == 0
            assert len(result["analytics"]["samples"]) <= 48
            assert all(len(row) == 9 for row in result["analytics"]["samples"])
            assert result["state"] == ("stopped" if finish == "stop" else "duration_limit")
            assert result["release_state"] == "acknowledged"
            assert result["world_metadata_unchanged"] is True
            assert result["measurement"]["time_s"] > 0
            assert actual["runtime"]["running"] is True  # No Stop/Pause fallback.
            assert actual["observation"]["control_authority"]["status"] == "revoked"
            assert actual["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [
                0,
                0,
                0,
            ]
        assert actual["observation"]["seed"] == 100
        assert actual["observation"]["geometry"]["debris_count"] == 500
    finally:
        stopped.set()
        worker.join(timeout=10)
    assert not worker.is_alive() and not errors
