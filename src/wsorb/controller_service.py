"""Named, reviewed Python profiles over the existing fenced native API.

The persistent service polls authenticated job metadata only until a browser
submits fresh Run consent. It never configures, resets, pauses or replays a job.
A completed job's metadata is retried until recorded; no flight command is
replayed. Each job uses a new native transport and an independent heartbeat
HTTP client.
"""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

from . import diagnostics
from .checkpoint import (
    MAX_OUTPUT_BYTES,
    CheckpointOptions,
    _summary,
    _validate_trial_world,
    run_checkpoint,
    run_trial,
    world_metadata,
)
from .controller_worlds import controller_world, uint32
from .controllers import PROFILE_IDS
from .flight_analytics import validate_run_analytics
from .local_api import (
    LocalPageMismatchError,
    LocalSimulation,
    _control_authority,
    _expected_context,
    control_context,
)
from .room_api import RoomSimulation, _http_client
from .scoring import native_score_breakdown

_PROFILE_NAMES = {
    "harry": "basic_pd_radar",
    "bui": "bui_alpha_beta_mvp",
    "tom": "tom_kalman_velocity_mvp",
    "ali": "ali_official_profile",
    "ali mvp": "ali_radar_mvp",
    "ali_mvp": "ali_radar_mvp",
    "ali mppi": "ali_mppi",
    "ali_mppi": "ali_mppi",
}
MAX_QUEUE_RESPONSE_BYTES = 512 * 1024
POLL_INTERVAL_S = 1.0
QUEUE_TIMEOUT_S = 2.0
# The relay marks a service lost after 15 s without a poll. Tolerate a short
# burst of transient poll failures, stop before that deadline, and keep the
# completion retry window independent of the relay's heartbeat.
HEARTBEAT_GRACE_S = 10.0
COMPLETION_RETRY_WINDOW_S = 30.0
COMPLETION_RETRY_MAX_DELAY_S = 5.0
EXECUTION_KIND = "asynchronous_fenced_leased_API"
_STATES = frozenset(
    {"native_terminal", "duration_limit", "wall_budget_limit", "stopped", "error", "interrupted"}
)
_RELEASE_STATES = frozenset(
    {
        "not_claimed",
        "acknowledged",
        "skipped_after_takeover_no_fallback",
        "rejected_unapplied_no_fallback",
        "page_mismatch_undelivered_no_fallback",
        "unconfirmed_native_watchdog_no_fallback",
    }
)
_ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "invalid_options",
        "invalid_job",
        "invalid_queue_response",
        "queue_unavailable",
        "queue_rejected",
        "external_scope_required",
        "owner_in_use",
        "heartbeat_lost",
        "execution_failed",
        "native_input_unavailable",
        "setup_failed",
        "invocation_failed",
    }
)
_COMPLETION_ERROR_CODES = frozenset(
    {
        *_ERROR_CODES,
        "controller_failed",
        "interrupted",
        "stopped",
        "context_mismatch",
        "stale_context",
        "command_expired",
        "sensor_unavailable",
        "sensor_no_sample",
        "page_mismatch",
        "local_busy",
        "owner_unavailable",
        "service_offline",
        "service_stalled",
        "transport_failed",
        "invalid_snapshot",
        "source_contract",
        "control_not_ready",
        "engine_build_mismatch",
        "comparison_mismatch",
        "stock_world_required",
        "fresh_trial_required",
        "external_rate",
        "mission_not_running",
        "manual_control",
        "consent_ended",
        "decision_expired",
        "lease_expired",
        "play_timeout",
        "policy_command_bounds",
        "policy_ineligible",
        "claim_unapplied",
        "claim_mismatch",
        "claim_failed",
        "command_failed",
        "play_failed",
        "rate_failed",
        "observation_failed",
        "decision_failed",
        "run_required",
        "invalid_scope",
        "invalid_policy",
        "shared_policy_unavailable",
        "owner_read_protocol",
    }
)
_COMPLETION_CODE_MAP = {
    "heartbeat_lost": "transport_failed",
    "transport_unavailable": "transport_failed",
    "owner_in_use": "consent_ended",
    "native_input_unavailable": "invalid_snapshot",
}
_MEASUREMENTS = ("time_s", "range_m", "relative_speed_mps", "damage_j", "fuel_used_kg")
_MISSION_STATES = frozenset({"running", "success", "failed_damage", "unknown"})
_SUMMARY_FIELDS = frozenset(
    {
        "state",
        "native_success",
        "measurement",
        "release_state",
        "world_metadata_unchanged",
        "error_code",
        "execution_kind",
        "engine_sha256",
        "elapsed_s",
    }
)


