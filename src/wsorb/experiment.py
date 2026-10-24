"""Explicit, local-only policy runs against an existing browser simulation.

Policies receive public observations and return body-frame SI commands. This
module supplies no controller, planner, physics or STP SDK implementation.
Only the worker writes commands. Stop signals it rather than starting a second
writer, and a slow Python callback cannot be interrupted. Keep callbacks bounded.

Cadence is a wall-clock upper target, not a promised HTTP or physics frequency.
``sample_count`` counts recorded public snapshots, including safety and cleanup reads,
not simulation steps. ``elapsed_sim_s`` comes only from a continuous observed
simulation clock. A browser pause, reset or manual intervention ends ownership.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from numbers import Real
from typing import Any

import numpy as np
import numpy.typing as npt

from .local_api import LocalSimulation

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
        "ship_mass_kg": _finite(ship.get("mass_kg")),
        "reference_accel_limit_mps2": _finite(observation.get("max_accel_mps2")),
        "reference_angular_accel_limit_rad_s2": _finite(
            observation.get("max_angular_accel_rad_s2")
        ),
        **{name: _finite(metrics.get(name)) for name in TELEMETRY_FIELDS},
    }
    for field, prefix, unit in (
        ("commanded_accel_mps2", "command_accel_body", "mps2"),
        ("commanded_angular_accel_rad_s2", "command_angular_accel_body", "rad_s2"),
    ):
        vector = ship.get(field)
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


class _RunError(RuntimeError):
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


class ExperimentRunner:
    """One explicitly started worker and a thread-safe, read-only status report."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._history: deque[dict[str, Any]] = deque(maxlen=HISTORY_LIMIT)
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
                if self._report["state"] not in ("finished", "error"):
                    self._report["state"] = "stopping"
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout_s)
        return self.status()

    def status(self) -> dict:
        """Copy actual reports without contacting the browser or exposing a client."""
        with self._lock:
            return deepcopy(self._report)

    def history(self) -> list[dict[str, Any]]:
        """Copy at most 2000 distinct measured telemetry samples; never contact a client."""
        with self._lock:
            return deepcopy(list(self._history))

    def record_snapshot(self, snapshot: dict, *, source: str = "manual_read") -> None:
        """Record an actual public read, including explicit paused-step results.

        This does not advance time or send a command. The notebook calls it only
        with real client results. Repeated held telemetry replaces the last row,
        rather than creating new measurements at observation or wall-clock time.
        ``segment`` splits runs, epochs, clock/frame changes and interventions.
        """
        row = measured_sample(snapshot)
        with self._lock:
            self._report["latest"] = deepcopy(snapshot)
            self._report["sample_count"] += 1
            if row is None:
                return
            previous = self._history[-1] if self._history else None
            intervention = bool(
                previous is not None
                and row["manual_interventions"] is not None
                and previous["manual_interventions"] is not None
                and row["manual_interventions"] != previous["manual_interventions"]
            )
            boundary = self._new_run or intervention
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
            )
            if previous is not None and all(
                row[key] == previous[key]
                for key in ("telemetry_epoch", "telemetry_seed", "telemetry_time_s")
            ):
                row["manual_intervention"] |= previous["manual_intervention"]
                if not intervention:
                    row["manual_intervention_source"] = previous["manual_intervention_source"]
                self._history[-1] = row
            else:
                self._history.append(row)

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
