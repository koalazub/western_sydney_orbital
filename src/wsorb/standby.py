"""Same-host process standby for an explicitly consented resilient local run.

The spawned peer runs checked-in ``wsorb.flight`` algorithms and data settings.
It never receives notebook closures, hidden physics data or a copied estimator.
Native epoch/term fencing, not Python IPC, chooses the only command writer.
This tolerates a controller process failure, not a host or region failure.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import re
import socket
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from numbers import Real
from typing import Any
from uuid import uuid4

import numpy as np

from .flight import (
    PlannerConfig,
    ResilienceConfig,
    ResilientGuidance,
    plan_route,
    predict_obstacles,
)
from .local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalBusyError,
    LocalSimulation,
    StaleContextError,
    control_context,
)

ALGORITHM_SOURCE = (
    "Checked-in wsorb.flight ResilientGuidance, predict_obstacles and plan_route "
    "with data settings. "
    "Unsaved notebook prediction/search closures are not replicated."
)


def control_authority(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an actual native report without default epochs or invented leaders."""
    observation = snapshot.get("observation")
    authority = observation.get("control_authority") if isinstance(observation, Mapping) else None
    fields = {"epoch", "term", "status", "run_id", "controller_id", "expires_at_sim_time_s"}
    if (
        not isinstance(authority, Mapping)
        or not fields.issubset(authority)
        or set(authority) - fields - {"expires_at_runtime_s"}
    ):
        raise ValueError("Resilient Run requires the native control_authority report")
    for name in ("epoch", "term"):
        if type(authority[name]) is not int or not 0 <= authority[name] <= 0xFFFFFFFF:
            raise ValueError("Invalid native control authority counter")
    status = authority["status"]
    if not isinstance(status, str) or status not in ("idle", "active", "expired", "revoked"):
        raise ValueError("Invalid native control authority status")
    for name in ("run_id", "controller_id"):
        value = authority[name]
        if value is not None and (
            not isinstance(value, str)
            or not 1 <= len(value) <= 64
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None
        ):
            raise ValueError("Invalid native controller identity")
    for name in ("expires_at_sim_time_s", "expires_at_runtime_s"):
        expiry = authority.get(name)
        if expiry is None:
            continue
        try:
            if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry < 0:
                raise ValueError("Invalid native authority expiry")
        except OverflowError:
            raise ValueError("Invalid native authority expiry") from None
    if status in ("active", "expired") and any(
        authority[name] is None for name in ("run_id", "controller_id", "expires_at_sim_time_s")
    ):
        raise ValueError("Native authority omitted its actual owner or expiry")
    return deepcopy(dict(authority))


