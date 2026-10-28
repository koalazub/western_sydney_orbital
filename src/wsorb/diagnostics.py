"""Opt-in, allowlisted JSONL diagnostics and a read-only public API collector.

``WSORB_DIAGNOSTICS=1`` enables stderr logging. ``enable()``, ``disable()`` and
``diagnostics_context()`` override that choice in the current Python context.
No request bodies, remote error text or arbitrary strings are loggable.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import stat
import sys
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

MAX_EVENTS = 2048
MAX_EVENTS_PER_SECOND = 20
MAX_LINE_BYTES = 8192
MAX_DURATION_S = 30.0
MAX_CADENCE_HZ = 2.0


def _enum(*values: str) -> frozenset[str]:
    return frozenset(values)


_METHODS = _enum(
    "status", "snapshot", "command", "preview", "raycast", "pan_tilt", "capture", "create_room"
)
_OPS = _enum(
    "pause",
    "play",
    "step",
    "reset",
    "set_command",
    "set_leased_command",
    "claim_control",
    "release_control",
    "set_sensor_fault",
    "clear_sensor_faults",
    "stop",
    "view",
    "debug_upright",
    "configure_experiment",
    "configure_flight",
    "place_lander",
    "move_launchpad",
    "tune_ship",
    "tune_debris_flow",
)
_CODES = _enum(
    "local_busy",
    "stale_context",
    "command_expired",
    "sensor_unavailable",
    "sensor_no_sample",
    "unknown_outcome",
    "transport_unavailable",
    "invalid_arguments",
    "invalid_options",
    "private_platform",
    "output_unavailable",
    "descriptor_unavailable",
    "owner_unavailable",
    "status_unavailable",
    "physics_unavailable",
    "interrupted",
)
_ERROR_TYPES = _enum(
    "Exception",
    "LocalApiError",
    "LocalBusyError",
    "StaleContextError",
    "CommandExpiredError",
    "SensorUnavailableError",
    "RoomApiError",
    "DiagnosticsError",
    "HTTPError",
    "URLError",
    "OSError",
    "TimeoutError",
    "ConnectionRefusedError",
    "ValueError",
    "TypeError",
    "KeyboardInterrupt",
    "JSONDecodeError",
    "HTTPException",
)
_KNOWN_UNAPPLIED = _enum(
    "local_busy",
    "stale_context",
    "command_expired",
    "sensor_unavailable",
    "sensor_no_sample",
    "owner_unavailable",
)
_CAMERA_MODES = _enum(
    "orbit",
    "mission",
    "first_person",
    "third_person",
    "follow",
    "free",
    "overview",
    "top",
    "ship_top",
    "ship_bottom",
)
_POSE = {
    "source_time_s": "number",
    "position_world_m": "vector3",
    "velocity_world_mps": "vector3",
    "rotation_body_to_world_xyzw": "vector4",
}
_CAMERA = {
    "available": "bool",
    "mode": _CAMERA_MODES,
    "focus": _enum("Shuttle", "Lander", "Target", "LandingPad", "Phobos", "Mars", "L1", "L2"),
    "position_m": "vector3",
    "rotation": "vector4",
    "distance_m": "number",
    "active": "bool",
    "locked": "bool",
    "speed_mps": "number",
}
_VISUAL = {
    "state": _enum("missing", "observed", "ambiguous"),
    "matching_entities": "int",
    "transforms": {
        "has_parent": "bool",
        "local_transform": {
            "translation_parent_m": "vector3",
            "rotation_parent_from_local_xyzw": "vector4",
            "scale": "vector3",
        },
        "global_transform": {
            "translation_world_m": "vector3",
            "linear_world_from_local_columns": "matrix3",
        },
    },
}
_PRESENTATION = {
    "schema": _enum("wsorb.presentation_diagnostics.v1"),
    "enabled": "bool",
    "available": "bool",
    "state": _enum("disabled", "waiting_for_scene", "observed"),
    "measurement": _enum("bevy_main_world_transforms_not_gpu_pixels"),
    "sample": {
        "collection": {
            "stage": _enum("post_update_after_transform_propagation"),
            "bevy_frame": "int",
            "real_elapsed_s": "number",
        },
        "simulation_time_basis": _enum("simulation_elapsed_seconds"),
        "scene_snapshot_clock": _enum(
            "not_exposed_by_scene_api", "scene_cache_snapshot", "missing_scene_cache"
        ),
        "scene_snapshot_source_time_s": "number",
        "scene_snapshot": {
            "source_time_s": "number",
            "source_seed": "int",
            "tuning_revision": "int",
            "lander": _POSE,
            "landing_pad": _POSE,
        },
        "published_snapshot": {
            "source_time_s": "number",
            "trial_revision": "int",
            "tuning_revision": "int",
            "lander": _POSE,
            "landing_pad": _POSE,
        },
        "native_lander": {
            "state": _enum("observed", "unavailable"),
            "source_time_s": "number",
            "tuning_revision": "int",
            "pose": _POSE,
        },
        "lander_visual": _VISUAL,
        "landing_pad_visual": _VISUAL,
        "position_differences": {
            "lander_global_minus_published_m": "vector3",
            "landing_pad_global_minus_published_m": "vector3",
            "lander_global_minus_native_m": "vector3",
            "lander_native_minus_published_m": "vector3",
        },
    },
}
_RENDERER = {
    "started": "bool",
    "ready": "bool",
    "rendered_frames": "int",
    "submitted_primary_frames": "int",
    "completed_primary_frame": "bool",
    "primary_world_draws": "int",
}
_OWNER = {
    "status": _enum("idle", "active", "expired", "revoked"),
    "epoch": "int",
    "term": "int",
    "expires_at_sim_time_s": "number",
}
_ACTIVE_CALL = {
    "job_id": "uuid",
    "trace_id": "uuid",
    "method": _METHODS,
    "op": _OPS,
    "delivered": "bool",
    "deadline_remaining_ms": "number",
}
_STATUS = {
    "enabled": "bool",
    "connected": "bool",
    "busy": "bool",
    "backend": _enum("native_headless", "browser"),
    "physics_ready": "bool",
    "physics_status_cached": "bool",
    "diagnostics": {
        "enabled": "bool",
        "page_id": "uuid",
        "page_lease_remaining_ms": "number",
        "active_control_call": _ACTIVE_CALL,
        "active_capture_call": _ACTIVE_CALL,
    },
    "physics": {
        "started": "bool",
        "phase": _enum("idle", "running", "paused", "failed", "stopped"),
        "source_time_s": "number",
    },
}
_SNAPSHOT = {
    "time_s": "number",
    "navigation_sample_time_s": "number",
    "running": "bool",
    "rate": "number",
    "range_m": "number",
    "relative_speed_mps": "number",
    "mission_status": _enum("running", "success", "failed", "failure"),
    "owner": _OWNER,
    "lander": _POSE,
    "renderer": _RENDERER,
    "page_id": "uuid",
    "page_id_available": "bool",
    "camera_available": "bool",
    "presentation_available": "bool",
    "camera_status_available": "bool",
    "presentation_diagnostics_available": "bool",
    "native_collection_available": "bool",
    "camera": _CAMERA,
    "presentation": _PRESENTATION,
}
_FIELDS = {
    "request_id": "uuid",
    "transport": _enum("local", "room"),
    "stage": _enum("http", "rpc", "collector"),
    "method": _METHODS,
    "op": _OPS,
    "http_status": "int",
    "code": _CODES,
    "error_type": _ERROR_TYPES,
    "elapsed_s": "number",
    "outcome": _enum("received", "acknowledged", "rejected_unapplied", "unknown"),
    "result_kind": _enum("status", "snapshot", "ack", "response", "envelope", "unavailable"),
    "source_time_s": "number",
    "lease_sim_s": "number",
    "fence_epoch": "int",
    "fence_term": "int",
    "status": _STATUS,
    "status_available": "bool",
    "snapshot": _SNAPSHOT,
    "sample_count": "int",
    "skip_count": "int",
    "error_count": "int",
    "dropped_events": "int",
    "sink_failures": "int",
    "duration_s": "number",
    "cadence_hz": "number",
    "actuation_permitted": "bool",
}
_EVENTS = _enum(
    "request_start",
    "request_end",
    "collector_start",
    "status",
    "sample",
    "skip",
    "error",
    "summary",
)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and -1e18 <= value <= 1e18 and math.isfinite(value)


def _valid(value: Any, rule: Any) -> bool:
    if isinstance(rule, frozenset):
        return type(value) is str and len(value) <= 64 and value in rule
    if rule == "bool":
        return type(value) is bool
    if rule == "number":
        return _number(value)
    if rule == "int":
        return type(value) is int and 0 <= value <= 2**53 - 1
    if rule == "uuid":
        if type(value) is not str or len(value) != 36:
            return False
        try:
            return str(uuid.UUID(value)) == value
        except ValueError:
            return False
    if rule in ("vector2", "vector3", "vector4"):
        return (
            type(value) in (list, tuple)
            and len(value) == int(rule[-1])
            and all(_number(item) for item in value)
        )
    if rule == "matrix3":
        return (
            type(value) in (list, tuple)
            and len(value) == 3
            and all(_valid(item, "vector3") for item in value)
        )
    return False


def _select(value: Any, schema: Mapping[str, Any], *, strict: bool = False) -> dict[str, Any]:
    """Visit only fixed fields. Never walk unknown payloads or sensor arrays."""
    if not isinstance(value, Mapping):
        if strict:
            raise ValueError("Invalid diagnostic fields")
        return {}
    if strict and (len(value) > len(schema) or any(key not in schema for key in value)):
        raise ValueError("Unsupported diagnostic field")
    result = {}
    for key, rule in schema.items():
        if key not in value:
            continue
        item = value[key]
        if isinstance(rule, dict):
            if item is None and not strict:
                continue
            result[key] = _select(item, rule, strict=strict)
        elif _valid(item, rule):
            if rule == "matrix3":
                result[key] = [list(row) for row in item]
            else:
                result[key] = list(item) if rule in ("vector2", "vector3", "vector4") else item
        elif strict:
            raise ValueError("Invalid diagnostic value")
    return result


class Diagnostics:
    """A bounded sink. Invalid fields and sink failures produce no fallback output."""

    def __init__(
        self,
        *,
        sink: TextIO | None = None,
        max_events: int = MAX_EVENTS,
        max_events_per_second: int = MAX_EVENTS_PER_SECOND,
    ) -> None:
        for value, limit in (
            (max_events, MAX_EVENTS),
            (max_events_per_second, MAX_EVENTS_PER_SECOND),
        ):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError("Diagnostic event limits must be bounded positive integers")
        self.sink = sink
        self.max_events = max_events
        self.max_events_per_second = max_events_per_second
        self.events = 0
        self.dropped = 0
        self.sink_failures = 0
        self._started = time.monotonic()
        self._window = self._started
        self._window_events = 0
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> bool:
        try:
            with self._lock:
                now = time.monotonic()
                if now - self._window >= 1:
                    self._window, self._window_events = now, 0
                if (
                    self.events >= self.max_events
                    or self._window_events >= self.max_events_per_second
                ):
                    self.dropped += 1
                    return False
                if type(event) is not str or event not in _EVENTS:
                    return False
                selected = _select(fields, _FIELDS, strict=True)
                line = (
                    json.dumps(
                        {
                            "schema": "wsorb.diagnostics",
                            "schema_version": 1,
                            "event": event,
                            "wall_time_ms": int(time.time() * 1000),
                            "monotonic_s": now - self._started,
                            **selected,
                        },
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                    self.dropped += 1
                    return False
                self.events += 1
                self._window_events += 1
                try:
                    target = sys.stderr if self.sink is None else self.sink
                    target.write(line)
                    target.flush()
                except BaseException:
                    # Even an interrupted/broken optional sink cannot change an RPC outcome.
                    self.sink_failures += 1
                    return False
                return True
        except BaseException:
            return False


_OVERRIDE: ContextVar[Diagnostics | bool | None] = ContextVar("wsorb_diagnostics", default=None)
_ENV_LOGGER = Diagnostics()


def _logger() -> Diagnostics | None:
    override = _OVERRIDE.get()
    if isinstance(override, Diagnostics):
        return override
    if override is False:
        return None
    return _ENV_LOGGER if os.environ.get("WSORB_DIAGNOSTICS") == "1" else None


def enabled() -> bool:
    return _logger() is not None


def enable(**options: Any) -> Diagnostics:
    """Enable in this context. Defaults to stderr, with hard event/rate limits."""
    logger = Diagnostics(**options)
    _OVERRIDE.set(logger)
    return logger


def disable() -> None:
    """Disable in this context, including when the environment flag is set."""
    _OVERRIDE.set(False)


@contextmanager
def diagnostics_context(enabled: bool = True, **options: Any) -> Iterator[Diagnostics | None]:
    """Temporarily enable or disable, then restore the prior feature choice."""
    if type(enabled) is not bool:
        raise ValueError("Diagnostic feature choice must be a boolean")
    logger = Diagnostics(**options) if enabled else None
    token = _OVERRIDE.set(logger if enabled else False)
    try:
        yield logger
    finally:
        _OVERRIDE.reset(token)


def emit(event: str, **fields: Any) -> bool:
    logger = _logger()
    if logger is None:
        return False
    return logger.emit(event, **fields)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def public_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Select public clocks, mission, owner, actual Rapier pose and optional visual reports."""
    observation = _mapping(value.get("observation"))
    runtime = _mapping(value.get("runtime"))
    mission = _mapping(observation.get("mission"))
    optional = _mapping(value.get("diagnostics"))
    camera = _select(optional.get("camera"), _CAMERA)
    presentation = _select(optional.get("presentation"), _PRESENTATION)
    fields = {
        **_select(observation, {"time_s": "number", "navigation_sample_time_s": "number"}),
        **_select(runtime, {"running": "bool", "rate": "number"}),
        "range_m": mission.get("target_distance_m"),
        "relative_speed_mps": mission.get("relative_speed_mps"),
        "mission_status": mission.get("status"),
        "owner": _select(observation.get("control_authority"), _OWNER),
        "lander": _select(observation.get("applied_physics"), _POSE),
        "renderer": _select(value.get("renderer"), _RENDERER),
        "page_id_available": _valid(optional.get("page_id"), "uuid"),
        "camera_available": (
            bool(camera)
            and _mapping(value.get("renderer")).get("started") is True
            and optional.get("camera_available") is not False
            and optional.get("camera_status_available") is not False
            and camera.get("available") is not False
        ),
        "presentation_available": presentation.get("available") is True,
        "camera": camera,
        "presentation": presentation,
        "page_id": optional.get("page_id"),
        **_select(
            optional,
            {
                "camera_status_available": "bool",
                "presentation_diagnostics_available": "bool",
                "native_collection_available": "bool",
            },
        ),
    }
    return _select(fields, _SNAPSHOT)