class ControllerServiceError(RuntimeError):
    """Fixed, credential-free failures. Remote exception text is never public."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _uuid(value: Any) -> str:
    try:
        if isinstance(value, str):
            parsed = uuid.UUID(value)
            if (
                str(parsed) == value
                and parsed.variant == uuid.RFC_4122
                and parsed.version in range(1, 9)
            ):
                return value
    except ValueError:
        pass
    raise ControllerServiceError("invalid_job", "Controller identifiers must be canonical UUIDs.")


def _profile_id(value: Any) -> str:
    if isinstance(value, str):
        if value in PROFILE_IDS:
            return value
        named = _PROFILE_NAMES.get(value.casefold())
        if named is not None:
            return named
    raise ControllerServiceError("invalid_options", "Select a reviewed controller profile.")


def _sha256(value: Any) -> str | None:
    return (
        value
        if isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
        else None
    )


@dataclass(frozen=True, slots=True)
class ControllerJob:
    id: str
    profile_id: str
    seed: int
    duration_s: float
    expected_context: Mapping[str, Any]
    owner_page_id: str | None
    status: str
    stop_requested: bool
    world_id: str | None = None

    @property
    def execution_identity(self) -> tuple[Any, ...]:
        """A heartbeat may change status/Stop, never the original consent."""
        return (
            self.id,
            self.profile_id,
            self.seed,
            self.duration_s,
            tuple(sorted(self.expected_context.items())),
            self.owner_page_id,
            self.world_id,
        )


def parse_job(value: Any, *, allow_stalled: bool = False) -> ControllerJob:
    fields = {
        "id",
        "profile_id",
        "seed",
        "duration_s",
        "expected_context",
        "owner_page_id",
        "status",
        "stop_requested",
    }
    if (
        not isinstance(value, dict)
        or not fields.issubset(value)
        or set(value) - fields - {"created_at", "world_id"}
    ):
        raise ControllerServiceError("invalid_job", "Invalid controller job metadata.")
    if "created_at" in value and (_number(value["created_at"]) is None or value["created_at"] < 0):
        raise ControllerServiceError("invalid_job", "Invalid controller creation timestamp.")
    duration = _number(value["duration_s"])
    try:
        context = _expected_context(value["expected_context"])
        seed = uint32(value["seed"])
        world_id = controller_world(value["world_id"]).id if "world_id" in value else None
    except ValueError:
        raise ControllerServiceError("invalid_job", "Invalid controller consent context.") from None
    statuses = ("queued", "running", "stopping") + (("stalled",) if allow_stalled else ())
    if (
        not isinstance(value["profile_id"], str)
        or value["profile_id"] not in PROFILE_IDS
        or context["seed"] != seed
        or context["running"] is not False
        or duration is None
        or not 0 < duration <= 1800
        or not isinstance(value["status"], str)
        or value["status"] not in statuses
        or type(value["stop_requested"]) is not bool
    ):
        raise ControllerServiceError("invalid_job", "Invalid bounded controller job.")
    owner = None if value["owner_page_id"] is None else _uuid(value["owner_page_id"])
    return ControllerJob(
        _uuid(value["id"]),
        value["profile_id"],
        seed,
        duration,
        context,
        owner,
        value["status"],
        value["stop_requested"],
        world_id,
    )


@dataclass(frozen=True, slots=True)
class PollReply:
    job: ControllerJob | None
    stop_requested: bool


def parse_poll(value: Any) -> PollReply:
    if (
        not isinstance(value, dict)
        or set(value) != {"job", "stop_requested"}
        or type(value["stop_requested"]) is not bool
    ):
        raise ControllerServiceError("invalid_queue_response", "Invalid controller poll response.")
    return PollReply(
        None if value["job"] is None else parse_job(value["job"]), value["stop_requested"]
    )


def validate_summary(value: Any) -> dict[str, Any]:
    """Validate the exact completion schema before any mutation is sent."""
    message = "Invalid bounded controller completion summary."
    if (
        not isinstance(value, dict)
        or not _SUMMARY_FIELDS.issubset(value)
        or set(value) - _SUMMARY_FIELDS - {"analytics", "world_id"}
    ):
        raise ControllerServiceError("invalid_queue_response", message)
    measurement = value["measurement"]
    required = {*_MEASUREMENTS, "status"}
    if (
        not isinstance(measurement, dict)
        or not required.issubset(measurement)
        or set(measurement) - required - {"score", "score_breakdown"}
    ):
        raise ControllerServiceError("invalid_queue_response", message)
    for name in _MEASUREMENTS:
        number = _number(measurement[name])
        if measurement[name] is not None and (number is None or number < 0):
            raise ControllerServiceError("invalid_queue_response", message)
    score = _number(measurement.get("score"))
    if measurement.get("score") is not None and score is None:
        raise ControllerServiceError("invalid_queue_response", message)
    breakdown = native_score_breakdown(measurement.get("score_breakdown"), score=score)
    if measurement.get("score_breakdown") is not None and breakdown is None:
        raise ControllerServiceError("invalid_queue_response", message)
    status = measurement["status"]
    error_code, state, release = value["error_code"], value["state"], value["release_state"]
    elapsed = _number(value["elapsed_s"])
    if (
        (not isinstance(status, str) or status not in _MISSION_STATES)
        or not isinstance(state, str)
        or state not in _STATES
        or not isinstance(release, str)
        or release not in _RELEASE_STATES
        or type(value["native_success"]) is not bool
        or type(value["world_metadata_unchanged"]) is not bool
        or (
            error_code is not None
            and (not isinstance(error_code, str) or error_code not in _COMPLETION_ERROR_CODES)
        )
        or value["execution_kind"] != EXECUTION_KIND
        or (value["engine_sha256"] is not None and _sha256(value["engine_sha256"]) is None)
        or elapsed is None
        or elapsed < 0
    ):
        raise ControllerServiceError("invalid_queue_response", message)
    analytics = validate_run_analytics(value.get("analytics"))
    if value.get("analytics") is not None and (analytics is None or len(analytics["samples"]) > 48):
        raise ControllerServiceError("invalid_queue_response", message)
    if "world_id" in value:
        try:
            controller_world(value["world_id"])
        except ValueError:
            raise ControllerServiceError("invalid_queue_response", message) from None
    return {
        **value,
        "measurement": {**measurement, "score": score, "score_breakdown": breakdown},
        "analytics": analytics,
    }


def completion_summary(trial: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only native outcome fields, never reports, radar, URLs or exception text."""
    final = trial.get("final")
    final = final if isinstance(final, Mapping) else {}
    measurement = {}
    for name in _MEASUREMENTS:
        number = _number(final.get(name))
        measurement[name] = number if number is not None and number >= 0 else None
    status = final.get("status")
    measurement["status"] = (
        status if isinstance(status, str) and status in _MISSION_STATES else "unknown"
    )
    measurement["score"] = _number(final.get("score"))
    measurement["score_breakdown"] = native_score_breakdown(
        final.get("score_breakdown"), score=measurement["score"]
    )
    if trial.get("world_metadata_unchanged") is False:
        # A later receipt may belong to a manual takeover or replacement world.
        # Never attribute that flight's score to the original profile job.
        measurement["score"] = measurement["score_breakdown"] = None
    error = trial.get("error")
    code = error.get("code") if isinstance(error, Mapping) else None
    if code is not None:
        code = (
            _COMPLETION_CODE_MAP.get(code, code) if isinstance(code, str) else "controller_failed"
        )
        if code not in _COMPLETION_ERROR_CODES:
            code = "controller_failed"
    state = trial.get("state")
    if not isinstance(state, str) or state not in _STATES:
        state, code = "error", "controller_failed"
    release = trial.get("release_state")
    if not isinstance(release, str) or release not in _RELEASE_STATES:
        release = "unconfirmed_native_watchdog_no_fallback"
    world = trial.get("initial_world")
    world = world if isinstance(world, Mapping) else {}
    build = world.get("engine_build")
    build = build if isinstance(build, Mapping) else {}
    elapsed = _number(trial.get("wall_elapsed_s"))
    return validate_summary(
        {
            "state": state,
            "native_success": trial.get("native_success") is True,
            "measurement": measurement,
            "release_state": release,
            "world_metadata_unchanged": trial.get("world_metadata_unchanged") is True,
            "error_code": code,
            "execution_kind": EXECUTION_KIND,
            "engine_sha256": _sha256(build.get("sha256")),
            "elapsed_s": max(0.0, elapsed) if elapsed is not None else 0.0,
            "analytics": (
                validate_run_analytics(trial.get("analytics"))
                if trial.get("world_metadata_unchanged") is True
                else None
            ),
            **({"world_id": controller_world(trial["world_id"]).id} if "world_id" in trial else {}),
        }
    )


