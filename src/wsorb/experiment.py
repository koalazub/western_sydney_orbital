"""Read-only Python guidance and explicit local controller runs.

Policies receive public observations and return body-frame SI commands. This
module supplies no controller, planner, physics or STP SDK implementation.
Only the worker writes flight commands. Connecting a watch never takes flight
ownership. Stop signals the worker rather than starting a second writer, and a
slow Python callback cannot be interrupted. Keep callbacks bounded.

Controller cadence is a wall-clock upper target, not an HTTP or physics frequency.
Guidance replans use simulation time, without repeated plans on a held clock.
``sample_count`` counts recorded public snapshots, including safety and cleanup reads,
not simulation steps. ``elapsed_sim_s`` comes only from a continuous observed
simulation clock. A browser pause, reset or manual intervention ends ownership.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from numbers import Real
from typing import Any

import numpy as np
import numpy.typing as npt

from .flight.control import reference_to_body
from .local_api import LocalSimulation, StaleContextError, control_context, validate_position

HISTORY_LIMIT = 2000
TELEMETRY_FIELDS = (
    "speed_mps",
    "radial_climb_mps",
    "terrain_center_clearance_m",
    "target_center_range_m",
    "target_closing_mps",
    "applied_thrust_accel_mps2",
    "thrust_force_n",
    "torque_nm",
    "angular_speed_rad_s",
    "fuel_used_kg_estimate",
    "damage_j_estimate",
)


def _finite(value: Any) -> float | None:
    if type(value) in (int, float):
        try:
            return float(value) if math.isfinite(value) else None
        except OverflowError:
            pass
    return None


def observed_setup(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Copy reported setup metadata without inventing an epoch or physical defaults."""
    baseline = observation.get("experiment_settings")
    debris = baseline.get("debris") if isinstance(baseline, Mapping) else None
    distribution = observation.get("debris_distribution")
    if distribution is None and isinstance(debris, Mapping):
        distribution = debris.get("distribution", {"kind": "directed_stream"})
    trial = observation.get("trial_revision")
    pad = observation.get("launchpad")
    pad_pose = (
        {key: deepcopy(pad.get(key)) for key in ("position", "rotation")}
        if isinstance(pad, Mapping)
        else None
    )
    return {
        "trial_revision": trial if type(trial) is int and 0 <= trial <= 0xFFFFFFFF else None,
        "flight_preset": observation.get("flight_preset"),
        "launch_site_direction": deepcopy(observation.get("launch_site_direction")),
        "launchpad": pad_pose,
        "debris_distribution": deepcopy(distribution),
        "experiment_settings": deepcopy(baseline),
    }


