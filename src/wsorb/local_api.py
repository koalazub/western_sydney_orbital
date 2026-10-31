"""Python access to the real local WASM simulation, not the STP SDK.

Start the built game with ``WSORB_LAB=1 just --no-deps local`` and open its
printed URL in a WebGPU browser. The loopback server relays requests to that
browser's existing WASM instance. With ``WSORB_HEADLESS=1`` as well, the private
server runs that same built engine's native physics/runtime without a renderer.

Use one command writer. Raw ``set_command`` writes are latched until replaced or stopped;
``run_step`` instead pauses, advances a bounded interval and stops in ``finally``.
Autonomous decisions can use ``set_leased_command`` with their original snapshot.
The native lease expires at that snapshot's time plus the requested interval.
No request is retried, since a lost response does not prove a command failed.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import stat
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import numpy as np

from . import diagnostics
from .flight.control import reference_to_body

DEFAULT_CONNECTION = Path(".devenv/wsorb-local.json")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_STEP_SECONDS = 1.0
MAX_DEBRIS_COUNT = 100_000
WORLD_POSITION_LIMIT_M = 10_000_000.0  # Versioned local scenario transport bound.
WORLD_CALL_TIMEOUT_S = 180.0
WORLD_OPERATIONS = frozenset({"configure_experiment", "configure_flight", "reset"})
FLIGHT_PRESETS = ("mars_facing_l1", "far_side_l2")
COMMANDS = frozenset(
    {
        "pause",
        "play",
        "step",
        "reset",
        "set_command",
        "set_leased_command",
        "claim_control",
        "renew_control",
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
    }
)


class LocalApiError(RuntimeError):
    """The local transport or actual WASM engine rejected a request."""


class LocalBusyError(LocalApiError):
    """The private server rejected a contended call before engine dispatch."""

    code = "local_busy"


class LocalPageMismatchError(LocalApiError):
    """HTTP rejected a changed/missing target page before dispatch, not a native error."""

    code = "page_mismatch"


class StaleContextError(LocalApiError):
    """The engine rejected a checked command before changing the current flight."""

    code = "stale_context"


class CommandExpiredError(LocalApiError):
    """The native decision lease expired before invocation; no command was applied."""

    code = "command_expired"


class SensorUnavailableError(LocalApiError):
    """A real capture was unavailable or had no actual sample to deliver."""

    def __init__(self, message: str, *, code: str = "sensor_unavailable") -> None:
        if code not in ("sensor_unavailable", "sensor_no_sample"):
            raise ValueError("Unsupported sensor rejection code")
        super().__init__(message)
        self.code = code


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the local bearer token to a redirect target.
        return None


def _response_page_id(headers: Message) -> str | None:
    """Parse only the transport receipt. Body diagnostics and status IDs cannot bind."""
    values = headers.get_all("X-WSORB-Page", [])
    if not values:
        return None
    if (
        len(values) != 1
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            values[0],
            re.IGNORECASE,
        )
        is None
    ):
        raise LocalApiError(
            "Local simulator returned an invalid page receipt. Command outcome may be unknown; "
            "do not automatically retry actuation."
        )
    return values[0].lower()


class _PageBinding:
    """Receipt/request policy only. Observing a page never grants control or GPU readiness."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_observed_page_id: str | None = None
        self._bound_page_id: str | None = None

    @property
    def last_observed_page_id(self) -> str | None:
        with self._lock:
            return self._last_observed_page_id

    @property
    def bound_page_id(self) -> str | None:
        with self._lock:
            return self._bound_page_id

    def request_headers(self) -> dict[str, str]:
        with self._lock:
            return (
                {} if self._bound_page_id is None else {"X-WSORB-Target-Page": self._bound_page_id}
            )

    def observe(self, headers: Message, *, successful_snapshot: bool = False) -> None:
        page_id = _response_page_id(headers)
        with self._lock:
            if self._bound_page_id is not None and page_id != self._bound_page_id:
                # A post-dispatch receipt failure does not prove non-application.
                # Only the server's explicit HTTP 409 page_mismatch does that.
                raise LocalApiError(
                    "Local simulator did not confirm the bound page. Command outcome may be "
                    "unknown; do not automatically retry actuation."
                )
            if successful_snapshot:
                self._last_observed_page_id = page_id

    def bind(self, *, require: bool = False) -> str | None:
        if type(require) is not bool:
            raise ValueError("require must be a boolean")
        with self._lock:
            if self._bound_page_id is None:
                if require and self._last_observed_page_id is None:
                    raise LocalApiError("No verified snapshot page receipt is available to bind.")
                self._bound_page_id = self._last_observed_page_id
            return self._bound_page_id


class _TransportChannels:
    """Serialize real calls across notebook cells and the guidance worker."""

    def __init__(self) -> None:
        self.control = threading.RLock()
        self.capture = threading.RLock()


_CHANNELS_LOCK = threading.Lock()
_CHANNELS: weakref.WeakValueDictionary[str, _TransportChannels] = weakref.WeakValueDictionary()


def _channels(url: str) -> _TransportChannels:
    with _CHANNELS_LOCK:
        channels = _CHANNELS.get(url)
        if channels is None:
            channels = _TransportChannels()
            _CHANNELS[url] = channels
        return channels


def _bounded_number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number between {low} and {high}")
    if not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number between {low} and {high}")
    return float(value)


def _vector(value: Sequence[float], name: str) -> list[float]:
    array = np.asarray(value)
    if array.shape != (3,) or array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite real vector of shape (3,)")
    with np.errstate(over="ignore", invalid="ignore"):
        result = array.astype(float)
    if not np.isfinite(result).all():
        raise ValueError(f"{name} must be a finite real vector of shape (3,)")
    return result.tolist()