def _queue_view(value: Any) -> dict[str, Any]:
    message = "Invalid controller queue view."
    if not isinstance(value, dict) or set(value) != {"catalog", "service", "active", "results"}:
        raise ControllerServiceError("invalid_queue_response", message)
    service, catalog, results = value["service"], value["catalog"], value["results"]
    if (
        not isinstance(service, dict)
        or set(service) != {"online", "busy"}
        or any(type(service[name]) is not bool for name in ("online", "busy"))
        or not isinstance(catalog, list)
        or len(catalog) != len(PROFILE_IDS)
        or not isinstance(results, list)
        or len(results) > 32
    ):
        raise ControllerServiceError("invalid_queue_response", message)
    ids = []
    catalog_fields = {
        "id",
        "owner",
        "label",
        "contributor",
        "description",
        "profile",
        "experimental",
        "target_seeking",
        "source",
    }
    for entry in catalog:
        if (
            not isinstance(entry, dict)
            or set(entry) != catalog_fields
            or any(
                not isinstance(entry[name], str) or len(entry[name]) > 2048
                for name in (
                    "id",
                    "owner",
                    "label",
                    "contributor",
                    "description",
                    "profile",
                    "source",
                )
            )
            or type(entry["experimental"]) is not bool
            or type(entry["target_seeking"]) is not bool
        ):
            raise ControllerServiceError("invalid_queue_response", message)
        ids.append(entry["id"])
    if len(set(ids)) != len(PROFILE_IDS) or set(ids) != set(PROFILE_IDS):
        raise ControllerServiceError("invalid_queue_response", message)
    if value["active"] is not None:
        parse_job(value["active"], allow_stalled=True)
    record_fields = _SUMMARY_FIELDS | {"id", "profile_id", "seed", "duration_s"}
    for record in results:
        if (
            not isinstance(record, dict)
            or not record_fields.issubset(record)
            or set(record) - record_fields - {"analytics", "world_id"}
            or record.get("profile_id") not in PROFILE_IDS
            or type(record.get("seed")) is not int
            or not 0 <= record["seed"] <= 0xFFFFFFFF
            or _number(record.get("duration_s")) is None
            or not 0 < record["duration_s"] <= 1800
        ):
            raise ControllerServiceError("invalid_queue_response", message)
        _uuid(record.get("id"))
        validate_summary(
            {name: record[name] for name in _SUMMARY_FIELDS}
            | ({"analytics": record["analytics"]} if "analytics" in record else {})
            | ({"world_id": record["world_id"]} if "world_id" in record else {})
        )
    return value