def _error_fields(error: BaseException) -> dict[str, str]:
    name = type(error).__name__
    code = getattr(error, "code", None)
    return {
        "error_type": name if name in _ERROR_TYPES else "Exception",
        "code": code if type(code) is str and code in _CODES else "unknown_outcome",
    }


@dataclass(frozen=True, slots=True)
class RequestTrace:
    logger: Diagnostics
    request_id: str
    started: float
    fields: dict[str, Any]


def start_request(
    transport: str,
    stage: str,
    payload: Mapping[str, Any] | None = None,
    *,
    method: str | None = None,
    request_id: str | None = None,
    parent: RequestTrace | None = None,
) -> RequestTrace | None:
    logger = _logger()
    if logger is None:
        return None
    try:
        if parent is not None:
            fields = {**parent.fields, "stage": stage}
            request_id = parent.request_id
        else:
            payload = _mapping(payload)
            fields = _select(
                {"transport": transport, "stage": stage, "method": method or payload.get("method")},
                _FIELDS,
            )
            command = _mapping(payload.get("command"))
            fields.update(
                _select(command, {"op": _OPS, "source_time_s": "number", "lease_sim_s": "number"})
            )
            fence = _mapping(command.get("controller_fence"))
            fields.update(
                _select(
                    {
                        "fence_epoch": fence.get("epoch", command.get("expected_epoch")),
                        "fence_term": fence.get("term", command.get("expected_term")),
                    },
                    _FIELDS,
                )
            )
        request_id = str(uuid.uuid4()) if request_id is None else request_id
        if not _valid(request_id, "uuid"):
            return None
        fields["request_id"] = request_id
        trace = RequestTrace(logger, request_id, time.monotonic(), fields)
        logger.emit("request_start", **fields)
        return trace
    except BaseException:
        return None