def controller_fence(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Copy exactly the four fence fields from a successful native claim receipt."""
    authority = control_authority(snapshot)
    if authority["status"] != "active":
        raise ValueError("A controller fence requires an actual active native grant")
    return {name: authority[name] for name in ("epoch", "term", "run_id", "controller_id")}


def known_busy(error: BaseException) -> bool:
    """Only an explicit pre-dispatch rejection is known contention, never error text."""
    return isinstance(error, LocalBusyError)


def read_public_snapshot(sim: LocalSimulation, stop: Any) -> dict[str, Any]:
    """Retry only known-busy reads briefly. Unknown failures are never retried here."""
    deadline = time.monotonic() + min(0.5, sim.timeout_s)
    while True:
        try:
            return sim.snapshot()
        except LocalApiError as error:
            if not known_busy(error) or time.monotonic() >= deadline or stop.is_set():
                raise
            stop.wait(0.025)


def standby_policy_data(
    planner: PlannerConfig,
    resilience: ResilienceConfig,
    gains: Mapping[str, Any],
    revision: int,
) -> dict[str, Any]:
    """Copy only versioned algorithm data. Arbitrary notebook config never crosses IPC."""
    if (
        not isinstance(planner, PlannerConfig)
        or not isinstance(resilience, ResilienceConfig)
        or not isinstance(gains, Mapping)
    ):
        raise ValueError("Standby requires PlannerConfig and ResilienceConfig data")
    values = {}
    for name, default in (
        ("waypoint_arrival_m", 20.0),
        ("position_gain_s2", 0.0012),
        ("velocity_gain_s", 0.07),
    ):
        value = gains.get(name, default)
        if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
            raise ValueError("Standby gains must be finite non-negative real numbers")
        try:
            value = float(value)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError("Standby gains must be finite non-negative real numbers") from error
        if not math.isfinite(value) or value < 0 or (name == "waypoint_arrival_m" and value == 0):
            raise ValueError("Standby gains must be finite non-negative real numbers")
        values[name] = value
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid standby policy revision")
    return json.loads(
        json.dumps(
            {
                "planner": asdict(planner),
                "resilience": asdict(resilience),
                "gains": values,
                "policy_revision": revision,
            },
            allow_nan=False,
        )
    )


def _configuration(data: Mapping[str, Any]) -> tuple[PlannerConfig, ResilienceConfig]:
    planner = PlannerConfig(**data["planner"])
    resilience = dict(data["resilience"])
    resilience["planner"] = PlannerConfig(**resilience["planner"])
    return planner, ResilienceConfig(**resilience)


class _StopSignal:
    """One shared byte, without a semaphore a killed process could leave locked."""

    def __init__(self, context: Any) -> None:
        self._flag = context.RawValue("b", 0)

    def set(self) -> None:
        self._flag.value = 1

    def is_set(self) -> bool:
        return self._flag.value != 0

    def wait(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.025, remaining))
        return True


class _DataChannel:
    """Bounded JSON messages over a nonblocking stream, with no queue feeder locks.

    Partial frames remain private buffers. Killing either process cannot leave
    its peer blocked in recv or send. EOF stops IPC, not the original Run consent.
    One partial outgoing frame and one replaceable latest frame bound storage.
    """

    _MAX_BYTES = 128 * 1024

    def __init__(self, endpoint: socket.socket) -> None:
        self._socket = endpoint
        endpoint.setblocking(False)
        self._incoming = bytearray()
        self._outgoing = b""
        self._latest = b""
        self._closed = False

    @property
    def pending(self) -> bool:
        return bool(self._outgoing or self._latest)

    def close(self) -> None:
        self._closed = True
        self._socket.close()
        self._incoming.clear()
        self._outgoing = self._latest = b""

    def flush(self) -> None:
        while not self._closed:
            if not self._outgoing:
                self._outgoing, self._latest = self._latest, b""
            if not self._outgoing:
                return
            try:
                sent = self._socket.send(self._outgoing)
            except BlockingIOError:
                return
            except OSError:
                self.close()
                return
            if sent == 0:
                self.close()
                return
            self._outgoing = self._outgoing[sent:]

    def offer(self, value: dict[str, Any]) -> bool:
        if self._closed:
            return False
        payload = json.dumps(value, allow_nan=False).encode("utf-8")
        if len(payload) > self._MAX_BYTES:
            raise ValueError("Standby data message exceeds its bounded IPC budget")
        self._latest = len(payload).to_bytes(4, "big") + payload
        self.flush()
        return not self._closed

    def receive(self) -> dict[str, Any] | None:
        self.flush()
        latest = None
        # A busy producer cannot hold a caller here indefinitely.
        for _ in range(8):
            if self._closed:
                break
            try:
                chunk = self._socket.recv(64 * 1024)
            except BlockingIOError:
                break
            except OSError:
                self.close()
                break
            if not chunk:
                self.close()
                break
            self._incoming.extend(chunk)
            while len(self._incoming) >= 4:
                length = int.from_bytes(self._incoming[:4], "big")
                if not 0 < length <= self._MAX_BYTES:
                    self.close()
                    return latest
                if len(self._incoming) < length + 4:
                    break
                frame = bytes(self._incoming[4 : length + 4])
                del self._incoming[: length + 4]
                try:
                    value = json.loads(frame)
                except (ValueError, UnicodeError):
                    self.close()
                    return latest
                if not isinstance(value, dict):
                    self.close()
                    return latest
                latest = value
        return latest


class StandbyCohort:
    """Parent-side lifecycle and bounded data IPC, never a second writing thread."""

    def __init__(
        self,
        sim: LocalSimulation,
        active: Mapping[str, Any],
        policy_data: dict[str, Any],
        *,
        poll_hz: float,
        replan_s: float,
    ) -> None:
        self.run_id = active["run_id"]
        self.consent_epoch = active["consent_epoch"]
        self.controller_id = uuid4().hex
        self.primary_controller_id = active["controller_id"]
        self._lock = threading.Lock()
        context = multiprocessing.get_context("spawn")
        self._stop = _StopSignal(context)
        parent, child = socket.socketpair()
        self._channel = _DataChannel(parent)
        self._last: dict[str, Any] = {"state": "starting", "algorithm_source": ALGORITHM_SOURCE}
        self._pending: dict[str, Any] | None = None
        self._sent: dict[str, Any] | None = None
        # Credentials stay in private process memory and are never in a report.
        connection = {"url": sim.url, "token": sim._token, "timeout_s": sim.timeout_s}
        self._process = context.Process(
            target=_serve_standby,
            args=(
                connection,
                deepcopy(dict(active)),
                self.controller_id,
                deepcopy(policy_data),
                poll_hz,
                replan_s,
                self._stop,
                child,
            ),
            name="wsorb-warm-standby",
            daemon=False,
        )
        try:
            self._process.start()
        except BaseException:
            self._channel.close()
            raise
        finally:
            child.close()

    @property
    def alive(self) -> bool:
        return self._process.is_alive()

    def update(self, policy_data: dict[str, Any], limits: Mapping[str, Any]) -> None:
        packet = {"policy": deepcopy(policy_data), "limits": dict(limits)}
        with self._lock:
            if packet != self._sent:
                self._pending = packet
            if self._pending is not None and self._channel.offer(self._pending):
                self._sent, self._pending = self._pending, None

    def request_stop(self) -> None:
        self._stop.set()

    def join(self, timeout_s: float) -> None:
        self._process.join(timeout_s)
        if not self.alive:
            with self._lock:
                report = self._channel.receive()
                if report is not None:
                    self._last = report
                self._channel.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            # Reports are data, not heartbeats that grant flight consent.
            report = self._channel.receive()
            if report is not None:
                self._last = report
            alive = self.alive
            state = self._last["state"]
            if not alive and state not in ("error", "stopped", "yielded", "completed"):
                state = "exited" if self._process.exitcode == 0 else "failed"
            return {
                **deepcopy(self._last),
                "state": state,
                "pid": self._process.pid,
                "alive": alive,
                "exitcode": self._process.exitcode,
                "stop_requested": self._stop.is_set(),
                "controller_id": self.controller_id,
                "run_id": self.run_id,
                "consent_epoch": self.consent_epoch,
                "isolation": "same_host_process_only_not_cross_region_availability",
                "settings_pending": self._pending is not None or self._channel.pending,
            }


def _serve_standby(connection, active, identity, data, poll_hz, replan_s, stop, endpoint):
    # Imported here so experiment can use the data/lifecycle helpers without a
    # module import cycle. No parent policy, client object or closure is unpickled.
    from .experiment import (
        _guidance_key,
        _preview_controller,
        controller_interruption,
        guidance_due,
        retime_run,
    )

    channel = _DataChannel(endpoint)
    sim = LocalSimulation(**connection)
    planner, config = _configuration(data)
    guidance = ResilientGuidance(config)
    route, previous_plan = None, None
    snapshot = None
    report = {
        "state": "warming",
        "pid": os.getpid(),
        "algorithm_source": ALGORITHM_SOURCE,
        "policy_revision": data["policy_revision"],
        "run_origin_time_s": active["origin"],
        "wall_origin_s": active["wall_origin"],
        "plan_count": 0,
        "last_command_time_s": None,
        "last_command_fence": None,
        "resilience": None,
        "error": None,
    }
    release = False
    awaiting_play = active["awaiting_play"]
    play_deadline = active["play_deadline"]
    fence = None
    pending = None
    active.update(next_decision=0.0, decision_time=None, last_decision_wall=None)
    try:
        while not stop.is_set():
            message = channel.receive()
            if message is not None:
                pending = message
            try:
                snapshot = read_public_snapshot(sim, stop)
            except LocalApiError as error:
                if not known_busy(error):
                    raise
                stop.wait(1 / poll_hz)
                continue
            observation, runtime = snapshot["observation"], snapshot["runtime"]
            authority = control_authority(snapshot)
            report.update(
                control_authority=authority,
                authority_source_time_s=observation["time_s"],
                elapsed_sim_s=observation["time_s"] - active["origin"],
            )
            interruption = controller_interruption(active, observation, runtime)
            if interruption:
                report.update(state="yielded", notice=interruption)
                break
            active["last_time"] = observation["time_s"]
            if runtime["running"]:
                active["expect_running"] = True
                awaiting_play = False
            if pending is not None:
                from .experiment import RunLimits

                new_data, limits = pending["policy"], pending["limits"]
                new_planner, new_config = _configuration(new_data)
                if new_config != config or new_planner != planner:
                    guidance = ResilientGuidance(new_config)
                    route, previous_plan = None, None
                elif new_data != data:
                    route, previous_plan = None, None
                data, planner, config = new_data, new_planner, new_config
                active = retime_run(
                    active,
                    RunLimits(limits["duration_s"], limits["cadence_hz"]),
                    now_wall_s=time.monotonic(),
                )
                report.update(
                    policy_revision=data["policy_revision"],
                    run_limits_revision=limits["revision"],
                )
                pending = None
            report["run_limits"] = {
                "duration_s": active["duration"],
                "requested_cadence_hz": active["cadence"],
            }
            if (
                observation["time_s"] - active["origin"] >= active["duration"]
                or time.monotonic() >= active["deadline"]
            ):
                release = True
                report.update(state="completed", notice="Original run limit reached")
                break
            ready = guidance.update(
                observation, wall_time_s=time.monotonic(), running=runtime["running"]
            )
            if ready.requires_replan:
                route, previous_plan = None, None
            if guidance_due(previous_plan, observation, data["policy_revision"], replan_s):
                previous_plan = _guidance_key(observation, data["policy_revision"])
                route = None
                if ready.can_command:
                    try:
                        frame = guidance.frame_for_planning(
                            predict_obstacles(ready.observation, planner)
                        )
                        route = plan_route(frame, planner)
                        report["plan_error"] = None
                    except (KeyError, TypeError, ValueError, OverflowError) as error:
                        report["plan_error"] = (
                            f"Planning rejected: {type(error).__name__}. Text omitted."
                        )
                report.update(
                    plan_count=report["plan_count"] + 1,
                    last_plan_time_s=observation["time_s"],
                    last_plan=route.to_preview() if route is not None else None,
                )
            accel, angular = guidance.command(ready.observation, route, data["gains"])
            lease = ready.lease_sim_s
            if lease == 0:
                accel, angular, lease = np.zeros(3), np.zeros(3), config.lease_sim_s
            if route is not None and (np.any(accel) or np.any(angular)):
                lease = min(
                    lease,
                    route.frame.source_time_s
                    + route.frame.forecast_horizon_s
                    - observation["time_s"],
                )
                if not math.isfinite(lease) or lease < 0.05:
                    accel, angular, lease = np.zeros(3), np.zeros(3), 0.05
            report.update(
                resilience=guidance.report,
                last_decision_source_time_s=observation["time_s"],
                last_decision_accel_body_mps2=np.asarray(accel).tolist(),
            )
            primary_or_other_leader = (
                authority["status"] == "active" and authority["controller_id"] != identity
            )
            report["state"] = (
                "following"
                if primary_or_other_leader
                else "leading"
                if authority["status"] == "active"
                else "awaiting_native_claim"
            )
            channel.offer(report)
            if (
                primary_or_other_leader
                or time.monotonic() < active["next_decision"]
                or active["decision_time"] == observation["time_s"]
                or (awaiting_play and not runtime["running"])
            ):
                if awaiting_play and time.monotonic() >= play_deadline:
                    release = True
                    report.update(state="error", error="Checked Play did not start in time")
                    break
                stop.wait(1 / poll_hz)
                continue
            # A late planner result never gets attached to this newer source clock.
            current = read_public_snapshot(sim, stop)
            if stop.is_set():
                break
            report.update(
                control_authority=control_authority(current),
                authority_source_time_s=current["observation"]["time_s"],
            )
            if controller_interruption(active, current["observation"], current["runtime"]):
                report["state"] = "yielded"
                break
            current_authority = control_authority(current)
            if (
                current["observation"]["time_s"] - active["origin"] >= active["duration"]
                or time.monotonic() >= active["deadline"]
            ):
                release = True
                report.update(state="completed", notice="Original run limit reached")
                break
            if (
                current_authority["status"] == "active"
                and current_authority["controller_id"] != identity
            ):
                continue
            if (
                _guidance_key(current["observation"], data["policy_revision"])[:-1]
                != _guidance_key(observation, data["policy_revision"])[:-1]
                or current["observation"]["time_s"] >= observation["time_s"] + lease
            ):
                route, previous_plan = None, None
                continue
            try:
                grant = current
                if current_authority["status"] != "active":
                    grant = sim.claim_control(
                        active["run_id"],
                        identity,
                        current,
                        expected_epoch=active["consent_epoch"],
                        lease_sim_s=config.lease_sim_s,
                    )
                    fence = controller_fence(grant)
                elif fence is None or fence != controller_fence(current):
                    raise ValueError("Peer has no matching actual claim receipt")
                if (
                    fence["epoch"] != active["consent_epoch"]
                    or fence["run_id"] != active["run_id"]
                    or fence["controller_id"] != identity
                ):
                    raise ValueError("Native claim did not grant this peer authority")
                if stop.is_set():
                    break
                sim.set_leased_command(
                    accel, angular, snapshot, lease_sim_s=lease, controller_fence=fence
                )
                if not current["runtime"]["running"]:
                    sim.play_checked(grant)
                    awaiting_play = True
                    play_deadline = min(active["deadline"], time.monotonic() + 10)
            except (StaleContextError, CommandExpiredError):
                # Native rejection proves no mutation. Recompute from a later read,
                # never retry this decision or adopt a new consent epoch.
                route, previous_plan = None, None
                stop.wait(1 / poll_hz)
                continue
            tick = time.monotonic()
            active.update(
                decision_time=observation["time_s"],
                last_decision_wall=tick,
                next_decision=tick + 1 / active["cadence"],
            )
            report.update(
                control_authority=control_authority(grant),
                authority_source_time_s=grant["observation"]["time_s"],
                last_command_time_s=observation["time_s"],
                last_command_fence=fence,
                last_command_policy_revision=data["policy_revision"],
                last_command_context=control_context(snapshot),
                state="leading",
            )
            # Only this actual leader publishes its own route. No parent route
            # is relabelled as a standby decision.
            packet = None
            if route is not None:
                packet = route.to_preview()
                packet["execution"] = {
                    "state": "running",
                    "controller": _preview_controller("resilient_waypoint_pd"),
                    "source_time_s": observation["time_s"],
                    "source_seed": observation["seed"],
                    "trial_revision": observation["trial_revision"],
                    "tuning_revision": observation["tuning_revision"],
                    "next_waypoint_world_m": guidance.report.get("next_waypoint_world_m"),
                }
            try:
                sim.preview(packet)
            except StaleContextError:
                route, previous_plan = None, None
            except LocalApiError as error:
                if not known_busy(error):
                    raise
            channel.offer(report)
            stop.wait(1 / poll_hz)
    except BaseException as error:
        # Never revoke a healthy primary because this peer failed. Unknown
        # actuation outcomes are not retried; the native watchdog bounds them.
        report.update(state="error", error=f"Standby failed: {type(error).__name__}. Text omitted.")
    finally:
        if stop.is_set() or release:
            try:
                current = read_public_snapshot(sim, stop)
                report.update(
                    control_authority=control_authority(current),
                    authority_source_time_s=current["observation"]["time_s"],
                )
                sim.release_control(active["run_id"], active["consent_epoch"], current)
                report["release_state"] = "acknowledged"
                after = read_public_snapshot(sim, stop)
                report.update(
                    control_authority=control_authority(after),
                    authority_source_time_s=after["observation"]["time_s"],
                )
            except StaleContextError:
                report["release_state"] = "rejected_after_takeover_no_fallback"
            except BaseException as error:
                report["release_state"] = f"unconfirmed_{type(error).__name__}_native_watchdog"
        if stop.is_set() and report["state"] not in ("error", "yielded", "completed"):
            report["state"] = "stopped"
        channel.offer(report)
        channel.close()