class ControllerQueue:
    """A separate no-proxy/no-redirect client for authenticated job metadata only."""

    def __init__(self, client: LocalSimulation) -> None:
        if not isinstance(client, LocalSimulation):
            raise ControllerServiceError(
                "invalid_options", "A validated native client is required."
            )
        self._source = client
        self._token = client._token
        self._endpoint = (
            f"{client.url}/api/rooms/{client.room_id}/controllers"
            if isinstance(client, RoomSimulation)
            else f"{client.url}/__local/controllers"
        )
        self._http = _http_client()

    def fork(self) -> ControllerQueue:
        """Never share urllib handlers with the heartbeat or native RPC thread."""
        return ControllerQueue(self._source)

    def _request(self, payload: Mapping[str, Any] | None = None) -> Request:
        return Request(
            self._endpoint,
            data=None if payload is None else json.dumps(payload, allow_nan=False).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="GET" if payload is None else "POST",
        )

    def _send(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # One request per call. Completion delivery retries the same summary at
        # a higher level, and the relay accepts an exact replay as idempotent.
        try:
            with self._http.open(self._request(payload), timeout=QUEUE_TIMEOUT_S) as response:
                if response.status != 200:
                    raise ControllerServiceError(
                        "queue_rejected", "Controller queue rejected the request."
                    )
                body = response.read(MAX_QUEUE_RESPONSE_BYTES + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            raise ControllerServiceError(
                "queue_rejected",
                "Controller queue rejected the request.",
                retryable=status in (408, 429, 500, 502, 503, 504),
            ) from None
        except (OSError, URLError, HTTPException):
            raise ControllerServiceError(
                "queue_unavailable",
                "Controller queue unavailable; no flight command was retried.",
                retryable=True,
            ) from None
        if len(body) > MAX_QUEUE_RESPONSE_BYTES:
            raise ControllerServiceError(
                "invalid_queue_response", "Controller response exceeds 512 KiB."
            )
        try:
            value = json.loads(body)
        except ValueError:
            raise ControllerServiceError(
                "invalid_queue_response", "Invalid controller queue JSON."
            ) from None
        if not isinstance(value, dict):
            raise ControllerServiceError(
                "invalid_queue_response", "Invalid controller queue response."
            )
        return value

    def get(self) -> dict[str, Any]:
        return _queue_view(self._send())

    def poll(self, worker_id: str) -> PollReply:
        return parse_poll(self._send({"action": "poll", "worker_id": _uuid(worker_id)}))

    def complete(self, worker_id: str, job_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        return _queue_view(
            self._send(
                {
                    "action": "complete",
                    "worker_id": _uuid(worker_id),
                    "job_id": _uuid(job_id),
                    "result": validate_summary(summary),
                }
            )
        )


class SeenJobs:
    """Remember every admitted ID, including failed or stopped jobs. Never replay one."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def take(self, reply: PollReply) -> ControllerJob | None:
        job = reply.job
        if job is None or job.id in self._seen:
            return None
        self._seen.add(job.id)
        return job


class JobMonitor:
    """Cancellation is sticky, including a heartbeat loss during policy computation."""

    def __init__(self, job: ControllerJob, service_stopped: threading.Event) -> None:
        self._identity = job.execution_identity
        self._service_stopped = service_stopped
        self._cancel = threading.Event()
        self.heartbeat_lost = False
        if job.stop_requested or job.status == "stopping":
            self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set() or self._service_stopped.is_set()

    def observe(self, reply: PollReply) -> None:
        if reply.job is None or reply.job.execution_identity != self._identity:
            self.fail_closed()
        elif reply.stop_requested or reply.job.stop_requested or reply.job.status == "stopping":
            self._cancel.set()

    def fail_closed(self) -> None:
        self.heartbeat_lost = True
        self._cancel.set()


def _log(event: str, **fields: Any) -> None:
    """Fixed, credential-free service diagnostics on stderr."""
    print(json.dumps({"event": event, **fields}, sort_keys=True), file=sys.stderr, flush=True)


def _heartbeat(
    queue: ControllerQueue, worker_id: str, monitor: JobMonitor, done: threading.Event
) -> None:
    last_poll = time.monotonic()
    while not done.wait(POLL_INTERVAL_S):
        try:
            reply = queue.poll(worker_id)
        except Exception as error:
            # A single timeout is not a lost service. The relay's own window is
            # 15 s; stop before it expires, leaving time to deliver the result.
            if time.monotonic() - last_poll >= HEARTBEAT_GRACE_S:
                _log(
                    "controller_heartbeat_lost",
                    code=getattr(error, "code", "heartbeat_lost"),
                )
                monitor.fail_closed()
        else:
            last_poll = time.monotonic()
            monitor.observe(reply)
        if monitor.heartbeat_lost:
            return


def _complete_with_retry(
    queue: ControllerQueue,
    worker_id: str,
    job_id: str,
    summary: dict[str, Any],
    stopped: threading.Event,
) -> dict[str, Any]:
    """Deliver one completion. The relay treats an exact replay as idempotent."""
    deadline = time.monotonic() + COMPLETION_RETRY_WINDOW_S
    delay, attempt = 0.5, 0
    while True:
        try:
            return queue.complete(worker_id, job_id, summary)
        except ControllerServiceError as error:
            attempt += 1
            if not error.retryable or time.monotonic() >= deadline:
                _log(
                    "controller_completion_failed",
                    code=error.code,
                    retryable=error.retryable,
                    attempts=attempt,
                )
                raise
            _log("controller_completion_retry", code=error.code, attempts=attempt)
            stopped.wait(delay)
            delay = min(delay * 2, COMPLETION_RETRY_MAX_DELAY_S)


def validate_job_context(job: ControllerJob, context: Mapping[str, Any], *, time_s: Any) -> None:
    """Pure consent comparison. Running, revisions and seed must all match exactly."""
    try:
        parsed = _expected_context(context)
    except ValueError:
        raise ControllerServiceError(
            "context_mismatch", "Controller consent context changed."
        ) from None
    if parsed != job.expected_context:
        raise ControllerServiceError("context_mismatch", "Controller consent context changed.")
    if _number(time_s) != 0 or parsed["running"] is not False:
        raise ControllerServiceError(
            "fresh_trial_required", "Controller jobs require a fresh paused trial."
        )


def _validate_job_snapshot(job: ControllerJob, snapshot: Mapping[str, Any]) -> None:
    observation = snapshot.get("observation", {})
    validate_job_context(job, control_context(snapshot), time_s=observation.get("time_s"))
    if _control_authority(snapshot)["status"] not in ("idle", "revoked"):
        raise ControllerServiceError(
            "owner_in_use", "An existing controller owns this flight; no takeover."
        )
    _validate_trial_world(
        snapshot, world_id=job.world_id, seed=job.seed if job.world_id is not None else None
    )


def _verify_job_page(client: LocalSimulation, job: ControllerJob) -> None:
    if isinstance(client, RoomSimulation):
        valid = job.owner_page_id is not None and client.bound_page_id == job.owner_page_id
    else:
        valid = client.last_observed_page_id == job.owner_page_id
    if not valid:
        raise ControllerServiceError(
            "page_mismatch", "The job's actual owner page no longer matches."
        )


class _JobClaimGuard:
    """Recheck the runner's actual claim receipt, not just an earlier preflight read."""

    def __init__(self, job: ControllerJob, *args: Any, **kwargs: Any) -> None:
        self._job = job
        super().__init__(*args, **kwargs)

    def claim_control(
        self,
        run_id: str,
        controller_id: str,
        snapshot: Mapping[str, Any],
        *,
        expected_epoch: int | None = None,
        lease_sim_s: float = 0.75,
    ) -> dict[str, Any]:
        _validate_job_snapshot(self._job, snapshot)
        _verify_job_page(self, self._job)
        return super().claim_control(
            run_id, controller_id, snapshot, expected_epoch=expected_epoch, lease_sim_s=lease_sim_s
        )


class _JobLocalSimulation(_JobClaimGuard, LocalSimulation):
    pass


class _JobRoomSimulation(_JobClaimGuard, RoomSimulation):
    pass


def _job_client(source: LocalSimulation, job: ControllerJob) -> LocalSimulation:
    if isinstance(source, RoomSimulation):
        if job.owner_page_id is None:
            raise ControllerServiceError(
                "page_mismatch", "Hosted jobs require their actual owner UUID."
            )
        client = _JobRoomSimulation(
            job,
            source.url,
            source.room_id,
            source._token,
            timeout_s=2.0,
            # Existing explicit test-only loopback consent is not expanded to the CLI.
            allow_loopback=source.url.startswith("http://127.0.0.1:"),
        )
        client.bind_controller_page(job.owner_page_id)
        return client
    return _JobLocalSimulation(job, source.url, source._token, timeout_s=2.0)


def _failure_trial(error: BaseException, elapsed_s: float) -> dict[str, Any]:
    code = getattr(error, "code", None)
    if isinstance(error, KeyboardInterrupt):
        code = "interrupted"
    elif isinstance(error, LocalPageMismatchError):
        code = "page_mismatch"
    if not isinstance(code, str) or (
        code not in _ERROR_CODES
        and code not in _COMPLETION_ERROR_CODES
        and code not in _COMPLETION_CODE_MAP
    ):
        code = "execution_failed"
    return {
        "state": "interrupted" if isinstance(error, KeyboardInterrupt) else "error",
        "release_state": "not_claimed",
        "error": {"code": code},
        "wall_elapsed_s": elapsed_s,
    }


def execute_job(
    source: LocalSimulation, job: ControllerJob, options: CheckpointOptions, monitor: JobMonitor
) -> dict[str, Any]:
    """Run once on the calling thread. Only run_trial writes native control."""
    if not isinstance(options, CheckpointOptions) or options.run is not True:
        raise ControllerServiceError(
            "run_required", "Controller execution requires explicit --run."
        )
    if options.isolated or options.playback_rate != 1:
        raise ControllerServiceError(
            "external_scope_required", "Service jobs require an external rate-1 transport."
        )
    started = time.monotonic()
    if monitor.cancelled():
        return completion_summary(
            {"state": "stopped", "release_state": "not_claimed"}
            | ({"world_id": job.world_id} if job.world_id is not None else {})
        )
    entered_runner = False
    try:
        client = _job_client(source, job)
        with client.control_transaction():
            initial = client.snapshot()
            _validate_job_snapshot(job, initial)
            _verify_job_page(client, job)
            if not isinstance(client, RoomSimulation):
                client.bind_to_observed_page(require=job.owner_page_id is not None)
        trial_options = replace(
            options, policy=job.profile_id, duration_s=job.duration_s, await_owner=False
        )
        entered_runner = True
        trial = run_trial(
            client,
            trial_options,
            policy=job.profile_id,
            owned=False,
            deadline=started + options.wall_budget_s,
            expected_world=world_metadata(initial),
            world_id=job.world_id,
            seed=job.seed if job.world_id is not None else None,
            cancelled=monitor.cancelled,
        )
    except (Exception, KeyboardInterrupt) as error:
        trial = _failure_trial(error, time.monotonic() - started)
        if entered_runner:
            trial["release_state"] = "unconfirmed_native_watchdog_no_fallback"
    if monitor.heartbeat_lost:
        trial["state"], trial["error"] = "error", {"code": "heartbeat_lost"}
    if job.world_id is not None:
        trial["world_id"] = job.world_id
    return completion_summary(trial)


def serve(options: CheckpointOptions, *, stopped: threading.Event | None = None) -> dict[str, Any]:
    """Wait indefinitely for explicit jobs. Idle time never consumes a flight budget."""
    if not isinstance(options, CheckpointOptions) or options.run is not True:
        raise ControllerServiceError("run_required", "Controller service requires explicit --run.")
    if options.isolated or (options.connection_file is None and options.room_file is None):
        raise ControllerServiceError(
            "external_scope_required", "Select one explicit service descriptor."
        )
    source = (
        RoomSimulation.from_file(options.room_file, timeout_s=2.0)
        if options.room_file is not None
        else LocalSimulation.from_file(options.connection_file, timeout_s=2.0)
    )
    queue = ControllerQueue(source)
    stopped = threading.Event() if stopped is None else stopped
    worker_id, seen, completed, unconfirmed = str(uuid.uuid4()), SeenJobs(), 0, 0
    while not stopped.is_set():
        try:
            reply = queue.poll(worker_id)
        except ControllerServiceError as error:
            if not error.retryable:
                raise
            stopped.wait(POLL_INTERVAL_S)
            continue
        job = seen.take(reply)
        if job is None:
            stopped.wait(POLL_INTERVAL_S)
            continue
        monitor = JobMonitor(job, stopped)
        monitor.observe(reply)
        done = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat,
            args=(queue.fork(), worker_id, monitor, done),
            name="wsorb-controller-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        failed_completion = False
        try:
            with diagnostics.diagnostics_context() if options.diagnostics else nullcontext():
                summary = execute_job(source, job, options, monitor)
            try:
                _complete_with_retry(queue, worker_id, job.id, summary, stopped)
                completed += 1
            except ControllerServiceError as error:
                failed_completion = True
                unconfirmed += 1
                _log("controller_job_unconfirmed", job_id=job.id, code=error.code)
        finally:
            done.set()
            heartbeat.join(timeout=QUEUE_TIMEOUT_S + POLL_INTERVAL_S)
        if failed_completion:
            # Do not keep renewing a job whose result could not be recorded.
            # The relay marks it stalled; an explicit Stop can abandon it.
            stopped.wait(16.0)
    return {
        "ok": unconfirmed == 0,
        "state": "stopped",
        "completed_jobs": completed,
        "unconfirmed_completions": unconfirmed,
    }


@contextmanager
def _interrupt_stops(stopped: threading.Event) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)

    def stop(signum: int, frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ControllerServiceError(
            "invalid_arguments", "Invalid controller arguments. Use --help."
        )


def _budgets(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration-s", type=float, default=1800)
    parser.add_argument("--wall-budget-s", type=float, default=3600)
    parser.add_argument(
        "--cadence-hz", type=float, default=10, help="10..50 wall-clock decisions per second"
    )
    parser.add_argument(
        "--playback-rate", type=int, default=1, help="1, or 5 on an owned isolated world"
    )
    parser.add_argument("--progress", action="store_true", help="bounded native progress on stderr")
    parser.add_argument(
        "--diagnostics", action="store_true", help="bounded allowlisted diagnostics"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _Parser(
        prog="wsorb controllers",
        description="Reviewed Python controller profiles; explicit Run only.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="controller_command", required=True)
    listing = sub.add_parser("list", help="JSON profiles grouped by owner", allow_abbrev=False)
    listing.add_argument("--json", action="store_true", help="JSON is also the default")
    run = sub.add_parser("run", help="one bounded native trial", allow_abbrev=False)
    scope = run.add_mutually_exclusive_group(required=True)
    scope.add_argument("--isolated", action="store_true")
    scope.add_argument("--connection-file", type=Path)
    scope.add_argument("--room-file", type=Path)
    run.add_argument(
        "--profile", required=True, help="profile ID, or Harry, Tom, Bui, Ali or Ali MVP"
    )
    run.add_argument("--run", action="store_true", required=True, help="explicit actuation consent")
    run.add_argument(
        "--await-owner", action="store_true", help="external read-only standby before one run"
    )
    _budgets(run)
    compare = sub.add_parser(
        "compare", help="one fresh owned world per profile", allow_abbrev=False
    )
    compare.add_argument(
        "--profiles", default=",".join(PROFILE_IDS), help="comma-separated reviewed IDs"
    )
    compare.add_argument("--isolated", action="store_true", required=True)
    compare.add_argument("--run", action="store_true", required=True)
    _budgets(compare)
    service = sub.add_parser("serve", help="wait for explicit browser Run jobs", allow_abbrev=False)
    service_scope = service.add_mutually_exclusive_group(required=True)
    service_scope.add_argument("--connection-file", type=Path)
    service_scope.add_argument("--room-file", type=Path)
    service.add_argument(
        "--run", action="store_true", required=True, help="permit explicit jobs only"
    )
    service.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args(argv)
    if args.controller_command == "list":
        return args
    if args.controller_command == "compare":
        profiles = tuple(_profile_id(value.strip()) for value in args.profiles.split(","))
        if not profiles or len(profiles) > len(PROFILE_IDS) or len(set(profiles)) != len(profiles):
            raise ControllerServiceError(
                "invalid_options", "Compare distinct reviewed profiles once each."
            )
        args.profiles = profiles
    if args.controller_command == "run":
        args.profile = _profile_id(args.profile)
    args.options = CheckpointOptions(
        isolated=getattr(args, "isolated", False),
        connection_file=getattr(args, "connection_file", None),
        room_file=getattr(args, "room_file", None),
        run=args.run,
        policy=getattr(args, "profile", "baseline"),
        duration_s=getattr(args, "duration_s", 1800),
        wall_budget_s=getattr(args, "wall_budget_s", 3600),
        cadence_hz=getattr(args, "cadence_hz", 10),
        playback_rate=getattr(args, "playback_rate", 1),
        progress=getattr(args, "progress", False),
        diagnostics=args.diagnostics,
        await_owner=getattr(args, "await_owner", False),
    )
    return args


def list_profiles() -> dict[str, Any]:
    from .controllers import catalog

    owners: dict[str, list[dict[str, Any]]] = {}
    for entry in catalog():
        owners.setdefault(entry["owner"], []).append(entry)
    return {"ok": True, "owners": owners}


def main(argv: Sequence[str] | None = None) -> int:
    is_catalog = False
    try:
        args = parse_args(argv)
        if args.controller_command == "list":
            result = list_profiles()
            is_catalog = True
        elif args.controller_command == "run":
            result = run_checkpoint(args.options)
        elif args.controller_command == "compare":
            from .checkpoint import run_profile_comparison

            result = run_profile_comparison(args.profiles, args.options)
        else:
            stopped = threading.Event()
            with _interrupt_stops(stopped):
                result = serve(args.options, stopped=stopped)
    except (Exception, KeyboardInterrupt) as error:
        failure = _failure_trial(error, 0)
        result = {"ok": False, "error": {"code": failure["error"]["code"]}}
    output = json.dumps(
        result if is_catalog else _summary(result), allow_nan=False, separators=(",", ":")
    )
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        result = {"ok": False, "error": {"code": "output_limit"}}
        output = json.dumps(result)
    print(output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