def request_headers(trace: RequestTrace | None) -> dict[str, str]:
    if trace is None:
        return {}
    return {"X-WSORB-Diagnostics": "1", "X-WSORB-Request-ID": trace.request_id}


def finish_request(
    trace: RequestTrace | None,
    *,
    response: Any = None,
    value: Any = None,
    error: BaseException | None = None,
    http_status: int | None = None,
) -> None:
    if trace is None:
        return
    try:
        fields = {**trace.fields, "elapsed_s": time.monotonic() - trace.started}
        if http_status is not None:
            fields["http_status"] = http_status
        if error is not None:
            fields.update(_error_fields(error))
            fields["outcome"] = (
                "rejected_unapplied" if fields["code"] in _KNOWN_UNAPPLIED else "unknown"
            )
            fields["result_kind"] = "unavailable"
        elif fields.get("transport") == "room" and fields.get("stage") == "http":
            # HTTP receipt is not a validated owner RPC or an engine ACK.
            fields.update(outcome="received", result_kind="envelope")
        elif fields.get("method") == "status":
            fields.update(
                outcome="received", result_kind="status", status=_select(response, _STATUS)
            )
        else:
            if fields.get("transport") == "local":
                result = _mapping(response)
                if result.get("ok") is not True or not isinstance(result.get("value"), dict):
                    code = result.get("code")
                    code = code if type(code) is str and code in _CODES else "unknown_outcome"
                    fields.update(
                        code=code,
                        result_kind="unavailable",
                        outcome=("rejected_unapplied" if code in _KNOWN_UNAPPLIED else "unknown"),
                    )
                    trace.logger.emit("request_end", **fields)
                    return
                value = result["value"]
            kind = {"snapshot": "snapshot", "command": "ack"}.get(fields.get("method"), "response")
            fields.update(result_kind=kind, outcome="acknowledged" if kind == "ack" else "received")
            if kind == "snapshot":
                fields["snapshot"] = public_snapshot(_mapping(value))
        trace.logger.emit("request_end", **fields)
    except BaseException:
        pass