def measured_sample(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a real public snapshot, retaining both independent source times.

    This is serialization, not a physics calculation. Missing/invalid readings
    stay null. A report without its actual telemetry clock and epoch is not a
    measurement and contributes no row. Observation fields are not backdated to
    the telemetry time; chart them against ``observation_time_s`` instead.
    """
    observation = snapshot.get("observation")
    telemetry = snapshot.get("telemetry")
    if not isinstance(observation, Mapping) or not isinstance(telemetry, Mapping):
        return None
    epoch = telemetry.get("epoch")
    now = _finite(telemetry.get("source_time_s"))
    if (
        now is None
        or now < 0
        or not isinstance(epoch, Mapping)
        or type(epoch.get("index")) is not int
        or epoch["index"] < 0
        or type(epoch.get("seed")) is not int
        or epoch["seed"] < 0
    ):
        return None
    metrics = telemetry.get("metrics")
    ship = observation.get("shuttle")
    runtime = snapshot.get("runtime")
    metrics = metrics if isinstance(metrics, Mapping) else {}
    ship = ship if isinstance(ship, Mapping) else {}
    runtime = runtime if isinstance(runtime, Mapping) else {}
    counter = observation.get("manual_interventions", runtime.get("manual_interventions"))
    tuning = observation.get("ship_tuning")
    flow = observation.get("debris_flow")
    tuning = tuning if isinstance(tuning, Mapping) else {}
    flow = flow if isinstance(flow, Mapping) else {}
    revision = observation.get("tuning_revision")
    physics = observation.get("applied_physics")
    physics = physics if isinstance(physics, Mapping) else None
    application = physics.get("last_actuator_application") if physics is not None else None
    application = application if isinstance(application, Mapping) else None
    row = {
        "telemetry_time_s": now,
        "telemetry_epoch": epoch["index"],
        "telemetry_seed": epoch["seed"],
        "telemetry_epoch_started_at_s": _finite(epoch.get("started_at_time_s")),
        "telemetry_status": telemetry.get("status"),
        "telemetry_clock": "local_simulation_elapsed_seconds",
        "observation_time_s": _finite(observation.get("time_s")),
        "observation_seed": observation.get("seed"),
        "observation_clock": "local_simulation_elapsed_seconds",
        "frame": observation.get("frame"),
        "radar_clock": observation.get("radar_clock"),
        "rotation_convention": observation.get("rotation_convention"),
        "manual_interventions": counter if type(counter) is int else None,
        **observed_setup(observation),
        "tuning_revision": revision if type(revision) is int and revision >= 0 else None,
        "ship_tuning": {
            key: _finite(tuning.get(key)) for key in ("mass_kg", "inertia_scale", "thrust_scale")
        },
        "debris_flow": {
            "speed_mps": _finite(flow.get("speed_mps")),
            "speed_spread_mps": _finite(flow.get("speed_spread_mps")),
            "stream_axis": flow.get("stream_axis")
            if flow.get("stream_axis") in ("x", "y", "z")
            else None,
            "stream_sign": flow.get("stream_sign")
            if type(flow.get("stream_sign")) is int and flow["stream_sign"] in (-1, 1)
            else None,
        },
        "ship_mass_kg": _finite(ship.get("mass_kg")),
        "ship_mass_report_kind": "public_shuttle_report",
        "applied_physics": deepcopy(dict(physics)) if physics is not None else None,
        "applied_physics_error": observation.get("applied_physics_error"),
        "applied_physics_time_s": (
            _finite(physics.get("source_time_s")) if physics is not None else None
        ),
        "applied_physics_frame": physics.get("frame") if physics is not None else None,
        "applied_physics_tuning_revision": (
            physics.get("tuning_revision") if physics is not None else None
        ),
        "rapier_mass_kg": _finite(physics.get("mass_kg")) if physics is not None else None,
        "actuation_state": physics.get("actuation_state") if physics is not None else None,
        "external_force_provenance": physics.get("external_force_provenance")
        if physics is not None
        else None,
        "actuator_application_time_s": _finite(application.get("application_time_s"))
        if application is not None
        else None,
        "actuator_application_tuning_revision": application.get("tuning_revision")
        if application is not None
        else None,
        "actuator_application_gain": _finite(application.get("actuator_gain"))
        if application is not None
        else None,
        "actuator_propellant_force_n": _finite(application.get("propellant_force_n"))
        if application is not None
        else None,
        "reference_accel_limit_mps2": _finite(observation.get("max_accel_mps2")),
        "reference_angular_accel_limit_rad_s2": _finite(
            observation.get("max_angular_accel_rad_s2")
        ),
        **{name: _finite(metrics.get(name)) for name in TELEMETRY_FIELDS},
    }
    for field_name, prefix, unit in (
        ("commanded_accel_mps2", "command_accel_body", "mps2"),
        ("commanded_angular_accel_rad_s2", "command_angular_accel_body", "rad_s2"),
    ):
        vector = ship.get(field_name)
        for index, axis in enumerate("xyz"):
            row[f"{prefix}_{axis}_{unit}"] = (
                _finite(vector[index])
                if isinstance(vector, (list, tuple)) and len(vector) == 3
                else None
            )
    inertia = ship.get("inertia_kg_m2")
    for index, axis in enumerate("xyz"):
        diagonal = None
        if isinstance(inertia, (list, tuple)) and len(inertia) == 3:
            values = inertia[index]
            if isinstance(values, (list, tuple)) and len(values) == 3:
                diagonal = _finite(values[index])
        row[f"ship_inertia_{axis}{axis}_kg_m2"] = diagonal
    for source, field_name, prefix, unit in (
        (physics, "position_world_m", "rapier_position_world", "m"),
        (physics, "velocity_world_mps", "rapier_velocity_world", "mps"),
        (physics, "total_external_force_world_n", "external_force_world", "n"),
        (physics, "total_external_torque_world_nm", "external_torque_world", "nm"),
        (application, "force_body_n", "actuator_force_body", "n"),
        (application, "torque_body_nm", "actuator_torque_body", "nm"),
        (application, "force_world_n", "actuator_force_world", "n"),
        (application, "torque_world_nm", "actuator_torque_world", "nm"),
    ):
        vector = source.get(field_name) if source is not None else None
        for index, axis in enumerate("xyz"):
            row[f"{prefix}_{axis}_{unit}"] = (
                _finite(vector[index])
                if isinstance(vector, (list, tuple)) and len(vector) == 3
                else None
            )
    actual_inertia = physics.get("inertia_body_kg_m2") if physics is not None else None
    for index, axis in enumerate("xyz"):
        diagonal = None
        if isinstance(actual_inertia, (list, tuple)) and len(actual_inertia) == 3:
            values = actual_inertia[index]
            if isinstance(values, (list, tuple)) and len(values) == 3:
                diagonal = _finite(values[index])
        row[f"rapier_inertia_{axis}{axis}_kg_m2"] = diagonal
    return row


@dataclass(frozen=True, slots=True)
class Decision:
    """Requested body-frame m/s², rad/s² and an optional browser render packet.

    Vectors are copied, finite float64 arrays. The actual engine applies its own
    actuator limits; constructing a decision neither clamps nor sends anything.
    Preview schema validation belongs to the real browser API.
    """

    accel: npt.ArrayLike
    angular_accel: npt.ArrayLike
    preview: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("accel", "angular_accel"):
            message = f"{name} must be a finite real vector of shape (3,)"
            try:
                array = np.asarray(getattr(self, name))
                if array.shape != (3,) or array.dtype.kind not in "iuf":
                    raise ValueError(message)
                with np.errstate(over="ignore", invalid="ignore"):
                    array = np.array(array, dtype=np.float64, copy=True)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(message) from error
            if not np.isfinite(array).all():
                raise ValueError(message)
            array.setflags(write=False)
            object.__setattr__(self, name, array)
        if self.preview is not None:
            if not isinstance(self.preview, Mapping):
                raise ValueError("preview must be a mapping or None")
            object.__setattr__(self, "preview", deepcopy(dict(self.preview)))


class FlightStateError(RuntimeError):
    """Fixed lifecycle diagnostics that can be shown without transport secrets."""


class _RunError(FlightStateError):
    """Fixed local diagnostics, never transport or callback exception text."""


def _number(value: Any, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite real number")
    try:
        result = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be a finite real number") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite real number")
    return result


def _failure(phase: str, error: BaseException) -> str:
    if isinstance(error, _RunError):
        return str(error)
    # Exception messages can contain connection credentials or arbitrary policy
    # input. Keep the phase and exception class, never their raw text or repr.
    return f"{phase} failed: {type(error).__name__}. Exception text omitted for privacy."


def _world_vector(value: Any, name: str) -> npt.NDArray[np.float64]:
    array = np.asarray(value)
    if array.shape != (3,) or array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a finite real world-frame 3-vector")
    return np.array(array, dtype=np.float64, copy=True)


def next_waypoint(
    observation: Mapping[str, Any], route: Any, arrival_m: float = 20.0
) -> npt.NDArray[np.float64] | None:
    """Select a genuine route point, using the notebook's editable arrival rule."""
    arrival_m = _number(arrival_m, "arrival_m")
    if arrival_m <= 0:
        raise ValueError("arrival_m must be positive")
    if route is None or route.status != "candidate":
        return None
    position = _world_vector(observation["shuttle"]["position"], "shuttle.position")
    points = np.asarray(route.points_world_m)
    if (
        points.ndim != 2
        or points.shape[1] != 3
        or points.dtype.kind not in "iuf"
        or not np.isfinite(points).all()
    ):
        raise ValueError("Candidate route must contain finite world-frame 3-vectors")
    # Point zero is the observation-time start, not a future waypoint. Flight
    # can advance while Python searches or publishes the route.
    return next(
        (
            np.array(point, dtype=np.float64, copy=True)
            for point in points[1:]
            if math.dist(point, position) > arrival_m
        ),
        None,
    )


def lookahead_target(
    observation: Mapping[str, Any],
    route: Any = None,
    *,
    arrival_m: float = 20.0,
    max_distance_m: float = 250.0,
) -> npt.NDArray[np.float64]:
    """Aim a debug query at the next Python waypoint, otherwise true body +X.

    This selects a target, not a ray hit or a sensor measurement. The real query
    engine chooses its current COM origin and excludes the ship's own hull.
    ``reference_to_body`` defines both supported Hamilton conventions; inverting
    that transform here maps body-forward into the public observation frame.
    """
    maximum = _number(max_distance_m, "max_distance_m")
    if maximum <= 0:
        raise ValueError("max_distance_m must be positive")
    waypoint = next_waypoint(observation, route, arrival_m)
    if waypoint is not None:
        return waypoint
    ship = observation["shuttle"]
    position = _world_vector(ship["position"], "shuttle.position")
    rotation = np.asarray(ship["rotation"])
    if rotation.shape != (4,) or rotation.dtype.kind not in "iuf":
        raise ValueError("rotation must be a finite Hamilton xyzw quaternion")
    inverse = np.array(rotation, dtype=np.float64, copy=True)
    inverse[:3] *= -1
    forward = reference_to_body([1.0, 0.0, 0.0], inverse, observation["rotation_convention"])
    with np.errstate(over="ignore", invalid="ignore"):
        return _world_vector(position + forward * maximum, "lookahead target")


def guidance_due(
    previous: tuple[Any, ...] | None,
    observation: Mapping[str, Any],
    policy_revision: int,
    interval_s: float = 0.5,
) -> bool:
    """Simulation-time scheduling, with one fresh plan after edits or discontinuities.

    A held clock is not a new planning tick. Missed intervals produce one plan,
    never a catch-up burst. Tuning and manual changes also invalidate a route.
    """
    current = _guidance_key(observation, policy_revision)
    return (
        previous is None
        or current[:-1] != previous[:-1]
        or current[-1] < previous[-1]
        or current[-1] - previous[-1] >= interval_s
    )


def _guidance_key(observation: Mapping[str, Any], revision: int) -> tuple[Any, ...]:
    mission = observation.get("mission")
    mission = mission if isinstance(mission, Mapping) else {}
    return (
        observation.get("seed"),
        observation.get("frame"),
        observation.get("rotation_convention"),
        observation.get("radar_clock"),
        observation.get("manual_interventions"),
        observation.get("tuning_revision"),
        observation.get("trial_revision"),
        observation.get("flight_preset"),
        mission.get("status"),
        mission.get("phase"),
        revision,
        observation["time_s"],
    )


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Live controller limits. Updating these never starts or rewinds flight."""

    duration_s: float = 30.0
    cadence_hz: float = 2.0

    def __post_init__(self) -> None:
        duration = _number(self.duration_s, "duration_s")
        cadence = _number(self.cadence_hz, "cadence_hz")
        if not 0 < duration <= 300:
            raise ValueError("duration_s must be > 0 and <= 300")
        if not 0.1 <= cadence <= 10:
            raise ValueError("cadence_hz must be between 0.1 and 10")
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "cadence_hz", cadence)

    def to_dict(self) -> dict[str, float]:
        return {"duration_s": self.duration_s, "requested_cadence_hz": self.cadence_hz}


def retime_run(
    active: Mapping[str, Any], limits: RunLimits, *, now_wall_s: float
) -> dict[str, Any]:
    """Change limits without replacing either run origin or making catch-up ticks."""
    now = _number(now_wall_s, "now_wall_s")
    origin = _number(active["wall_origin"], "wall_origin")
    updated = {
        **active,
        "duration": limits.duration_s,
        "cadence": limits.cadence_hz,
        "deadline": origin + 5 * limits.duration_s + 15,
    }
    if limits.cadence_hz != active["cadence"]:
        last_decision = active.get("last_decision_wall")
        updated["next_decision"] = (
            now
            if last_decision is None
            else max(now, _number(last_decision, "last_decision_wall") + 1 / limits.cadence_hz)
        )
    return updated


def run_limit_notice(active: Mapping[str, Any]) -> str | None:
    """Describe a real elapsed-duration limit, not an invented simulation tick."""
    elapsed = active["last_time"] - active["origin"]
    if elapsed >= active["duration"]:
        return f"Observed run duration {elapsed:.3f} s reached the {active['duration']:g} s limit."
    return None


def terminal_mission_notice(observation: Mapping[str, Any] | None) -> str | None:
    """Use the public mission outcome, never distance, speed or a zero force guess."""
    if not isinstance(observation, Mapping):
        return None
    mission = observation.get("mission")
    if not isinstance(mission, Mapping):
        return None
    if mission.get("status") == "failed_damage" or mission.get("phase") == "failed":
        return "Mission ended after damage. Use Reset flight in Launch to fly again."
    if mission.get("status") == "success" or mission.get("phase") == "arrived":
        return "Mission completed. Use Reset flight in Launch to fly again."
    return None


def controller_start_notice(snapshot: Mapping[str, Any]) -> str | None:
    """Reject a terminal or unreported flight before any command or Play request."""
    observation, runtime = snapshot.get("observation"), snapshot.get("runtime")
    if not isinstance(observation, Mapping) or not isinstance(runtime, Mapping):
        return "No current public flight state. Connect / Refresh before Run controller."
    terminal = terminal_mission_notice(observation)
    if terminal:
        return terminal
    mission = observation.get("mission")
    if not isinstance(mission, Mapping) or mission.get("status") != "running":
        return (
            "The game has not reported a running mission. Connect / Refresh before Run controller."
        )
    if type(runtime.get("running")) is not bool:
        return "Actual playback state is unavailable. Connect / Refresh before Run controller."
    if runtime.get("keyboard_thrust") is True:
        return "Release native manual thrust before Run controller."
    trial = observation.get("trial_revision", runtime.get("trial_revision"))
    if trial is not None and (type(trial) is not int or not 0 <= trial <= 0xFFFFFFFF):
        return "The local trial revision is invalid. Connect / Refresh before Run controller."
    now, seed = _finite(observation.get("time_s")), observation.get("seed")
    counter = observation.get("manual_interventions", runtime.get("manual_interventions"))
    if (
        now is None
        or now < 0
        or type(seed) is not int
        or not 0 <= seed <= 0xFFFFFFFF
        or type(counter) is not int
        or not 0 <= counter <= 0xFFFFFFFF
    ):
        return "The real flight clock or intervention counter is unavailable. Connect / Refresh."
    return None


def controller_interruption(
    active: Mapping[str, Any], observation: Mapping[str, Any], runtime: Mapping[str, Any]
) -> str | None:
    """Yield to actual native input, pause or reset without erasing a manual command."""
    trial = observation.get("trial_revision", runtime.get("trial_revision"))
    previous_trial = active.get("trial_revision")
    if trial != previous_trial and (trial is not None or previous_trial is not None):
        return "The local trial/setup changed. The old controller ended; Python is watching only."
    terminal = terminal_mission_notice(observation)
    if terminal:
        return terminal
    if runtime.get("keyboard_thrust") is True:
        return "Native manual thrust took over. Python guidance is watching only."
    counter = observation.get("manual_interventions", runtime.get("manual_interventions"))
    if counter != active["counter"]:
        return "Native intervention ended controller ownership. Python guidance is watching only."
    if observation["seed"] != active["seed"] or observation["time_s"] < active["last_time"]:
        return "The flight was reset or its clock rewound. Python guidance is watching only."
    if active["expect_running"] and not runtime["running"]:
        return (
            "The game was paused. Python guidance is watching only; Run controller resumes control."
        )
    return None


def flight_context(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """Separate confirmed calibration, requested commands and reported physics.

    No force, mass, inertia or acceleration is calculated here. The optional
    Rapier getter report retains its own fields and source clock unchanged.
    """
    source = snapshot or {}
    observation = source.get("observation")
    observation = observation if isinstance(observation, Mapping) else {}
    runtime = source.get("runtime")
    mission, ship = observation.get("mission"), observation.get("shuttle")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    mission = mission if isinstance(mission, Mapping) else {}
    ship = ship if isinstance(ship, Mapping) else {}
    physics = observation.get("applied_physics")
    physics = deepcopy(dict(physics)) if isinstance(physics, Mapping) else None
    requested = physics.get("requested") if physics is not None else None
    requested = requested if isinstance(requested, Mapping) else None
    vectors = []
    for field_name, requested_name in (
        ("commanded_accel_mps2", "accel_body_mps2"),
        ("commanded_angular_accel_rad_s2", "angular_accel_body_rad_s2"),
    ):
        value = (
            requested.get(requested_name)
            if requested is not None
            else ship.get(field_name)
            if physics is None
            else None
        )
        if (
            isinstance(value, (list, tuple))
            and len(value) == 3
            and all(_finite(component) is not None for component in value)
        ):
            vectors.append(list(value))
        else:
            vectors.append(None)
    known = all(vector is not None for vector in vectors)
    zero = known and all(component == 0 for vector in vectors for component in vector)
    playback = "unavailable"
    if runtime.get("running") is True:
        playback = "running"
    elif runtime.get("running") is False:
        playback = "paused"
    return {
        "source_time_s": _finite(observation.get("time_s")),
        "source_seed": observation.get("seed"),
        **observed_setup(observation),
        "mission_status": mission.get("status"),
        "mission_phase": mission.get("phase"),
        "terminal_notice": terminal_mission_notice(observation),
        "playback": playback,
        "command_state": "zero" if zero else "nonzero" if known else "unavailable",
        "requested_accel_body_mps2": vectors[0],
        "requested_angular_accel_body_rad_s2": vectors[1],
        "requested_report_source": (
            "applied_physics.requested"
            if requested is not None
            else "public_shuttle_report"
            if physics is None
            else "unavailable"
        ),
        "confirmed_ship_tuning": deepcopy(observation.get("ship_tuning")),
        "confirmed_debris_flow": deepcopy(observation.get("debris_flow")),
        "tuning_revision": observation.get("tuning_revision"),
        "stored_actuator_report": {
            field_name: deepcopy(ship.get(field_name))
            for field_name in (
                "thrust_n",
                "torque_nm",
                "actuator_gain",
                "thrust_application",
                "wrench_provenance",
            )
        },
        "applied_physics": physics,
        "applied_physics_error": observation.get("applied_physics_error"),
    }


@dataclass(frozen=True, slots=True)
class LivePolicy:
    """The current editable Python functions and settings, accepted by one worker.

    The notebook replaces this bundle after an edit. A decision never silently
    keeps an earlier closure. No callback receives a client or debug ray report.
    """

    predict_scene: Callable
    find_path: Callable
    decide_command: Callable
    settings: Any
    controller: str = "planned_waypoint_pd"
    gains: Mapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("predict_scene", "find_path", "decide_command"):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.controller not in ("coast", "basic_pd_radar", "planned_waypoint_pd"):
            raise ValueError("Unknown live controller")
        if not isinstance(self.gains, Mapping) or not isinstance(self.config, Mapping):
            raise TypeError("gains and config must be mappings")
        arrival = _number(self.gains.get("waypoint_arrival_m", 20.0), "waypoint_arrival_m")
        if arrival <= 0:
            raise ValueError("waypoint_arrival_m must be positive")
        object.__setattr__(self, "settings", deepcopy(self.settings))
        object.__setattr__(self, "gains", deepcopy(dict(self.gains)))
        object.__setattr__(self, "config", deepcopy(dict(self.config)))

    def plan(self, observation: dict) -> Any:
        return self.find_path(self.predict_scene(observation, self.settings), self.settings)

    def decision(self, observation: dict, route: Any) -> Decision:
        accel, angular = self.decide_command(observation, route, self.controller, self.gains)
        return Decision(accel, angular)

    @property
    def arrival_m(self) -> float:
        return float(self.gains.get("waypoint_arrival_m", 20.0))


class ExperimentRunner:
    """One worker, hot-replaceable Python policy and a read-only status report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self._configuration_changes: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
        self._live = False
        self._policy: LivePolicy | None = None
        self._policy_revision = 0
        self._run_limits = RunLimits()
        self._run_limits_revision = 0
        self._run_request: dict[str, Any] | None = None
        self._segment = 0
        self._run_index = 0
        self._new_run = False
        self._report: dict[str, Any] = {
            "state": "idle",
            "sample_count": 0,
            "elapsed_sim_s": 0.0,
            "latest": None,
            "error": None,
        }

    def start(
        self,
        sim: LocalSimulation,
        policy: Callable[[dict], Decision],
        *,
        duration_s: float = 30.0,
        cadence_hz: float = 1.0,
    ) -> None:
        """Pause, clear old forces, then run a captured policy at real-time rate.

        Requires the real observation/runtime manual-intervention counter. An
        active or still-cleaning-up worker must finish before another can start.
        No mutating request is retried after an uncertain transport response.
        """
        duration_s = _number(duration_s, "duration_s")
        cadence_hz = _number(cadence_hz, "cadence_hz")
        if not 0 < duration_s <= 300:
            raise ValueError("duration_s must be > 0 and <= 300")
        if not 0.1 <= cadence_hz <= 10:
            raise ValueError("cadence_hz must be between 0.1 and 10")
        if not isinstance(sim, LocalSimulation):
            raise TypeError("sim must be a LocalSimulation")
        if not callable(policy):
            raise TypeError("policy must be callable")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("An experiment worker is already active; stop and wait first")
            self._stop.clear()
            self._live = False
            self._report.update(
                state="starting", sample_count=0, elapsed_sim_s=0.0, latest=None, error=None
            )
            self._run_index += 1
            self._new_run = True
            self._worker = threading.Thread(
                target=self._run,
                args=(sim, policy, duration_s, cadence_hz),
                name="wsorb-experiment",
                daemon=True,
            )
            try:
                self._worker.start()
            except BaseException as error:
                self._report.update(state="error", error=_failure("Starting worker", error))
                raise

    def watch(
        self,
        sim: LocalSimulation,
        policy: LivePolicy,
        *,
        poll_hz: float = 4.0,
        replan_s: float = 0.5,
    ) -> None:
        """Observe the existing flight and publish real Python plans and debug rays.

        This never pauses, stops, plays or commands flight unless a later explicit
        ``run_controller`` request takes ownership. Even watch cleanup is read-only
        with respect to flight. The one worker also owns preview/query I/O. A
        one-second publication heartbeat holds the original plan and source time;
        it is not another A* invocation or a new measurement.
        """
        if not isinstance(sim, LocalSimulation):
            raise TypeError("sim must be a LocalSimulation")
        if not isinstance(policy, LivePolicy):
            raise TypeError("policy must be a LivePolicy")
        poll_hz = _number(poll_hz, "poll_hz")
        replan_s = _number(replan_s, "replan_s")
        if not 0.5 <= poll_hz <= 10:
            raise ValueError("poll_hz must be between 0.5 and 10")
        if not 0.1 <= replan_s <= 30:
            raise ValueError("replan_s must be between 0.1 and 30")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise RuntimeError("An experiment worker is already active; stop and wait first")
            self._stop.clear()
            self._wake.clear()
            self._live = True
            self._policy = policy
            self._policy_revision += 1
            self._run_request = None
            self._report.update(
                state="connecting",
                worker_active=True,
                controller_running=False,
                flight_owned=False,
                requested_policy_revision=self._policy_revision,
                accepted_policy_revision=None,
                accepted_config=None,
                controller=None,
                plan_count=0,
                last_plan_time_s=None,
                last_command_time_s=None,
                last_command_policy_revision=None,
                last_command_context=None,
                stale_context_count=0,
                requested_run_limits_revision=self._run_limits_revision,
                accepted_run_limits_revision=None,
                requested_run_limits=self._run_limits.to_dict(),
                run_limits=None,
                run_request_state="idle",
                run_origin_time_s=None,
                notice=None,
                notice_source=None,
                raycast=None,
                raycast_error=None,
                error=None,
            )
            self._worker = threading.Thread(
                target=self._watch,
                args=(sim, poll_hz, replan_s),
                name="wsorb-guidance",
                daemon=True,
            )
            try:
                self._worker.start()
            except BaseException as error:
                self._live = False
                self._report.update(
                    state="error", worker_active=False, error=_failure("Starting watch", error)
                )
                raise

    def update_policy(self, policy: LivePolicy) -> int:
        """Queue current functions/settings for the next plan or decision, not a restart.

        The returned revision is requested, not accepted. ``status`` reports the
        revision/config only after the worker accepts it against a real snapshot.
        This method cannot start actuation or contact the simulation.
        """
        if not isinstance(policy, LivePolicy):
            raise TypeError("policy must be a LivePolicy")
        with self._lock:
            if not self._live or self._worker is None or not self._worker.is_alive():
                raise FlightStateError("Connect the read-only guidance watch first")
            if self._stop.is_set():
                raise FlightStateError(
                    "The guidance worker is stopping; reconnect after it finishes"
                )
            if policy is self._policy:
                return self._policy_revision
            self._policy = policy
            self._policy_revision += 1
            self._report["requested_policy_revision"] = self._policy_revision
            self._wake.set()
            return self._policy_revision

    def _queue_run_limits(self, limits: RunLimits) -> int:
        """Caller holds the runner lock. This queues data only, never flight I/O."""
        if limits != self._run_limits:
            self._run_limits = limits
            self._run_limits_revision += 1
        self._report.update(
            requested_run_limits_revision=self._run_limits_revision,
            requested_run_limits=limits.to_dict(),
        )
        self._wake.set()
        return self._run_limits_revision

    def update_run_limits(self, *, duration_s: float, cadence_hz: float) -> int:
        """Queue hot limits for the watch worker, preserving an active run's origins.

        The worker accepts the revision against a real public snapshot. Shortening
        duration below observed elapsed time ends that controller with Stop/Pause.
        An idle watch only updates its next-run settings; this never issues Play.
        """
        limits = RunLimits(duration_s, cadence_hz)
        with self._lock:
            if not self._live or self._worker is None or not self._worker.is_alive():
                raise FlightStateError("Connect the read-only guidance watch first")
            if self._stop.is_set():
                raise FlightStateError(
                    "The guidance worker is stopping; reconnect after it finishes"
                )
            return self._queue_run_limits(limits)

    def run_controller(
        self, *, duration_s: float | None = None, cadence_hz: float | None = None
    ) -> None:
        """Explicit consent to drive the current flight with the current Python policy.

        Reuses the watch worker and preserves pose, motion and simulation time.
        A paused game resumes only after the worker sends its first valid command.
        Limits and manual-intervention checks still apply. There is no auto-retry.
        """
        if duration_s is not None:
            RunLimits(duration_s=duration_s)
        if cadence_hz is not None:
            RunLimits(cadence_hz=cadence_hz)
        with self._lock:
            if not self._live or self._worker is None or not self._worker.is_alive():
                raise FlightStateError("Connect the read-only guidance watch first")
            if self._stop.is_set():
                raise FlightStateError(
                    "The guidance worker is stopping; reconnect after it finishes"
                )
            if self._run_request is not None or self._report.get("controller_running"):
                raise FlightStateError("The controller is already starting or running")
            latest = self._report["latest"] or {}
            notice = controller_start_notice(latest)
            if notice:
                self._report.update(
                    run_request_state="rejected",
                    notice=notice,
                    notice_source="mission"
                    if terminal_mission_notice(latest.get("observation", {}))
                    else "request",
                )
                raise FlightStateError(notice)
            limits = RunLimits(
                self._run_limits.duration_s if duration_s is None else duration_s,
                self._run_limits.cadence_hz if cadence_hz is None else cadence_hz,
            )
            self._queue_run_limits(limits)
            observation, runtime = latest["observation"], latest["runtime"]
            self._run_request = {
                "seed": observation["seed"],
                "counter": observation.get(
                    "manual_interventions", runtime.get("manual_interventions")
                ),
                "last_time": observation["time_s"],
                "expect_running": runtime["running"],
                "trial_revision": observation.get("trial_revision", runtime.get("trial_revision")),
            }
            self._report.update(
                state="starting",
                run_request_state="queued",
                notice=None,
                notice_source=None,
                error=None,
            )
            self._wake.set()

    def stop(self, timeout_s: float = 10.0) -> dict:
        """Signal and bounded-join the worker, without writing network commands.

        A timed-out join leaves the state stopping and prevents a second writer.
        In-flight HTTP and policy calls can delay cleanup beyond this timeout.
        """
        timeout_s = _number(timeout_s, "timeout_s")
        if not 0 <= timeout_s <= threading.TIMEOUT_MAX:
            raise ValueError("timeout_s is outside the supported non-negative join range")
        with self._lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._stop.set()
                self._wake.set()
                if self._report["state"] not in ("finished", "error"):
                    self._report["state"] = "stopping"
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout_s)
        return self.status()

    def place_lander(
        self, sim: LocalSimulation, position_m: Sequence[float], timeout_s: float = 10.0
    ) -> dict:
        """Hold the writer lock through native validation, then stop on success.

        Rejection leaves the controller attached to its existing command. A real
        placement clears thrust and changes trial context before the worker can
        write again. Its final ownership read then yields without Stop/Pause.
        """
        if not isinstance(sim, LocalSimulation):
            raise TypeError("sim must be a LocalSimulation")
        position = validate_position(position_m)
        timeout_s = _number(timeout_s, "timeout_s")
        if not 0 <= timeout_s <= threading.TIMEOUT_MAX:
            raise ValueError("timeout_s is outside the supported non-negative join range")
        with sim.control_transaction():
            with self._lock:
                if self._worker is not None and self._worker.is_alive() and not self._live:
                    raise FlightStateError(
                        "Stop a legacy experiment explicitly before local placement."
                    )
            after = sim.place_lander(position)
            self.record_snapshot(after, source="place_lander")
            # Signal while writes are still excluded. Join only after releasing
            # the transaction so final context/ownership reads can complete.
            self.stop(timeout_s=0)
        status = self.stop(timeout_s)
        if status.get("worker_active") is True or status.get("state") == "stopping":
            raise FlightStateError(
                "Placement applied, but the worker is still stopping. Wait before reconnecting."
            )
        return after

    def status(self) -> dict:
        """Copy actual reports without contacting the browser or exposing a client."""
        with self._lock:
            return deepcopy(self._report)

    def history(self) -> list[dict[str, Any]]:
        """Copy at most 2000 received rows, including labelled same-clock tuning reads."""
        with self._lock:
            return deepcopy(list(self._history))

    def configuration_changes(self) -> list[dict[str, Any]]:
        """Accepted Python revisions and actual reported tuning, with original clocks."""
        with self._lock:
            return deepcopy(list(self._configuration_changes))

    def _accept_policy(self, observation: Mapping[str, Any]) -> tuple[LivePolicy, int]:
        """Atomically accept the pending bundle at a real observed decision boundary."""
        with self._lock:
            policy, revision = self._policy, self._policy_revision
            if policy is None:
                raise _RunError("No current Python policy")
            if revision != self._report.get("accepted_policy_revision"):
                config = {**deepcopy(dict(policy.config)), "controller": policy.controller}
                self._report.update(
                    accepted_policy_revision=revision,
                    accepted_config=config,
                    controller=policy.controller,
                )
                self._configuration_changes.append(
                    {
                        "kind": "python_policy_accepted",
                        "policy_revision": revision,
                        "source_time_s": observation.get("time_s"),
                        "source_seed": observation.get("seed"),
                        **observed_setup(observation),
                        "tuning_revision": observation.get("tuning_revision"),
                        "ship_tuning": deepcopy(observation.get("ship_tuning")),
                        "debris_flow": deepcopy(observation.get("debris_flow")),
                        "applied_physics": deepcopy(observation.get("applied_physics")),
                        "applied_physics_error": observation.get("applied_physics_error"),
                        "config": config,
                    }
                )
            return policy, revision

    def _accept_run_limits(self, observation: Mapping[str, Any]) -> tuple[RunLimits, int]:
        """Accept pending timing values once, on the same worker as the policy."""
        with self._lock:
            limits, revision = self._run_limits, self._run_limits_revision
            if revision != self._report.get("accepted_run_limits_revision"):
                self._report.update(
                    accepted_run_limits_revision=revision, run_limits=limits.to_dict()
                )
                self._configuration_changes.append(
                    {
                        "kind": "run_limits_accepted",
                        "run_limits_revision": revision,
                        "run_index": self._run_index,
                        "run_origin_time_s": self._report.get("run_origin_time_s")
                        if self._report.get("controller_running")
                        else None,
                        "active_run": self._report.get("controller_running") is True,
                        "source_time_s": observation.get("time_s"),
                        "source_seed": observation.get("seed"),
                        **observed_setup(observation),
                        "tuning_revision": observation.get("tuning_revision"),
                        "ship_tuning": deepcopy(observation.get("ship_tuning")),
                        "debris_flow": deepcopy(observation.get("debris_flow")),
                        "applied_physics": deepcopy(observation.get("applied_physics")),
                        "applied_physics_error": observation.get("applied_physics_error"),
                        "run_limits": limits.to_dict(),
                    }
                )
            return limits, revision

    def record_snapshot(self, snapshot: dict, *, source: str = "manual_read") -> None:
        """Record an actual public read, including explicit paused-step results.

        This does not advance time or send a command. The notebook calls it only
        with real client results. Repeated held telemetry replaces the last row,
        rather than creating new measurements at observation or wall-clock time.
        ``segment`` also splits actual tuning changes. A same-clock tuning read
        keeps both observed configurations and labels the telemetry as held.
        """
        row = measured_sample(snapshot)
        with self._lock:
            observation = snapshot.get("observation")
            previous_snapshot = self._report["latest"] or {}
            previous_observation = previous_snapshot.get("observation") or {}
            setup = observed_setup(observation) if isinstance(observation, Mapping) else {}
            previous_setup = observed_setup(previous_observation)
            setup_changed = any(
                setup.get(key) != previous_setup.get(key)
                for key in (
                    "trial_revision",
                    "flight_preset",
                    "launch_site_direction",
                    "launchpad",
                    "debris_distribution",
                    "experiment_settings",
                )
            )
            placement = source == "place_lander"
            launch_site_changed = bool(previous_observation) and any(
                setup.get(key) != previous_setup.get(key)
                for key in ("launch_site_direction", "launchpad")
            )
            if isinstance(observation, Mapping) and (setup_changed or placement):
                self._configuration_changes.append(
                    {
                        "kind": "recorded_placement"
                        if placement
                        else "reported_launch_site"
                        if launch_site_changed
                        else "reported_setup",
                        "sample_source": source,
                        "source_time_s": _finite(observation.get("time_s")),
                        "source_seed": observation.get("seed"),
                        "manual_interventions": observation.get("manual_interventions"),
                        **deepcopy(setup),
                        "applied_physics": deepcopy(observation.get("applied_physics")),
                    }
                )
            if (
                isinstance(observation, Mapping)
                and type(observation.get("tuning_revision")) is int
                and observation["tuning_revision"] >= 0
                and any(
                    observation.get(key) != previous_observation.get(key)
                    for key in ("seed", "tuning_revision", "ship_tuning", "debris_flow")
                )
            ):
                self._configuration_changes.append(
                    {
                        "kind": "reported_tuning",
                        "sample_source": source,
                        "source_time_s": _finite(observation.get("time_s")),
                        "source_seed": observation.get("seed"),
                        "tuning_revision": observation["tuning_revision"],
                        "ship_tuning": deepcopy(observation.get("ship_tuning")),
                        "debris_flow": deepcopy(observation.get("debris_flow")),
                        "applied_physics": deepcopy(observation.get("applied_physics")),
                        "applied_physics_error": observation.get("applied_physics_error"),
                    }
                )
            self._report["latest"] = deepcopy(snapshot)
            self._report["sample_count"] += 1
            if row is None:
                return
            row.update(
                accepted_policy_revision=self._report.get("accepted_policy_revision"),
                accepted_run_limits_revision=self._report.get("accepted_run_limits_revision"),
                run_limits=deepcopy(self._report.get("run_limits")),
            )
            previous = self._history[-1] if self._history else None
            intervention = bool(
                previous is not None
                and row["manual_interventions"] is not None
                and previous["manual_interventions"] is not None
                and row["manual_interventions"] != previous["manual_interventions"]
            )
            boundary = self._new_run or intervention or setup_changed or placement
            tuning_changed = bool(
                previous is not None
                and row["tuning_revision"] is not None
                and previous["tuning_revision"] is not None
                and any(
                    row[key] != previous[key]
                    for key in ("tuning_revision", "ship_tuning", "debris_flow")
                )
            )
            boundary |= tuning_changed
            configuration_changed = bool(
                previous is not None
                and any(
                    row[key] != previous[key]
                    for key in ("accepted_policy_revision", "accepted_run_limits_revision")
                )
            )
            boundary |= configuration_changed
            if previous is not None:
                boundary |= any(
                    row[key] != previous[key]
                    for key in (
                        "telemetry_epoch",
                        "telemetry_seed",
                        "observation_seed",
                        "frame",
                        "radar_clock",
                        "rotation_convention",
                    )
                )
                boundary |= row["telemetry_time_s"] < previous["telemetry_time_s"]
                if (
                    row["observation_time_s"] is not None
                    and previous["observation_time_s"] is not None
                ):
                    boundary |= row["observation_time_s"] < previous["observation_time_s"]
            if boundary:
                self._segment += 1
            self._new_run = False
            row.update(
                segment=self._segment,
                run_index=self._run_index,
                sample_source=source,
                manual_intervention=intervention,
                manual_intervention_source=source if intervention else None,
                tuning_changed=tuning_changed,
                configuration_changed=configuration_changed,
                setup_changed=setup_changed,
                launch_site_changed=launch_site_changed,
                recorded_placement=placement,
                telemetry_held=False,
            )
            if previous is not None and all(
                row[key] == previous[key]
                for key in ("telemetry_epoch", "telemetry_seed", "telemetry_time_s")
            ):
                row["telemetry_held"] = True
                if tuning_changed or configuration_changed or setup_changed or placement:
                    self._history.append(row)
                else:
                    row["manual_intervention"] |= previous["manual_intervention"]
                    row["tuning_changed"] |= previous["tuning_changed"]
                    row["configuration_changed"] |= previous["configuration_changed"]
                    row["setup_changed"] |= previous["setup_changed"]
                    row["launch_site_changed"] |= previous["launch_site_changed"]
                    row["recorded_placement"] |= previous["recorded_placement"]
                    if not intervention:
                        row["manual_intervention_source"] = previous["manual_intervention_source"]
                    self._history[-1] = row
            else:
                self._history.append(row)

    def _watch(self, sim: LocalSimulation, poll_hz: float, replan_s: float) -> None:
        phase = "Reading public snapshot"
        owned = False
        active: dict[str, Any] | None = None
        route = None
        preview = None
        previous_plan: tuple[Any, ...] | None = None
        snapshot: dict | None = None
        policy: LivePolicy | None = None
        revision = 0
        fatal: str | None = None
        last_publication_wall = 0.0

        def read(*, check_owner: bool = True) -> dict:
            nonlocal phase, owned, active, route, preview, previous_plan
            phase = "Reading public snapshot"
            with sim.control_transaction():
                result = sim.snapshot()
                self.record_snapshot(result, source="guidance_worker")
            observation, runtime = result.get("observation"), result.get("runtime")
            if not isinstance(observation, dict) or not isinstance(runtime, dict):
                raise _RunError("Snapshot lacks public observation or runtime state")
            now = _finite(observation.get("time_s"))
            seed = observation.get("seed")
            counter = observation.get("manual_interventions", runtime.get("manual_interventions"))
            tuning = observation.get("tuning_revision")
            trial = observation.get("trial_revision", runtime.get("trial_revision"))
            if now is None or now < 0:
                raise _RunError("Snapshot lacks a finite non-negative simulation time")
            if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFF:
                raise _RunError("Snapshot lacks a simulation seed")
            if type(counter) is not int or not 0 <= counter <= 0xFFFFFFFF:
                raise _RunError("Snapshot lacks the real u32 manual-intervention counter")
            if type(tuning) is not int or tuning < 0:
                raise _RunError(
                    "Snapshot lacks the actual tuning revision; reconnect to the current engine"
                )
            if trial is not None and (type(trial) is not int or not 0 <= trial <= 0xFFFFFFFF):
                raise _RunError("Snapshot has an invalid local trial revision")
            if type(runtime.get("running")) is not bool:
                raise _RunError("Snapshot lacks actual runtime playback state")
            if owned and runtime.get("keyboard_thrust") is True:
                # A real manual command has superseded ours. Do not send a
                # cleanup Stop/Pause that would erase the user's new command.
                owned, active = False, None
                route, preview, previous_plan = None, None, None
                with self._lock:
                    self._report.update(
                        state="watching",
                        controller_running=False,
                        flight_owned=False,
                        run_request_state="yielded",
                        notice="Native manual thrust took over. Python guidance is watching only.",
                        notice_source="native",
                        error=None,
                    )
            if active is not None and check_owner:
                interruption = controller_interruption(active, observation, runtime)
                if interruption:
                    completed = terminal_mission_notice(observation) is not None
                    elapsed = now - active["origin"] if seed == active["seed"] else None
                    owned, active = False, None
                    route, preview, previous_plan = None, None, None
                    with self._lock:
                        self._report.update(
                            state="watching",
                            controller_running=False,
                            flight_owned=False,
                            run_request_state="completed" if completed else "yielded",
                            notice=interruption,
                            notice_source="mission" if completed else "native",
                            error=None,
                        )
                        if elapsed is not None and elapsed >= 0:
                            self._report["elapsed_sim_s"] = elapsed
                else:
                    if active["awaiting_play"] and runtime["running"]:
                        active.update(awaiting_play=False, expect_running=True)
                        with self._lock:
                            self._report.update(state="running", run_request_state="running")
                    active["last_time"] = now
                    with self._lock:
                        self._report["elapsed_sim_s"] = now - active["origin"]
            mission = observation.get("mission")
            if (
                isinstance(mission, Mapping)
                and mission.get("status") == "running"
                and terminal_mission_notice(observation) is None
            ):
                with self._lock:
                    if self._report.get("notice_source") == "mission":
                        self._report.update(
                            notice=None, notice_source=None, run_request_state="idle"
                        )
            return result

        def accept_limits(observation: Mapping[str, Any]) -> tuple[RunLimits, int]:
            nonlocal active
            limits, limits_revision = self._accept_run_limits(observation)
            if active is not None and active["limits_revision"] != limits_revision:
                active = retime_run(active, limits, now_wall_s=time.monotonic())
                active["limits_revision"] = limits_revision
            return limits, limits_revision

        def recover_stale(*, path: bool = False) -> None:
            nonlocal owned, active, snapshot, route, preview, previous_plan
            previous_owned = owned
            owned = False
            if path:
                active = None  # A path from another trial cannot retain flight ownership.
            route, preview, previous_plan = None, None, None
            with self._lock:
                self._report.update(
                    flight_owned=False,
                    stale_context_count=self._report["stale_context_count"] + 1,
                    notice="Old local context rejected before mutation; reading fresh public data.",
                    notice_source="stale_context",
                )
            try:
                snapshot = read()
            except BaseException as error:
                active = None
                with self._lock:
                    self._report.update(
                        state="error",
                        controller_running=False,
                        run_request_state="yielded",
                        error=_failure("Read after stale local context", error),
                    )
                return
            if active is not None:
                owned = previous_owned
                # A command/Play rejected in the previous context cannot require
                # the paused simulation clock to advance before a fresh decision.
                active.update(decision_time=None, next_decision=time.monotonic())
            with self._lock:
                self._report.update(
                    flight_owned=owned,
                    controller_running=active is not None,
                    state="running"
                    if active and snapshot["runtime"]["running"]
                    else "starting"
                    if active
                    else "watching",
                )
                if active is None and self._report.get("run_request_state") not in (
                    "completed",
                    "rejected",
                ):
                    self._report["run_request_state"] = "yielded"
                elif active is not None:
                    self._report["run_request_state"] = (
                        "running" if snapshot["runtime"]["running"] else "starting"
                    )

        def finish_run(reason: str | None = None, *, notice: str | None = None) -> str | None:
            nonlocal owned, active, snapshot
            errors = [reason] if reason else []
            cleanup_requested = owned or active is not None
            if cleanup_requested:
                with sim.control_transaction():
                    try:
                        snapshot = read()
                    except BaseException as error:
                        errors.append(_failure("Cleanup ownership read", error))
                    if active is None:
                        return None  # Native takeover/pause owns the flight now.
                    # Distinct safety operations, never a thrust/Play retry.
                    for label, action, recheck_owner in (
                        ("Cleanup Stop", sim.stop, False),
                        ("Cleanup Pause", sim.pause, True),
                    ):
                        if recheck_owner:
                            try:
                                snapshot = read()
                            except BaseException as error:
                                errors.append(_failure("Cleanup ownership read", error))
                            if not owned:
                                return None
                        # A valid explicit Run also authorizes bounded cleanup
                        # when its limit expires before its first thrust command.
                        if not owned:
                            owned = True
                            with self._lock:
                                self._report["flight_owned"] = True
                        try:
                            action()
                        except BaseException as error:
                            errors.append(_failure(label, error))
                    try:
                        snapshot = read(check_owner=False)
                        if snapshot["runtime"]["running"]:
                            raise _RunError(
                                "Cleanup could not confirm that actual runtime is paused"
                            )
                    except BaseException as error:
                        errors.append(_failure("Cleanup snapshot", error))
            owned, active = False, None
            message = "; ".join(errors) or None
            if notice and not message:
                notice += (
                    " Controller stopped and paused; the flight was not reset."
                    if cleanup_requested
                    else " No flight command was sent."
                )
            with self._lock:
                self._report.update(
                    state="error" if message else "watching",
                    controller_running=False,
                    flight_owned=False,
                    run_request_state="failed" if message else "completed",
                    notice=notice,
                    notice_source="run_limit" if notice else None,
                    error=message,
                )
            return message

        def empty_preview(observation: dict, detail: str) -> dict:
            # No search succeeded. Zero used obstacles/expansions are algorithm
            # counters, not invented sensor readings or a candidate path.
            geometry = observation["geometry"]
            radius = _number(geometry["arrival_radius_m"], "arrival_radius_m")
            if radius <= 0 or policy is None:
                raise ValueError("No public goal region")
            return {
                "source_seed": observation["seed"],
                "source_time_s": observation["time_s"],
                "status": "invalid_input",
                "detail": detail[:240],
                "points_world_m": [],
                "goal_center_m": _world_vector(
                    observation["target"]["position"], "target.position"
                ).tolist(),
                "goal_radius_m": radius,
                "measured_obstacles": 0,
                "unmodelled_obstacles_lower_bound": geometry["debris_count"],
                "forecast_horizon_s": policy.settings.forecast_horizon_s,
                "max_measurement_age_s": policy.settings.max_measurement_age_s,
                "oldest_measurement_time_s": None,
                "newest_measurement_time_s": None,
                "assumed_tangential_speed_allowance_mps": (
                    policy.settings.unknown_tangential_speed_mps
                ),
                "expanded_nodes": 0,
            }

        def publish(state: str, *, failure: str | None = None, query: bool = False) -> bool:
            nonlocal phase, last_publication_wall, route, preview, previous_plan
            if snapshot is None or policy is None:
                return False
            observation = snapshot["observation"]
            packet = deepcopy(preview)
            if failure is not None or packet is None:
                try:
                    packet = empty_preview(observation, failure or "Python guidance has no route")
                except (KeyError, TypeError, ValueError, AttributeError):
                    phase = "Clearing Python preview"
                    sim.preview(None)
                    last_publication_wall = time.monotonic()
                    return True
            current_route = route if failure is None and preview is not None else None
            waypoint = next_waypoint(observation, current_route, policy.arrival_m)
            packet["execution"] = {
                "state": state,
                "controller": policy.controller,
                "source_time_s": observation["time_s"],
                "source_seed": observation["seed"],
                "next_waypoint_world_m": waypoint.tolist() if waypoint is not None else None,
                "tuning_revision": observation["tuning_revision"],
            }
            if observation.get("trial_revision") is not None:
                packet["execution"]["trial_revision"] = observation["trial_revision"]
            phase = "Publishing Python guidance"
            try:
                sim.preview(packet)
            except StaleContextError:
                recover_stale(path=True)
                return False
            last_publication_wall = time.monotonic()
            if query and not self._stop.is_set():
                # Debug truth is deliberately kept out of observation, prediction
                # and control. The query itself publishes the real native trace.
                try:
                    target = lookahead_target(
                        observation, current_route, arrival_m=policy.arrival_m
                    )
                    result = sim.raycast(target.tolist(), max_distance_m=250.0)
                    with self._lock:
                        self._report.update(raycast=deepcopy(result), raycast_error=None)
                except BaseException as error:
                    with self._lock:
                        self._report.update(
                            raycast=None, raycast_error=_failure("Debug lookahead query", error)
                        )
            return True

        def policy_changed() -> bool:
            with self._lock:
                return revision != self._policy_revision

        try:
            while not self._stop.is_set():
                try:
                    snapshot = read()
                except _RunError as error:
                    if active is None:
                        raise
                    reason = finish_run(str(error))
                    route, preview, previous_plan = None, None, None
                    publish("error", failure=reason)
                    if self._stop.is_set():
                        break
                    self._wake.wait(1 / poll_hz)
                    self._wake.clear()
                    continue
                if self._stop.is_set():
                    break
                observation = snapshot["observation"]
                policy, revision = self._accept_policy(observation)
                limits, limits_revision = accept_limits(observation)
                with self._lock:
                    request, self._run_request = self._run_request, None
                    if request is not None:
                        blocked = controller_start_notice(snapshot) or controller_interruption(
                            request, observation, snapshot["runtime"]
                        )
                        if blocked:
                            self._report.update(
                                state="watching",
                                controller_running=False,
                                flight_owned=False,
                                run_request_state="rejected",
                                notice=blocked,
                                notice_source="mission"
                                if terminal_mission_notice(observation)
                                else "request",
                                error=None,
                            )
                        else:
                            self._run_index += 1
                            self._new_run = True
                            wall_origin = time.monotonic()
                            active = {
                                "origin": observation["time_s"],
                                "wall_origin": wall_origin,
                                "last_time": observation["time_s"],
                                "seed": observation["seed"],
                                "counter": observation.get(
                                    "manual_interventions",
                                    snapshot["runtime"].get("manual_interventions"),
                                ),
                                "trial_revision": observation.get(
                                    "trial_revision", snapshot["runtime"].get("trial_revision")
                                ),
                                "duration": limits.duration_s,
                                "cadence": limits.cadence_hz,
                                "limits_revision": limits_revision,
                                "deadline": wall_origin + 5 * limits.duration_s + 15,
                                "next_decision": 0.0,
                                "last_decision_wall": None,
                                "decision_time": None,
                                "expect_running": snapshot["runtime"]["running"],
                                "awaiting_play": False,
                                "play_deadline": None,
                            }
                            self._report.update(
                                state="starting",
                                controller_running=True,
                                run_request_state="starting",
                                elapsed_sim_s=0.0,
                                run_origin_time_s=active["origin"],
                                notice=None,
                                notice_source=None,
                                error=None,
                            )
                            self._configuration_changes.append(
                                {
                                    "kind": "controller_started",
                                    "run_index": self._run_index,
                                    "source_time_s": observation["time_s"],
                                    "source_seed": observation["seed"],
                                    **observed_setup(observation),
                                    "run_origin_time_s": active["origin"],
                                    "policy_revision": revision,
                                    "run_limits_revision": limits_revision,
                                    "run_limits": limits.to_dict(),
                                }
                            )

                if active is not None and run_limit_notice(active):
                    reason = finish_run(notice=run_limit_notice(active))
                    publish("error" if reason else "watching", failure=reason)
                    observation = snapshot["observation"]
                if guidance_due(previous_plan, observation, revision, replan_s):
                    if self._stop.is_set():
                        break
                    planning_key = _guidance_key(observation, revision)
                    previous_plan = planning_key
                    phase = "Python prediction and A*"
                    guidance_error = None
                    try:
                        route = policy.plan(deepcopy(observation))
                        preview = route.to_preview()
                        next_waypoint(observation, route, policy.arrival_m)
                    except BaseException as error:
                        route, preview = None, None
                        guidance_error = _failure("Python guidance", error)
                    if self._stop.is_set():
                        break
                    # A long search must not publish across a reset, intervention
                    # or a policy edit, nor write from the old observation.
                    after = read()
                    current = after["observation"]
                    changed = _guidance_key(current, revision)[:-1] != planning_key[:-1]
                    changed |= current["time_s"] < observation["time_s"]
                    snapshot = after
                    accept_limits(current)
                    if changed or policy_changed():
                        route, preview, previous_plan = None, None, None
                        continue
                    with self._lock:
                        state = "watching"
                        if active:
                            state = "running" if owned else "starting"
                        elif guidance_error:
                            state = "error"
                        self._report.update(
                            state=state,
                            last_plan_time_s=observation["time_s"],
                            plan_count=self._report["plan_count"] + 1,
                            error=guidance_error,
                        )
                    if not publish(
                        "running" if owned else "error" if guidance_error else "watching",
                        failure=guidance_error,
                        query=True,
                    ):
                        continue
                elif active is None:
                    with self._lock:
                        if self._report["state"] == "connecting":
                            self._report["state"] = "watching"

                if self._stop.is_set():
                    break
                if active is not None:
                    if time.monotonic() >= active["deadline"]:
                        finish_run(
                            "Wall-time limit reached without completing the observed duration"
                        )
                        publish("error", failure=self.status()["error"])
                    elif run_limit_notice(active):
                        reason = finish_run(notice=run_limit_notice(active))
                        publish("error" if reason else "watching", failure=reason)
                    elif active["awaiting_play"]:
                        if time.monotonic() >= active["play_deadline"]:
                            finish_run(
                                "Play was acknowledged but actual runtime did not start in time"
                            )
                            publish("error", failure=self.status()["error"])
                    elif time.monotonic() >= active["next_decision"] and (
                        active["decision_time"] is None
                        or active["last_time"] > active["decision_time"]
                    ):
                        phase = "Python controller"
                        tick_started = time.monotonic()
                        decision_context = _guidance_key(snapshot["observation"], revision)
                        try:
                            decision = policy.decision(deepcopy(snapshot["observation"]), route)
                        except BaseException as error:
                            reason = finish_run(_failure("Python controller", error))
                            route, preview = None, None
                            publish("error", failure=reason)
                            continue
                        if self._stop.is_set():
                            break
                        with sim.control_transaction():
                            snapshot = read()
                            if self._stop.is_set() or active is None or policy_changed():
                                continue
                            accept_limits(snapshot["observation"])
                            if (
                                _guidance_key(snapshot["observation"], revision)[:-1]
                                != decision_context[:-1]
                            ):
                                route, preview, previous_plan = None, None, None
                                continue
                            if time.monotonic() >= active["deadline"]:
                                reason = finish_run(
                                    "Wall-time limit reached without completing "
                                    "the observed duration"
                                )
                                route, preview = None, None
                                publish("error", failure=reason)
                                continue
                            if run_limit_notice(active):
                                reason = finish_run(notice=run_limit_notice(active))
                                publish("error" if reason else "watching", failure=reason)
                                continue
                            if time.monotonic() < active["next_decision"]:
                                continue
                            expected = control_context(snapshot)
                            previous_owned = owned
                            owned = True  # An uncertain transport response requires cleanup.
                            with self._lock:
                                self._report["flight_owned"] = True
                            phase = "Applying body-frame command"
                            try:
                                sim.set_command_checked(
                                    decision.accel, decision.angular_accel, snapshot
                                )
                            except StaleContextError:
                                # The engine guarantees that this command did not
                                # mutate flight. Never Stop/Pause a native takeover.
                                owned = previous_owned
                                recover_stale()
                                continue
                            active["decision_time"] = snapshot["observation"]["time_s"]
                            active["last_decision_wall"] = tick_started
                            active["next_decision"] = tick_started + 1 / active["cadence"]
                            with self._lock:
                                self._report.update(
                                    state="running"
                                    if snapshot["runtime"]["running"]
                                    else "starting",
                                    run_request_state="running"
                                    if snapshot["runtime"]["running"]
                                    else "starting",
                                    last_command_time_s=active["decision_time"],
                                    last_command_policy_revision=revision,
                                    last_command_context=expected,
                                )
                            if not snapshot["runtime"]["running"]:
                                phase = "Starting real-time playback"
                                try:
                                    sim.play_checked(snapshot)
                                except StaleContextError:
                                    recover_stale()
                                    continue
                                active.update(
                                    awaiting_play=True,
                                    play_deadline=min(active["deadline"], time.monotonic() + 10),
                                )
                        publish("running")
                if not self._stop.is_set() and time.monotonic() - last_publication_wall >= 1.0:
                    # A connection heartbeat holds the original plan/source time.
                    # It does not rerun A*, fabricate readings or query cameras.
                    error = self.status()["error"]
                    publish("running" if owned else "error" if error else "watching", failure=error)
                self._wake.wait(1 / poll_hz)
                self._wake.clear()
        except BaseException as error:
            fatal = _failure(phase, error)
        finally:
            if active is not None or owned:
                fatal = finish_run(fatal)
            # A watch-only disconnect never clears force, pauses or claims flight.
            # It may replace its own advisory so native HUD ownership stays honest.
            try:
                if fatal:
                    route, preview = None, None
                if fatal and (snapshot is None or policy is None):
                    sim.preview(None)
                else:
                    publish("error" if fatal else "stopped", failure=fatal)
            except BaseException as error:
                fatal = "; ".join(
                    filter(None, (fatal, _failure("Final guidance publication", error)))
                )
                with suppress(BaseException):
                    sim.preview(None)
            with self._lock:
                self._live = False
                self._run_request = None
                self._report.update(
                    state="error" if fatal else "finished",
                    worker_active=False,
                    controller_running=False,
                    flight_owned=False,
                    run_request_state="failed" if fatal else "stopped",
                    error=fatal,
                )

    def _run(
        self,
        sim: LocalSimulation,
        policy: Callable[[dict], Decision],
        duration_s: float,
        cadence_hz: float,
    ) -> None:
        deadline = time.monotonic() + 5 * duration_s + 15
        origin: float | None = None
        last_time = 0.0
        seed: int | None = None
        interventions: int | None = None
        phase = "Starting experiment"
        owned = False
        errors: list[str] = []

        def cancelled() -> bool:
            if self._stop.is_set():
                return True
            if time.monotonic() >= deadline:
                raise _RunError("Wall-time limit reached without completing the observed duration")
            return False

        def sample(running: bool | None = None, *, cleanup: bool = False) -> dict:
            nonlocal origin, last_time, seed, interventions, phase
            phase = "Reading public snapshot"
            snapshot = sim.snapshot()
            self.record_snapshot(snapshot, source="worker")
            observation, runtime = snapshot.get("observation"), snapshot.get("runtime")
            if not isinstance(observation, dict) or not isinstance(runtime, dict):
                raise _RunError("Snapshot lacks public observation or runtime state")
            now = observation.get("time_s")
            current_seed = observation.get("seed")
            counter = observation.get("manual_interventions", runtime.get("manual_interventions"))
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise _RunError("Snapshot lacks a finite non-negative simulation time")
            if type(current_seed) is not int or current_seed < 0:
                raise _RunError("Snapshot lacks a simulation seed")
            if type(counter) is not int or not 0 <= counter <= 0xFFFFFFFF:
                raise _RunError("Snapshot lacks the real u32 manual-intervention counter")
            if type(runtime.get("running")) is not bool:
                raise _RunError("Snapshot lacks actual runtime playback state")
            if origin is None:
                origin, last_time, seed, interventions = (
                    float(now),
                    float(now),
                    current_seed,
                    counter,
                )
            elif not cleanup:
                if counter != interventions:
                    raise _RunError("Manual intervention detected; yielding command ownership")
                if current_seed != seed or now < last_time:
                    raise _RunError("Simulation seed changed or time rewound; ending experiment")
            if current_seed == seed and now >= last_time:
                last_time = float(now)
                with self._lock:
                    self._report["elapsed_sim_s"] = last_time - origin
            if running is not None and runtime["running"] != running:
                if cleanup:
                    raise _RunError("Cleanup could not confirm that actual runtime is paused")
                raise _RunError(
                    "Runtime paused after playback began; yielding command ownership"
                    if running
                    else "Runtime resumed while policy initialization was paused"
                )
            return snapshot

        def apply(snapshot: dict, *, running: bool) -> bool:
            nonlocal phase
            if cancelled():
                return False
            phase = "Policy callback"
            try:
                decision = policy(deepcopy(snapshot["observation"]))
            except BaseException as error:
                raise _RunError(
                    f"Policy callback failed: {type(error).__name__}. "
                    "Exception text omitted for privacy."
                ) from None
            if not isinstance(decision, Decision):
                raise _RunError("Policy must return a Decision")
            if cancelled():
                return False
            # Planning may be expensive. Never write from its now-stale input
            # without rereading the actual manual counter and playback state.
            sample(running)
            if cancelled() or last_time - origin >= duration_s:
                return False
            if decision.preview is not None:
                phase = "Publishing Python preview"
                sim.preview(decision.preview)
                if cancelled():
                    return False
                sample(running)
            if cancelled() or last_time - origin >= duration_s:
                return False
            phase = "Applying body-frame command"
            sim.SetCommand(
                np.array(decision.accel, dtype=np.float64, copy=True),
                np.array(decision.angular_accel, dtype=np.float64, copy=True),
            )
            return True

        try:
            if cancelled():
                return
            owned = True  # Even an uncertain first pause response needs cleanup.
            phase = "Pausing actual runtime"
            sim.pause()
            if cancelled():
                return
            phase = "Clearing previous requested forces"
            sim.stop()
            if cancelled():
                return
            snapshot = sample(False)
            tick_started = time.monotonic()
            if not apply(snapshot, running=False):
                return
            sample(False)
            if cancelled():
                return
            phase = "Starting real-time playback"
            sim.command({"op": "play", "rate": 1})
            play_deadline = min(deadline, time.monotonic() + 10.0)
            while True:
                if cancelled():
                    return
                if time.monotonic() >= play_deadline:
                    raise _RunError(
                        "Play was acknowledged but actual runtime did not start in time"
                    )
                snapshot = sample()
                if snapshot["runtime"]["running"]:
                    break
                self._stop.wait(0.05)
            with self._lock:
                if not self._stop.is_set():
                    self._report["state"] = "running"
            next_tick = tick_started + 1 / cadence_hz
            while not cancelled():
                remaining = duration_s - (last_time - origin)
                if remaining <= 0:
                    return
                # Safety/duration reads continue between low-cadence policy
                # calls. Missed deadlines never produce catch-up command bursts.
                wait_s = min(0.25, max(0.0, next_tick - time.monotonic()), remaining)
                if self._stop.wait(wait_s):
                    return
                snapshot = sample(True)
                if last_time - origin >= duration_s:
                    return
                if time.monotonic() < next_tick:
                    continue
                tick_started = time.monotonic()
                if not apply(snapshot, running=True):
                    return
                next_tick = tick_started + 1 / cadence_hz
        except BaseException as error:
            errors.append(_failure(phase, error))
        finally:
            with self._lock:
                self._report.update(state="stopping", error="; ".join(errors) or None)
            if owned:
                # These are distinct safety operations, not retries of thrust or
                # Play. Try Pause even when Stop delivery cannot be established.
                for label, action in (("Cleanup Stop", sim.stop), ("Cleanup Pause", sim.pause)):
                    try:
                        action()
                    except BaseException as error:
                        errors.append(_failure(label, error))
                        with self._lock:
                            self._report["error"] = "; ".join(errors)
                try:
                    sample(False, cleanup=True)
                except BaseException as error:
                    errors.append(_failure("Cleanup snapshot", error))
            with self._lock:
                self._report.update(
                    state="error" if errors else "finished", error="; ".join(errors) or None
                )