def _integer(value: Any, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be a whole number from 0 to {maximum}")
    return value


def validate_position(position_m: Sequence[float]) -> list[float]:
    """Validate XYZ against the local world's full bound, never a launch-pad clamp."""
    position = _vector(position_m, "position_m")
    if any(abs(component) > WORLD_POSITION_LIMIT_M for component in position):
        raise ValueError("position_m must stay within the local +/-10000000 m world bound")
    return position


def validate_surface_direction(value: Sequence[float]) -> list[float]:
    """Normalize a surface direction without assuming spherical terrain."""
    direction = _vector(value, "surface_direction")
    scale = max(abs(component) for component in direction)
    if scale == 0:
        raise ValueError("surface_direction must be a nonzero finite three-vector")
    scaled = [component / scale for component in direction]
    length = math.hypot(*scaled)
    return [component / length for component in scaled]


def _distribution(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Debris distribution must be a typed mapping")
    kind = value.get("kind")
    if (
        isinstance(kind, str)
        and kind in ("directed_stream", "transit_stream")
        and set(value) == {"kind"}
    ):
        return {"kind": kind}
    if kind != "impact_ejecta" or set(value) != {"kind", "settings"}:
        raise ValueError(
            "Debris distribution must be directed_stream, transit_stream or impact_ejecta"
        )
    settings = value["settings"]
    fields = {
        "source_direction",
        "kick_velocity_mps",
        "tangential_speed_mps",
        "patch_half_angle_deg",
    }
    if not isinstance(settings, Mapping) or set(settings) != fields:
        raise ValueError(
            "Impact ejecta requires exactly source, kick, tangential speed and patch angle"
        )
    direction = _vector(settings["source_direction"], "source_direction")
    if not any(component != 0 for component in direction):
        raise ValueError("source_direction must be nonzero")
    kick = _vector(settings["kick_velocity_mps"], "kick_velocity_mps")
    if math.hypot(*kick) > 200.0:
        raise ValueError("kick_velocity_mps must have speed at most 200 m/s")
    tangential = _bounded_number(settings["tangential_speed_mps"], "tangential_speed_mps", 0, 200)
    angle = _bounded_number(settings["patch_half_angle_deg"], "patch_half_angle_deg", 0, 180)
    if angle == 0:
        raise ValueError("patch_half_angle_deg must be greater than zero")
    return {
        "kind": kind,
        "settings": {
            "source_direction": direction,
            "kick_velocity_mps": kick,
            "tangential_speed_mps": tangential,
            "patch_half_angle_deg": angle,
        },
    }


def validate_experiment_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Parse setup data without I/O. Omitted distributions keep legacy directed_stream.

    An omitted trajectory adjustment stays absent; explicit off normalizes to
    absence, matching native serialization without changing archived settings.
    """
    if not isinstance(settings, Mapping) or set(settings) != {"ship", "debris"}:
        raise ValueError("Experiment settings require exactly ship and debris")
    ship, debris = settings["ship"], settings["debris"]
    fields = {
        "count",
        "radius_min_m",
        "radius_max_m",
        "speed_mps",
        "speed_spread_mps",
        "shell_inner_m",
        "shell_outer_m",
        "stream_axis",
        "stream_sign",
    }
    if not isinstance(ship, Mapping) or set(ship) != {"mass_kg", "inertia_scale"}:
        raise ValueError("Experiment ship requires mass_kg and inertia_scale")
    if (
        not isinstance(debris, Mapping)
        or not fields.issubset(debris)
        or set(debris) - fields - {"distribution", "trajectory_adjustment"}
    ):
        raise ValueError("Experiment debris fields do not match the versioned local schema")
    parsed_ship = {
        "mass_kg": _bounded_number(ship["mass_kg"], "mass_kg", 100, 100_000),
        "inertia_scale": _bounded_number(ship["inertia_scale"], "inertia_scale", 0.1, 10),
    }
    minimum = _bounded_number(debris["radius_min_m"], "radius_min_m", 0.05, 100)
    maximum = _bounded_number(debris["radius_max_m"], "radius_max_m", minimum, 100)
    speed = _bounded_number(debris["speed_mps"], "speed_mps", 0.1, 200)
    spread = _bounded_number(debris["speed_spread_mps"], "speed_spread_mps", 0, speed / 2)
    inner = _bounded_number(debris["shell_inner_m"], "shell_inner_m", 0, 49_990)
    outer = _bounded_number(debris["shell_outer_m"], "shell_outer_m", 10, 50_000)
    if outer - inner < 10:
        raise ValueError("Debris shell must be at least 10 m thick")
    if debris["stream_axis"] not in ("x", "y", "z") or type(debris["stream_sign"]) is not int:
        raise ValueError("Flow direction needs stream_axis x/y/z and stream_sign -1/1")
    if debris["stream_sign"] not in (-1, 1):
        raise ValueError("Flow direction needs stream_axis x/y/z and stream_sign -1/1")
    parsed = {
        "ship": parsed_ship,
        "debris": {
            "count": _integer(debris["count"], "debris.count", MAX_DEBRIS_COUNT),
            "radius_min_m": minimum,
            "radius_max_m": maximum,
            "speed_mps": speed,
            "speed_spread_mps": spread,
            "shell_inner_m": inner,
            "shell_outer_m": outer,
            "stream_axis": debris["stream_axis"],
            "stream_sign": debris["stream_sign"],
            "distribution": _distribution(debris.get("distribution", {"kind": "directed_stream"})),
        },
    }
    if "trajectory_adjustment" in debris:
        adjustment = debris["trajectory_adjustment"]
        if not isinstance(adjustment, str) or adjustment not in ("off", "toward_lander"):
            raise ValueError("trajectory_adjustment must be off or toward_lander")
        if adjustment != "off":
            parsed["debris"]["trajectory_adjustment"] = adjustment
    return parsed


def control_context(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Build local atomic-control metadata from an actual public receipt, not defaults."""
    if not isinstance(snapshot, Mapping):
        raise ValueError("Checked command requires the actual public observation and runtime")
    observation, runtime = snapshot.get("observation"), snapshot.get("runtime")
    if not isinstance(observation, Mapping) or not isinstance(runtime, Mapping):
        raise ValueError("Checked command requires the actual public observation and runtime")
    running = runtime.get("running")
    if type(running) is not bool:
        raise ValueError("Checked command requires actual running playback state")
    context = {
        name: _integer(observation.get(name, runtime.get(name)), name, 0xFFFFFFFF)
        for name in ("trial_revision", "tuning_revision", "manual_interventions")
    }
    context["seed"] = _integer(observation.get("seed"), "seed", 0xFFFFFFFF)
    context["running"] = running
    return context


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        raise ValueError(
            f"{name} must contain 1 to 64 ASCII letters, digits, underscores or hyphens"
        )
    return value


def _source_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("source_time_s must be a finite non-negative number")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("source_time_s must be a finite non-negative number") from error
    if not math.isfinite(result):
        raise ValueError("source_time_s must be a finite non-negative number")
    return result


def _expected_context(context: Any) -> dict[str, Any]:
    fields = ("trial_revision", "tuning_revision", "manual_interventions", "seed")
    if not isinstance(context, Mapping) or set(context) != {*fields, "running"}:
        raise ValueError("Checked commands require the complete five-field expected_context")
    if type(context["running"]) is not bool:
        raise ValueError("expected_context.running must be a boolean")
    return {
        **{name: _integer(context[name], name, 0xFFFFFFFF) for name in fields},
        "running": context["running"],
    }


def _controller_fence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "epoch",
        "term",
        "run_id",
        "controller_id",
    }:
        raise ValueError("controller_fence requires exactly epoch, term, run_id and controller_id")
    return {
        "epoch": _integer(value["epoch"], "controller_fence.epoch", 0xFFFFFFFF),
        "term": _integer(value["term"], "controller_fence.term", 0xFFFFFFFF),
        "run_id": _identifier(value["run_id"], "run_id"),
        "controller_id": _identifier(value["controller_id"], "controller_id"),
    }


def _control_authority(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    observation = snapshot.get("observation") if isinstance(snapshot, Mapping) else None
    authority = observation.get("control_authority") if isinstance(observation, Mapping) else None
    if not isinstance(authority, Mapping) or set(authority) != {
        "epoch",
        "term",
        "status",
        "run_id",
        "controller_id",
        "expires_at_sim_time_s",
    }:
        raise ValueError("Control ownership requires actual observation.control_authority")
    parsed = {
        "epoch": _integer(authority["epoch"], "control_authority.epoch", 0xFFFFFFFF),
        "term": _integer(authority["term"], "control_authority.term", 0xFFFFFFFF),
    }
    if authority["status"] not in ("idle", "active", "expired", "revoked"):
        raise ValueError("Unknown control_authority.status")
    parsed["status"] = authority["status"]
    for name in ("run_id", "controller_id"):
        value = authority[name]
        parsed[name] = None if value is None else _identifier(value, name)
    expiry = authority["expires_at_sim_time_s"]
    parsed["expires_at_sim_time_s"] = None if expiry is None else _source_time(expiry)
    if parsed["status"] == "active" and any(
        parsed[name] is None for name in ("run_id", "controller_id", "expires_at_sim_time_s")
    ):
        raise ValueError("Active control authority requires actual owner IDs and expiry")
    return parsed


def controller_fence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Read an active native fence from a real receipt, never from a claim request."""
    authority = _control_authority(snapshot)
    if authority["status"] != "active":
        raise ValueError("The actual control authority is not active")
    return _controller_fence(
        {name: authority[name] for name in ("epoch", "term", "run_id", "controller_id")}
    )


def _ownership_command(request: Mapping[str, Any]) -> dict[str, Any]:
    op = request.get("op")
    if op == "renew_control":
        if set(request) != {
            "op",
            "controller_fence",
            "source_time_s",
            "lease_sim_s",
            "expected_context",
            "expected_sensor_revision",
        }:
            raise ValueError(
                "renew_control requires the exact fence, original source and both contexts"
            )
        return {
            "op": op,
            "controller_fence": _controller_fence(request["controller_fence"]),
            "source_time_s": _source_time(request["source_time_s"]),
            "lease_sim_s": _bounded_number(request["lease_sim_s"], "lease_sim_s", 0.05, 0.75),
            "expected_context": _expected_context(request["expected_context"]),
            "expected_sensor_revision": _integer(
                request["expected_sensor_revision"], "expected_sensor_revision", 0xFFFFFFFF
            ),
        }
    fields = {"op", "run_id", "expected_epoch", "expected_context"}
    if op == "claim_control":
        fields |= {"controller_id", "expected_term", "source_time_s", "lease_sim_s"}
    elif op != "release_control":
        raise ValueError("Unsupported ownership command")
    if set(request) != fields:
        raise ValueError(f"{op} requires exactly {', '.join(sorted(fields))}")
    parsed = {
        "op": op,
        "run_id": _identifier(request["run_id"], "run_id"),
        "expected_epoch": _integer(request["expected_epoch"], "expected_epoch", 0xFFFFFFFF),
        "expected_context": _expected_context(request["expected_context"]),
    }
    if op == "claim_control":
        parsed.update(
            controller_id=_identifier(request["controller_id"], "controller_id"),
            expected_term=_integer(request["expected_term"], "expected_term", 0xFFFFFFFF),
            source_time_s=_source_time(request["source_time_s"]),
            lease_sim_s=_bounded_number(request["lease_sim_s"], "lease_sim_s", 0.05, 1.0),
        )
    return parsed


def ownership_renewal_supported(snapshot: Mapping[str, Any]) -> bool:
    """Require an explicit native advertisement, never a client or transport version."""
    observation = snapshot.get("observation") if isinstance(snapshot, Mapping) else None
    capabilities = (
        observation.get("control_capabilities") if isinstance(observation, Mapping) else None
    )
    renewal = capabilities.get("ownership_renewal") if isinstance(capabilities, Mapping) else None
    return (
        isinstance(renewal, Mapping)
        and type(renewal.get("schema_version")) is int
        and renewal["schema_version"] == 1
        and type(renewal.get("max_lease_sim_s")) in (int, float)
        and renewal["max_lease_sim_s"] == 0.75
    )


def _control_renewal_request(snapshot, *, controller_fence, lease_sim_s):
    if not ownership_renewal_supported(snapshot):
        raise ValueError("This native source does not advertise ownership renewal support")
    observation = snapshot.get("observation") if isinstance(snapshot, Mapping) else None
    health = observation.get("sensor_health") if isinstance(observation, Mapping) else None
    if (
        not isinstance(health, Mapping)
        or type(health.get("schema_version")) is not int
        or health["schema_version"] != 1
    ):
        raise ValueError("Ownership renewal requires actual schema-version-1 sensor_health")
    fence = _controller_fence(controller_fence)
    authority = _control_authority(snapshot)
    if authority["status"] != "active" or any(
        authority[name] != value for name, value in fence.items()
    ):
        raise ValueError("Ownership renewal requires this source's actual active owner fence")
    return _ownership_command(
        {
            "op": "renew_control",
            "controller_fence": fence,
            "source_time_s": observation.get("time_s"),
            "lease_sim_s": lease_sim_s,
            "expected_context": control_context(snapshot),
            "expected_sensor_revision": health.get("revision"),
        }
    )


def _resilience_command(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the bounded delivery/lease operations before any transport I/O."""
    op = request.get("op")
    if op == "clear_sensor_faults":
        if set(request) != {"op"}:
            raise ValueError("clear_sensor_faults requires only op")
        return {"op": op}
    if op == "set_sensor_fault":
        required = {"op", "target", "mode"}
        if not required.issubset(request) or set(request) - required - {"duration_sim_s"}:
            raise ValueError("Sensor faults require op, target, mode and optional duration_sim_s")
        target, mode = request["target"], request["mode"]
        if not isinstance(target, str) or target not in (
            "radar_primary",
            "radar_backup",
            "camera_top",
            "camera_bottom",
            "radar_bus",
            "navigation",
            "all_avionics",
        ):
            raise ValueError("Unknown sensor fault target")
        if not isinstance(mode, str) or mode not in ("nominal", "unavailable", "frozen"):
            raise ValueError("Sensor fault mode must be nominal, unavailable or frozen")
        parsed = {"op": op, "target": target, "mode": mode}
        if "duration_sim_s" in request:
            parsed["duration_sim_s"] = _bounded_number(
                request["duration_sim_s"], "duration_sim_s", 0.05, 300.0
            )
        elif mode != "nominal":
            raise ValueError(
                "Non-nominal sensor faults require duration_sim_s between 0.05 and 300"
            )
        return parsed
    if op != "set_leased_command":
        raise ValueError("Unsupported resilience command")
    required = {
        "op",
        "accel",
        "angular_accel",
        "lease_sim_s",
        "source_time_s",
        "expected_context",
        "expected_sensor_revision",
    }
    if not required.issubset(request) or set(request) - required - {"controller_fence"}:
        raise ValueError(
            "Leased commands require exact vectors, lease, source time and both contexts"
        )
    parsed_context = _expected_context(request["expected_context"])
    source = _source_time(request["source_time_s"])
    fence = (
        _controller_fence(request["controller_fence"]) if "controller_fence" in request else None
    )
    vectors = {}
    for name, limit in (("accel", 0.5), ("angular_accel", 0.05)):
        vector = _vector(request[name], name)
        if any(isinstance(component, (bool, np.bool_)) for component in request[name]):
            raise ValueError(f"{name} must be a finite real vector without boolean components")
        if math.hypot(*vector) > limit:
            raise ValueError(f"{name} must have vector norm at most {limit}")
        vectors[name] = vector
    return {
        "op": op,
        **vectors,
        "lease_sim_s": _bounded_number(request["lease_sim_s"], "lease_sim_s", 0.05, 1.0),
        "source_time_s": source,
        "expected_context": parsed_context,
        "expected_sensor_revision": _integer(
            request["expected_sensor_revision"], "expected_sensor_revision", 0xFFFFFFFF
        ),
        **({"controller_fence": fence} if fence is not None else {}),
    }


def _leased_command_request(
    accel: Sequence[float],
    angular_accel: Sequence[float],
    snapshot: Mapping[str, Any],
    *,
    lease_sim_s: float,
    controller_fence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    context = control_context(snapshot)
    observation = snapshot["observation"]
    health = observation.get("sensor_health")
    if (
        not isinstance(health, Mapping)
        or type(health.get("schema_version")) is not int
        or health["schema_version"] != 1
    ):
        raise ValueError("Leased commands require actual schema-version-1 sensor_health")
    return _resilience_command(
        {
            "op": "set_leased_command",
            "accel": accel,
            "angular_accel": angular_accel,
            "lease_sim_s": lease_sim_s,
            "source_time_s": observation.get("time_s"),
            "expected_context": context,
            "expected_sensor_revision": health.get("revision"),
            **({"controller_fence": controller_fence} if controller_fence is not None else {}),
        }
    )


def _trial_revision(snapshot: Mapping[str, Any]) -> int:
    observation, runtime = snapshot.get("observation", {}), snapshot.get("runtime", {})
    return _integer(
        observation.get("trial_revision", runtime.get("trial_revision")),
        "trial_revision",
        0xFFFFFFFF,
    )


def placement_readback_matches(snapshot: Mapping[str, Any], position_m: Sequence[float]) -> bool:
    """Compare real Rapier pose/motion and current requests, never an ACK echo."""
    try:
        expected = validate_position(position_m)
        if snapshot["runtime"]["running"] is not False:
            return False
        physics = snapshot["observation"]["applied_physics"]
        actual = _vector(physics["position_world_m"], "actual position")
        if not np.allclose(actual, expected, rtol=0, atol=1e-6):
            return False
        for field in (
            "velocity_world_mps",
            "angular_velocity_world_rad_s",
            "total_external_force_world_n",
            "total_external_torque_world_nm",
        ):
            if any(abs(component) > 1e-9 for component in _vector(physics[field], field)):
                return False
        for field in ("accel_body_mps2", "angular_accel_body_rad_s2"):
            if any(component != 0 for component in _vector(physics["requested"][field], field)):
                return False
        rotation = np.asarray(physics["rotation_body_to_world_xyzw"])
        if rotation.shape != (4,) or rotation.dtype.kind not in "iuf":
            return False
        inverse = np.array(rotation, dtype=np.float64, copy=True)
        inverse[:3] *= -1
        up = reference_to_body([0, 0, 1], inverse, "active_xyzw")
        radius = math.hypot(*actual)
        return radius > 0 and float(np.dot(up, np.asarray(actual) / radius)) >= 1 - 1e-8
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be a finite number between 0 and 1")
    if not math.isfinite(value) or not 0 <= value <= MAX_STEP_SECONDS:
        raise ValueError("seconds must be a finite number between 0 and 1")
    return float(value)


def _connection(value: Any) -> tuple[str, str]:
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise LocalApiError("Unsupported local connection file version")
    url, token = value.get("url"), value.get("token")
    if not isinstance(url, str) or not isinstance(token, str):
        raise LocalApiError("Invalid local connection file")
    try:
        parsed = urlsplit(url)
        valid_url = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.port is not None
            and 1 <= parsed.port <= 65535
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid_url = False
    if not valid_url or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise LocalApiError("Connection must use an authenticated 127.0.0.1 HTTP endpoint")
    return url.rstrip("/"), token


class LocalSimulation:
    """A synchronous, authenticated client for one real local WASM engine."""

    topCamera = "top"
    bottomCamera = "bottom"

    def __init__(self, url: str, token: str, *, timeout_s: float = 8.0):
        self.url, self._token = _connection({"version": 1, "url": url, "token": token})
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 30:
            raise ValueError("timeout_s must be between 0 and 30")
        self.timeout_s = timeout_s
        self._channels = _channels(self.url)
        self._page_binding = _PageBinding()
        # Ignore proxy environment variables for this loopback-only connection.
        self._http = build_opener(ProxyHandler({}), _NoRedirects())

    @classmethod
    def from_file(
        cls, path: str | Path | None = None, *, timeout_s: float = 8.0
    ) -> LocalSimulation:
        """Read a private connection file. This does not send any command."""
        path = Path(path or os.environ.get("WSORB_LOCAL_CONNECTION", DEFAULT_CONNECTION))
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192:
                raise LocalApiError(
                    "Local connection file must be a private regular file, mode 0600"
                )
            url, token = _connection(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise LocalApiError(
                f"Cannot read {path}. Start the game with WSORB_LAB=1 and connect again."
            ) from error
        return cls(url, token, timeout_s=timeout_s)

    @property
    def last_observed_page_id(self) -> str | None:
        """The last successful snapshot's verified transport UUID, or None for no receipt."""
        return self._page_binding.last_observed_page_id

    @property
    def bound_page_id(self) -> str | None:
        """The immutable target UUID, or None when page pinning has not occurred."""
        return self._page_binding.bound_page_id

    def bind_to_observed_page(self, *, require: bool = False) -> str | None:
        """Pin only a successful snapshot receipt, without I/O or accepting a guessed UUID.

        Returns the bound UUID, or None for legacy/no-header/native-headless servers
        when require=False. A bound client never adopts another page. This is only
        transport identity, not a control grant or proof that a renderer is ready.
        """
        with self._channels.control:
            return self._page_binding.bind(require=require)

    def _request(
        self,
        route: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode("utf-8")
        trace = diagnostics.start_request(
            "local", "http", payload, method="status" if route == "status" else None
        )
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if route == "call":
            headers.update(self._page_binding.request_headers())
        if trace is not None:
            headers.update(diagnostics.request_headers(trace))
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.url}/__local/{route}", data=data, headers=headers)
        http_status = None
        try:
            try:
                with self._http.open(
                    request, timeout=self.timeout_s if timeout_s is None else timeout_s
                ) as response:
                    if trace is not None:
                        http_status = response.status
                    response_headers = response.headers
                    body = response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as error:
                if trace is not None:
                    http_status = error.code
                with error:
                    detail = error.read(8192)
                try:
                    failure = json.loads(detail)
                except ValueError:
                    failure = None
                rejected = self._rejection(failure, f"HTTP {error.code}", http_status=error.code)
                if route == "call" and not (
                    error.code == 409
                    and isinstance(rejected, (LocalBusyError, LocalPageMismatchError))
                ):
                    self._page_binding.observe(error.headers)
                raise rejected from error
            except (OSError, URLError) as error:
                unavailable = LocalApiError(
                    "Local simulator transport unavailable. Command outcome may be unknown; "
                    "do not automatically retry thrust. Check the game and reconnect."
                )
                unavailable.code = "transport_unavailable"
                raise unavailable from error
            if len(body) > MAX_RESPONSE_BYTES:
                raise LocalApiError("Local simulator response exceeds the 2 MiB limit")
            try:
                result = json.loads(body)
            except ValueError as error:
                raise LocalApiError("Local simulator returned invalid JSON") from error
            if not isinstance(result, dict):
                raise LocalApiError("Local simulator returned an invalid response")
            if route == "call":
                self._page_binding.observe(
                    response_headers,
                    successful_snapshot=(
                        payload is not None
                        and payload.get("method") == "snapshot"
                        and result.get("ok") is True
                        and isinstance(result.get("value"), dict)
                    ),
                )
        except BaseException as error:
            diagnostics.finish_request(trace, error=error, http_status=http_status)
            raise
        diagnostics.finish_request(trace, response=result, http_status=http_status)
        return result

    def _rejection(
        self, failure: Any, fallback: str, *, http_status: int | None = None
    ) -> LocalApiError:
        if not isinstance(failure, Mapping):
            return LocalApiError(fallback)
        code = failure.get("code")
        if code == "page_mismatch":
            if http_status == 409:
                return LocalPageMismatchError(
                    "The bound local page changed or disappeared. This request was not dispatched."
                )
            return LocalApiError(
                "Local page rejection was not verified before dispatch. Command outcome may be "
                "unknown; do not automatically retry actuation."
            )
        message = failure.get("error")
        if not isinstance(message, str) or not message:
            message = failure.get("message")
        if not isinstance(message, str) or not message:
            message = fallback
        message = message.replace(self._token, "[redacted]")
        if code == "local_busy":
            return LocalBusyError(message)
        if code == "stale_context":
            return StaleContextError(message)
        if code == "command_expired":
            return CommandExpiredError(message)
        if code in ("sensor_unavailable", "sensor_no_sample"):
            return SensorUnavailableError(message, code=code)
        return LocalApiError(message)

    def status(self) -> dict[str, Any]:
        """Read backend lifecycle without issuing a flight command.

        Headless physics_ready follows the actual native scheduler lifecycle.
        A failed driver retains its last report with physics_status_cached=true.
        connected refers only to a browser page and remains false for headless.
        """
        return self._request("status")

    def _call(
        self, payload: Mapping[str, Any], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        # GPU readback has its own channel. It cannot hold flight control or
        # live tuning behind a multi-second image transfer.
        channel = (
            self._channels.capture if payload.get("method") == "capture" else self._channels.control
        )
        with channel:
            result = self._request("call", payload, timeout_s=timeout_s)
        if result.get("ok") is not True:
            raise self._rejection(result, "Local engine request failed")
        value = result.get("value")
        if not isinstance(value, dict):
            raise LocalApiError("Local engine returned an invalid result")
        return value

    def snapshot(self) -> dict[str, Any]:
        """Read public observations, telemetry, navigation, runtime and renderer reports.

        This does not return the debug snapshot's hidden debris truth. Each report
        keeps its own timestamps; sensor/telemetry cadence can differ from physics.
        """
        return self._call({"method": "snapshot"})

    @contextmanager
    def control_transaction(self) -> Iterator[None]:
        """Keep related RPCs and their recorded receipts ordered in this kernel.

        This does not freeze physics or turn a later read into an earlier
        measurement. Snapshot clocks remain those reported by the real engine.
        """
        with self._channels.control:
            yield

    def GetNearestDebrisLocations(self, N: int) -> list[np.ndarray]:
        """Return public 3×3 radar rows using the official helper's name and shape.

        This is the local adapter, not an SDK call. It preserves measurement
        timestamps and row ordering, without adding IDs or debris velocities.
        """
        if type(N) is not int or N < 0:
            raise ValueError("N must be a non-negative integer")
        rows = self.snapshot()["observation"]["radar"]
        return [np.asarray(row, dtype=np.float64) for row in rows[:N]]

    def SetCommand(self, accelCommand: Sequence[float], angAccelCommand: Sequence[float]) -> None:
        """Use the official body-frame vector contract through the local adapter.

        The licensed STPClient implements this same helper with real SetParam
        calls. Neither policy code nor its commands need Marimo or Rust imports.
        This raw command is latched until replaced or explicitly stopped.
        """
        accel = _vector(accelCommand, "accelCommand")
        angular_accel = _vector(angAccelCommand, "angAccelCommand")
        self.command({"op": "set_command", "accel": accel, "angular_accel": angular_accel})

    def set_command_checked(
        self, accel: Sequence[float], angular_accel: Sequence[float], snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Install a local command only if the engine still has this exact context.

        The return value is an ACK, not a physics snapshot or actuator measurement.
        This never pauses, plays or retries. The SDK-shaped SetCommand stays raw.
        A successful checked command is still latched, not time-limited.
        """
        request = {
            "op": "set_command",
            "accel": _vector(accel, "accel"),
            "angular_accel": _vector(angular_accel, "angular_accel"),
            "expected_context": control_context(snapshot),
        }
        return self.command(request)

    def set_leased_command(
        self,
        accel: Sequence[float],
        angular_accel: Sequence[float],
        snapshot: Mapping[str, Any],
        *,
        lease_sim_s: float = 0.75,
        controller_fence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one native, bounded decision lease using only its original snapshot.

        Returns an ACK, not an actuator measurement. Native expiry is the supplied
        observation.time_s plus lease_sim_s, never transport arrival time. The
        original control context and sensor revision must still match. No fresh
        snapshot, pause, playback change or retry is performed here.
        A supplied controller_fence must come from actual active ownership;
        omission is the explicit legacy, unfenced path, not automatic ownership.

        Lease duration is 0.05..1.0 sim seconds. Vector norms are bounded to 0.5
        m/s² and 0.05 rad/s². StaleContextError and CommandExpiredError mean native
        rejection before application; other failures can leave the outcome unknown.
        """
        request = _leased_command_request(
            accel,
            angular_accel,
            snapshot,
            lease_sim_s=lease_sim_s,
            controller_fence=controller_fence,
        )
        return self.command(request)

    def claim_control(
        self,
        run_id: str,
        controller_id: str,
        snapshot: Mapping[str, Any],
        *,
        expected_epoch: int | None = None,
        lease_sim_s: float = 0.75,
    ) -> dict[str, Any]:
        """Claim once, then return actual authority-bearing readback after the ACK.

        The default epoch is for the initial explicit grant only. A standby must
        supply its original Run consent epoch and a fresh snapshot for the term.
        Source time stays that of the supplied snapshot. No mutation is retried.
        """
        authority = _control_authority(snapshot)
        request = _ownership_command(
            {
                "op": "claim_control",
                "run_id": run_id,
                "controller_id": controller_id,
                "expected_epoch": authority["epoch"] if expected_epoch is None else expected_epoch,
                "expected_term": authority["term"],
                "source_time_s": snapshot["observation"].get("time_s"),
                "lease_sim_s": lease_sim_s,
                "expected_context": control_context(snapshot),
            }
        )
        with self.control_transaction():
            self.command(request)
            readback = self.snapshot()
        try:
            fence = controller_fence(readback)
        except ValueError as error:
            raise LocalApiError(
                "Control claim was acknowledged but active ownership was not read back. "
                "Inspect actual state; no claim was retried."
            ) from error
        if any(fence[name] != request[name] for name in ("run_id", "controller_id")):
            raise LocalApiError(
                "Control claim was acknowledged but actual ownership differs. "
                "Inspect actual state; no claim was retried."
            )
        return readback

    def release_control(
        self, run_id: str, expected_epoch: int, snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Release only this native run/epoch. Never fall back to raw Stop or Pause."""
        request = _ownership_command(
            {
                "op": "release_control",
                "run_id": run_id,
                "expected_epoch": expected_epoch,
                "expected_context": control_context(snapshot),
            }
        )
        return self.command(request)

    def renew_control(
        self,
        snapshot: Mapping[str, Any],
        *,
        controller_fence: Mapping[str, Any],
        lease_sim_s: float = 0.75,
    ) -> dict[str, Any]:
        """Renew only the already-active native owner from this genuine source.

        Returns the native ownership receipt without a second snapshot request.
        It installs no command and changes no watchdog or actuator measurement.
        Native checks both contexts, the exact live fence and the source deadline.
        Expired owners cannot reclaim through this operation. Nothing is retried.
        """
        request = _control_renewal_request(
            snapshot, controller_fence=controller_fence, lease_sim_s=lease_sim_s
        )
        result = self.command(request)
        receipt = result.get("ownership_renewal")
        try:
            if not isinstance(receipt, Mapping) or set(receipt) != {
                "source_time_s",
                "lease_sim_s",
                "sensor_revision",
                "control_authority",
            }:
                raise ValueError("Missing native ownership receipt")
            authority = _control_authority({"observation": receipt})
            if (
                _source_time(receipt["source_time_s"]) != request["source_time_s"]
                or _bounded_number(receipt["lease_sim_s"], "lease_sim_s", 0.05, 0.75)
                != request["lease_sim_s"]
                or type(receipt["sensor_revision"]) is not int
                or receipt["sensor_revision"] != request["expected_sensor_revision"]
                or authority["status"] != "active"
                or any(
                    authority[name] != value for name, value in request["controller_fence"].items()
                )
                or authority["expires_at_sim_time_s"]
                != request["source_time_s"] + request["lease_sim_s"]
            ):
                raise ValueError("Native renewal receipt differs from the original request")
        except (TypeError, ValueError, OverflowError) as error:
            raise LocalApiError(
                "Ownership renewal was acknowledged without a matching native receipt. "
                "Inspect actual state; no renewal was retried."
            ) from error
        return {**receipt, "control_authority": authority}

    def set_sensor_fault(
        self, target: str, mode: str, *, duration_sim_s: float = 5.0
    ) -> dict[str, Any]:
        """Change native sensor delivery once, then read the actual public snapshot.

        Faults affect delivery, not physics or playback, and use a separate sensor
        revision. The readback may already show recovery if sim time has elapsed.
        duration_sim_s must be finite and between 0.05 and 300 sim seconds.
        """
        request = _resilience_command(
            {
                "op": "set_sensor_fault",
                "target": target,
                "mode": mode,
                "duration_sim_s": duration_sim_s,
            }
        )
        with self.control_transaction():
            self.command(request)
            return self.snapshot()

    def clear_sensor_faults(self) -> dict[str, Any]:
        """Clear native delivery faults once and read actual state without pausing."""
        with self.control_transaction():
            self.command({"op": "clear_sensor_faults"})
            return self.snapshot()

    def play_checked(self, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Resume an explicitly requested local run only in the same engine context.

        The runtime driver must validate this metadata at execution, just like a
        checked command. There is deliberately no fallback to an unchecked Play.
        """
        return self.command(
            {"op": "play", "rate": 1, "expected_context": control_context(snapshot)}
        )

    def preview(self, report: Mapping[str, Any] | None) -> None:
        """Display a Python-computed advisory, or clear it with None. Never steer."""
        self._call({"method": "preview", "preview": None if report is None else dict(report)})

    def raycast(
        self, target_world_m: Sequence[float], max_distance_m: float = 250.0
    ) -> dict[str, Any]:
        """Query actual Rapier colliders and display the local debug look-ahead ray.

        This is not an STP sensor. Its hit must not be treated as radar data or
        supplied to a competition policy as if the SDK had measured it.
        """
        target = _vector(target_world_m, "target_world_m")
        if any(abs(component) > 10_000_000 for component in target):
            raise ValueError("target_world_m must stay within the local 10000000 m transport bound")
        distance = _bounded_number(max_distance_m, "max_distance_m", 1.0, 100_000.0)
        return self._call(
            {
                "method": "raycast",
                "query": {
                    "target_world_m": target,
                    "max_distance_m": distance,
                },
            }
        )

    def tune_ship(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Change local hardware in place without pausing or replacing the flight.

        Requested SDK-style command limits stay fixed. The local thrust_scale
        calibrates applied force/torque; telemetry reports the actual result.
        """
        if set(settings) != {"mass_kg", "inertia_scale", "thrust_scale"}:
            raise ValueError("Ship tuning requires mass_kg, inertia_scale and thrust_scale")
        bounded = {
            "mass_kg": _bounded_number(settings["mass_kg"], "mass_kg", 100.0, 100_000.0),
            "inertia_scale": _bounded_number(settings["inertia_scale"], "inertia_scale", 0.1, 10.0),
            "thrust_scale": _bounded_number(settings["thrust_scale"], "thrust_scale", 0.1, 10.0),
        }
        self.command({"op": "tune_ship", "settings": bounded})
        snapshot = self.snapshot()
        if snapshot.get("observation", {}).get("ship_tuning") != bounded:
            raise LocalApiError(
                "The ship update was acknowledged but the game readback does not match. "
                "Inspect the game and reconnect."
            )
        return snapshot

    def tune_debris_flow(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        """Adjust the existing stream in place, preserving positions and spin.

        Existing collision/gravity deviations are retained. Spread controls new
        births, not a random replacement of the current field's velocities.
        """
        if set(settings) != {"speed_mps", "speed_spread_mps", "stream_axis", "stream_sign"}:
            raise ValueError(
                "Flow tuning requires speed_mps, speed_spread_mps, stream_axis and stream_sign"
            )
        speed = _bounded_number(settings["speed_mps"], "speed_mps", 0.1, 200.0)
        spread = _bounded_number(settings["speed_spread_mps"], "speed_spread_mps", 0.0, speed / 2.0)
        if (
            settings["stream_axis"] not in ("x", "y", "z")
            or type(settings["stream_sign"]) is not int
            or settings["stream_sign"] not in (-1, 1)
        ):
            raise ValueError("Flow direction needs stream_axis x/y/z and stream_sign -1/1")
        applied = {
            "speed_mps": speed,
            "speed_spread_mps": spread,
            "stream_axis": settings["stream_axis"],
            "stream_sign": settings["stream_sign"],
        }
        self.command(
            {
                "op": "tune_debris_flow",
                "settings": applied,
            }
        )
        snapshot = self.snapshot()
        if snapshot.get("observation", {}).get("debris_flow") != applied:
            raise LocalApiError(
                "The flow update was acknowledged but the game readback does not match. "
                "Inspect the game and reconnect."
            )
        return snapshot

    def camera_status(self) -> dict[str, Any]:
        """Read actual requested aim, optics and sampled ship-camera metadata."""
        status = self.snapshot()["cameras"]
        if not isinstance(status, dict):
            raise LocalApiError("The real ship cameras have not initialized")
        return status

    def PanTiltCamera(self, camera: str, yaw_deg: float, pitch_deg: float) -> None:
        """Apply the official helper's degree convention and asymmetric clipping.

        Local handles are topCamera/bottomCamera strings; STPClient uses real
        SDK entity handles. FOV, resolution and exposure are not changed.
        """
        if camera not in (self.topCamera, self.bottomCamera):
            raise ValueError("camera must be topCamera or bottomCamera")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (yaw_deg, pitch_deg)
        ):
            raise ValueError("camera angles must be finite numbers")
        self._call(
            {
                "method": "pan_tilt",
                "camera": camera,
                "yaw_deg": float(yaw_deg),
                "pitch_deg": float(pitch_deg),
            }
        )

    def capture_image(self, camera: str) -> dict[str, Any]:
        """Read a real GPU frame with no enhancement, overlays or replacement pixels.

        Returns metadata plus immutable uint8 RGB of shape (512,512,3).
        The readback has its own bounded channel, separate from flight writes.
        This local convenience is not the SDK's CaptureImageBlocking function.
        Native delivery failures raise SensorUnavailableError, with code
        sensor_unavailable or sensor_no_sample and the actual rejection message.
        """
        if camera not in (self.topCamera, self.bottomCamera):
            raise ValueError("camera must be topCamera or bottomCamera")
        result = self._call(
            {"method": "capture", "camera": camera}, timeout_s=max(25.0, self.timeout_s)
        )
        metadata, encoded = result.get("metadata"), result.get("rgb_base64")
        if not isinstance(metadata, dict) or not isinstance(encoded, str):
            raise LocalApiError("Camera returned an invalid capture")
        if (
            metadata.get("width"),
            metadata.get("height"),
            metadata.get("channels"),
            metadata.get("bits_per_channel"),
        ) != (512, 512, 3, 8):
            raise LocalApiError("Camera capture does not match 512×512 RGB8")
        try:
            pixels = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise LocalApiError("Camera returned invalid RGB data") from error
        if len(pixels) != 512 * 512 * 3:
            raise LocalApiError("Camera returned an incomplete RGB frame")
        rgb = np.frombuffer(pixels, dtype=np.uint8).reshape(512, 512, 3)
        return {"metadata": metadata, "rgb": rgb}

    def command(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Forward a local API operation. Queued playback ACKs are not applied state.

        These operations are not official STP SDK calls. Prefer ``run_step`` for
        experiments; raw ``set_command`` persists until replaced or stopped.
        """
        op = request.get("op")
        if not isinstance(op, str) or op not in COMMANDS:
            raise ValueError("Unsupported local lab command; read data through snapshot()")
        if op in ("set_sensor_fault", "clear_sensor_faults", "set_leased_command"):
            request = _resilience_command(request)
        elif op in ("claim_control", "renew_control", "release_control"):
            request = _ownership_command(request)
        timeout = max(self.timeout_s, WORLD_CALL_TIMEOUT_S) if op in WORLD_OPERATIONS else None
        return self._call({"method": "command", "command": dict(request)}, timeout_s=timeout)

    def pause(self) -> None:
        """Wait for the actual runtime to pause before allowing a manual step."""
        self.command({"op": "pause"})
        deadline = time.monotonic() + self.timeout_s
        while self.snapshot()["runtime"]["running"]:
            if time.monotonic() >= deadline:
                raise LocalApiError("Pause has not applied. Keep the game visible and try Stop.")
            time.sleep(0.05)

    def stop(self) -> None:
        """Zero requested thrust and torque. This does not brake the lander."""
        self.command({"op": "stop"})

    def reset(self, seed: int | None = None) -> dict[str, Any]:
        if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
            raise ValueError("seed must be an integer between 0 and 4294967295")
        with self.control_transaction():
            before = self.snapshot()
            previous_trial = _trial_revision(before)
            source = before["observation"]
            self.command({"op": "reset", "seed": seed})
            return self._wait_for_receipt(
                lambda snapshot: (
                    snapshot["runtime"]["running"] is False
                    and _trial_revision(snapshot) != previous_trial
                    and snapshot["observation"]["time_s"] == 0
                    and snapshot["observation"]["seed"]
                    == (source["seed"] if seed is None else seed)
                    and snapshot["observation"].get("flight_preset") == source.get("flight_preset")
                    and snapshot["observation"].get("launch_site_direction")
                    == source.get("launch_site_direction")
                    and snapshot["observation"].get("ship_tuning") == source.get("ship_tuning")
                    and snapshot["observation"].get("debris_flow") == source.get("debris_flow")
                    and snapshot["observation"].get("experiment_settings")
                    == source.get("experiment_settings")
                    and snapshot["observation"]["mission"]["status"] == "running"
                    and isinstance(snapshot["observation"].get("applied_physics"), dict)
                ),
                "Reset ACK did not produce the retained setup/calibration and a new paused trial.",
            )

    def upright(self) -> dict[str, Any]:
        """Local test reset: pause, align to the pad, clear spin/thrust, record it.

        This is not an STP command or a physically flown maneuver. Position,
        linear velocity, time, fuel and damage are preserved by the engine.
        """
        self.pause()
        self.command({"op": "debug_upright"})
        return self.snapshot()

    def configure_experiment(
        self, settings: Mapping[str, Any], seed: int | None = None
    ) -> dict[str, Any]:
        """Pause and reset the real local world with explicit bounded what-if settings.

        Ship mass/inertia and asteroid fields are local experiments, not SDK
        hardware setters. Reference acceleration limits remain unchanged.
        """
        if seed is not None and (type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF):
            raise ValueError("seed must be an integer between 0 and 4294967295")
        parsed = validate_experiment_settings(settings)
        payload = {"op": "configure_experiment", "settings": parsed, "seed": seed}
        with self.control_transaction():
            before = self.snapshot()
            previous_trial = _trial_revision(before)
            preset = before["observation"].get("flight_preset")
            self.command(payload)
            return self._wait_for_receipt(
                lambda snapshot: (
                    snapshot["runtime"]["running"] is False
                    and _trial_revision(snapshot) != previous_trial
                    and snapshot["observation"]["time_s"] == 0
                    and snapshot["observation"].get("flight_preset") == preset
                    and snapshot["observation"].get("launch_site_direction") is None
                    and validate_experiment_settings(snapshot["observation"]["experiment_settings"])
                    == parsed
                    and snapshot["observation"].get("ship_tuning")
                    == {**parsed["ship"], "thrust_scale": 1.0}
                    and isinstance(snapshot["observation"].get("applied_physics"), dict)
                ),
                "World configuration ACK did not produce the requested paused world/readback.",
            )

    def _wait_for_receipt(
        self, predicate: Callable[[dict[str, Any]], bool], message: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while True:
            snapshot = self.snapshot()
            try:
                if predicate(snapshot):
                    return snapshot
            except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
                pass
            if time.monotonic() >= deadline:
                raise LocalApiError(message + " No mutation was retried; inspect the actual game.")
            time.sleep(0.05)

    def configure_flight(
        self, settings: Mapping[str, Any], preset: str, seed: int | None = None
    ) -> dict[str, Any]:
        """Explicitly create a paused local trial with a selected launch/target setup.

        All parsing precedes I/O. The command is sent once; only public receipts
        are polled. This is not a live property edit or an SDK flight maneuver.
        """
        parsed = validate_experiment_settings(settings)
        if not isinstance(preset, str) or preset not in FLIGHT_PRESETS:
            raise ValueError("Flight preset must be mars_facing_l1 or far_side_l2")
        if seed is not None:
            seed = _integer(seed, "seed", 0xFFFFFFFF)
        with self.control_transaction():
            before = self.snapshot()
            previous_trial = _trial_revision(before)
            expected_seed = before["observation"]["seed"] if seed is None else seed
            self.command(
                {"op": "configure_flight", "settings": parsed, "seed": seed, "preset": preset}
            )

            def confirmed(snapshot: dict[str, Any]) -> bool:
                observation = snapshot["observation"]
                if (
                    snapshot["runtime"]["running"] is not False
                    or _trial_revision(snapshot) == previous_trial
                    or observation.get("flight_preset") != preset
                    or observation.get("launch_site_direction") is not None
                    or observation.get("seed") != expected_seed
                    or observation.get("time_s") != 0
                    or validate_experiment_settings(observation["experiment_settings"]) != parsed
                ):
                    return False
                tuning = observation["ship_tuning"]
                body = observation.get("applied_physics")
                if not isinstance(body, Mapping):
                    return False
                mass = body.get("mass_kg")
                return (
                    tuning == {**parsed["ship"], "thrust_scale": 1.0}
                    and type(mass) in (int, float)
                    and math.isfinite(mass)
                    and math.isclose(mass, parsed["ship"]["mass_kg"], rel_tol=1e-6, abs_tol=1e-5)
                    and observation["mission"]["target_region"]
                    == ("L1" if preset == "mars_facing_l1" else "L2")
                )

            return self._wait_for_receipt(
                confirmed, "Flight setup ACK did not produce the requested paused trial/readback."
            )

    def place_lander(self, position_m: Sequence[float]) -> dict[str, Any]:
        """Record an explicit at-rest/upright local placement on the same body.

        No Pause is sent before validation or the command. Rejected placement must
        leave playback untouched. Actual Rapier pose/motion, not an ACK echo, must
        confirm success. Backend collision checks remain authoritative.
        """
        position = validate_position(position_m)
        with self.control_transaction():
            before = self.snapshot()
            previous_trial = _trial_revision(before)
            source = before["observation"]
            self.command({"op": "place_lander", "position_m": position})

            def confirmed(snapshot: dict[str, Any]) -> bool:
                observation = snapshot["observation"]
                if (
                    _trial_revision(snapshot) == previous_trial
                    or observation.get("seed") != source["seed"]
                    or observation.get("ship_tuning") != source.get("ship_tuning")
                    or observation.get("flight_preset") != source.get("flight_preset")
                    or observation.get("launch_site_direction")
                    != source.get("launch_site_direction")
                    or not placement_readback_matches(snapshot, position)
                ):
                    return False
                mission = source.get("mission", {})
                if mission.get("status") in ("success", "failed_damage"):
                    return all(
                        observation["mission"].get(field) == mission.get(field)
                        for field in ("status", "phase", "score", "fuel_used_kg", "damage")
                    )
                return True

            return self._wait_for_receipt(
                confirmed,
                "Placement ACK did not produce a confirmed paused, upright, at-rest body.",
            )

    def move_launchpad(self, surface_direction: Sequence[float]) -> dict[str, Any]:
        """Move a paused local launch site, seating the lander on its real pad.

        The game owns terrain projection and collision checks. This sends one
        checked command, never Pause or a new-world action. Dragging in the game
        uses the same physical operation without requiring numeric entry.
        """
        direction = validate_surface_direction(surface_direction)
        with self.control_transaction():
            before = self.snapshot()
            if before["runtime"]["running"]:
                raise LocalApiError("Pause the game before moving the launchpad.")
            expected = control_context(before)
            self.command(
                {
                    "op": "move_launchpad",
                    "surface_direction": direction,
                    "expected_context": expected,
                }
            )

            def confirmed(snapshot: dict[str, Any]) -> bool:
                observation = snapshot["observation"]
                source = before["observation"]
                anchor = _vector(observation["launch_site_direction"], "launch site receipt")
                physics = observation["applied_physics"]
                pad = observation["launchpad"]
                return (
                    snapshot["runtime"]["running"] is False
                    and _trial_revision(snapshot) == expected["trial_revision"] + 1
                    and observation["manual_interventions"] == expected["manual_interventions"] + 1
                    and np.allclose(anchor, direction, rtol=0, atol=1e-12)
                    and observation["time_s"] == source["time_s"]
                    and observation["seed"] == source["seed"]
                    and observation["ship_tuning"] == source["ship_tuning"]
                    and observation["flight_preset"] == source["flight_preset"]
                    and observation["mission"]["status"] == source["mission"]["status"]
                    and all(
                        observation["mission"].get(key) == source["mission"].get(key)
                        for key in ("fuel_used_kg", "damage", "score")
                    )
                    and np.allclose(
                        physics["rotation_body_to_world_xyzw"], pad["rotation"], rtol=0, atol=1e-10
                    )
                    and all(
                        _vector(physics[key], key) == [0.0, 0.0, 0.0]
                        for key in (
                            "velocity_world_mps",
                            "angular_velocity_world_rad_s",
                            "total_external_force_world_n",
                            "total_external_torque_world_nm",
                        )
                    )
                )

            return self._wait_for_receipt(
                confirmed, "Launchpad movement ACK did not produce the requested paused site."
            )

    def step(self, seconds: float) -> dict[str, Any]:
        seconds = _seconds(seconds)
        self.pause()
        self.command({"op": "step", "seconds": seconds})
        return self.snapshot()

    def run_step(
        self,
        accel: Sequence[float],
        angular_accel: Sequence[float] = (0.0, 0.0, 0.0),
        *,
        seconds: float = 0.05,
    ) -> dict[str, Any]:
        """Apply body-frame m/s² and rad/s² for one paused, bounded physics step.

        The actual engine clamps reference vector norms to 0.5 and 0.05. This
        uses the local command actuator, not keyboard nozzles or Space boost.
        Always attempts Stop after taking command ownership. Delivery cannot be
        guaranteed if the browser/connection fails. Returned data is sampled
        before that cleanup, so it can describe the force used during the step.
        """
        accel = _vector(accel, "accel")
        angular_accel = _vector(angular_accel, "angular_accel")
        seconds = _seconds(seconds)
        self.pause()
        try:
            self.SetCommand(accel, angular_accel)
            self.command({"op": "step", "seconds": seconds})
            return self.snapshot()
        finally:
            self.stop()