class DiagnosticsError(RuntimeError):
    """Fixed diagnostic codes only. Exception text is never emitted."""

    def __init__(self, code: str) -> None:
        super().__init__("Diagnostics unavailable")
        self.code = code


@dataclass(frozen=True, slots=True)
class DiagnosticsOptions:
    connection_file: Path | None = None
    room_file: Path | None = None
    duration_s: float = 5.0
    cadence_hz: float = 1.0
    output: Path | None = None

    def __post_init__(self) -> None:
        if (self.connection_file is None) == (self.room_file is None):
            raise DiagnosticsError("invalid_options")
        for name in ("connection_file", "room_file", "output"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, Path)) or not str(value):
                    raise DiagnosticsError("invalid_options")
                object.__setattr__(self, name, Path(value))
        for name, maximum in (("duration_s", MAX_DURATION_S), ("cadence_hz", MAX_CADENCE_HZ)):
            value = getattr(self, name)
            if not _number(value) or not 0 < value <= maximum:
                raise DiagnosticsError("invalid_options")
            object.__setattr__(self, name, float(value))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DiagnosticsError("invalid_arguments")


def parse_args(argv: Sequence[str] | None = None) -> DiagnosticsOptions:
    parser = _Parser(
        prog="wsorb diagnostics",
        allow_abbrev=False,
        description="Read-only status and fresh public snapshots. Never claim, Play or restart.",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--connection-file", type=Path, help="explicit private local descriptor")
    scope.add_argument("--room-file", type=Path, help="explicit private hosted room descriptor")
    parser.add_argument("--duration-s", type=float, default=5.0, help="wall budget, at most 30 s")
    parser.add_argument("--cadence-hz", type=float, default=1.0, help="default 1 Hz, at most 2 Hz")
    parser.add_argument("--output", type=Path, help="new private JSONL file; never overwrite")
    return DiagnosticsOptions(**vars(parser.parse_args(argv)))


@contextmanager
def _output_file(path: Path) -> Iterator[TextIO]:
    if os.name != "posix":
        raise DiagnosticsError("private_platform")
    fd = None
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        os.fchmod(fd, 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise DiagnosticsError("output_unavailable")
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            fd = None
            yield target
    except OSError:
        raise DiagnosticsError("output_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


def _collect(options: DiagnosticsOptions, logger: Diagnostics) -> dict[str, Any]:
    # These imports are deliberately outside the portable SDK and policy modules.
    from .local_api import LocalBusyError, LocalSimulation
    from .room_api import RoomSimulation

    started = time.monotonic()
    deadline = started + options.duration_s
    counts = {"sample_count": 0, "skip_count": 0, "error_count": 0}
    transport = "room" if options.room_file is not None else "local"
    common = {"transport": transport, "stage": "collector"}
    logger.emit(
        "collector_start",
        **common,
        duration_s=options.duration_s,
        cadence_hz=options.cadence_hz,
        actuation_permitted=False,
    )
    try:
        if os.name != "posix":
            raise DiagnosticsError("private_platform")
        timeout = min(2.0, options.duration_s)
        client = (
            RoomSimulation.from_file(options.room_file, timeout_s=timeout)
            if options.room_file is not None
            else LocalSimulation.from_file(options.connection_file, timeout_s=timeout)
        )
        next_tick = started
        for _ in range(math.ceil(options.duration_s * options.cadence_hz)):
            time.sleep(max(0.0, min(next_tick, deadline) - time.monotonic()))
            if time.monotonic() >= deadline:
                break
            client.timeout_s = min(2.0, deadline - time.monotonic())
            try:
                skip = None
                if transport == "local":
                    status = client.status()
                    logger.emit(
                        "status", **common, status_available=True, status=_select(status, _STATUS)
                    )
                    skip = _local_skip(status)
                else:
                    # Hosted status would read saved state, not the connected owner's measurement.
                    logger.emit(
                        "status", **common, status_available=False, code="status_unavailable"
                    )
                if skip is not None:
                    counts["skip_count"] += 1
                    logger.emit("skip", **common, code=skip)
                elif time.monotonic() < deadline:
                    client.timeout_s = min(2.0, deadline - time.monotonic())
                    snapshot = client.snapshot()
                    logger.emit("sample", **common, snapshot=public_snapshot(snapshot))
                    counts["sample_count"] += 1
            except LocalBusyError:
                counts["skip_count"] += 1
                logger.emit("skip", **common, code="local_busy")
            # Do not catch up missed slots or hammer a busy owner after a slow call.
            next_tick = time.monotonic() + 1 / options.cadence_hz
    except (Exception, KeyboardInterrupt) as error:
        counts["error_count"] += 1
        logger.emit("error", **common, **_error_fields(error))
        # Unknown failures end collection. No immediate retry, fallback or actuation.
    summary = {
        **counts,
        "elapsed_s": time.monotonic() - started,
        "dropped_events": logger.dropped,
        "sink_failures": logger.sink_failures,
        "actuation_permitted": False,
    }
    logger.emit("summary", **common, **summary)
    summary["sink_failures"] = logger.sink_failures
    return summary


def _local_skip(status: Mapping[str, Any]) -> str | None:
    if status.get("busy") is True:
        return "local_busy"
    if status.get("backend") == "native_headless":
        return None if status.get("physics_ready") is True else "physics_unavailable"
    return None if status.get("connected") is True else "owner_unavailable"


def collect(options: DiagnosticsOptions) -> dict[str, Any]:
    """Read one explicit existing transport. Only an explicitly selected log is written."""
    if not isinstance(options, DiagnosticsOptions):
        raise DiagnosticsError("invalid_options")
    output = _output_file(options.output) if options.output is not None else nullcontext(sys.stdout)
    with output as target, diagnostics_context(sink=target) as logger:
        return _collect(options, logger)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        summary = collect(parse_args(argv))
        return 1 if summary["error_count"] or summary["sink_failures"] else 0
    except (Exception, KeyboardInterrupt) as error:
        with diagnostics_context(sink=sys.stdout) as logger:
            logger.emit("error", stage="collector", **_error_fields(error))
            logger.emit(
                "summary", sample_count=0, skip_count=0, error_count=1, actuation_permitted=False
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
