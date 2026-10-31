"""Actual controller flights and process loss against the private headless native/WASM server.

No browser, renderer, SDK, network, sensor or physics result is replaced. Each
test starts its own server, flight and controllers. Pause uses the real native
Pause operation, not an automated keyboard/browser claim. This proves only
same-host process isolation, not cross-region or licensed-runtime availability.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import shutil
import struct
import subprocess
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from wsorb.experiment import (
    ExperimentRunner,
    FlightStateError,
    LivePolicy,
    _guidance_key,
    advance_controller_tick,
    controller_lease_within_run,
    controller_ownership_renewal_lease,
    controller_poll_delay,
    controller_source_can_wait,
    guidance_due,
)
from wsorb.flight import ResilienceConfig, coast_command, plan_route, predict_obstacles
from wsorb.flight.control import _bounded
from wsorb.local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalSimulation,
    StaleContextError,
    control_context,
    validate_experiment_settings,
)
from wsorb.standby import ALGORITHM_SOURCE, control_authority, controller_fence, known_busy

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("work_s, expected_s", [(0.03, 0.07), (0.1, 0.0), (0.12, 0.0)])
def test_controller_poll_schedule_counts_computation_inside_the_requested_period(
    work_s, expected_s
):
    # Clock arithmetic only. These numbers do not stand in for engine samples.
    active = {"next_decision": 10.1, "decision_time": 3.0, "last_time": 3.1}
    assert controller_poll_delay(active, 10.0, 10.0 + work_s) == pytest.approx(expected_s)
    assert controller_poll_delay(active, 10.0, 10.0 + work_s, can_decide=False) == 0.1
    active["last_time"] = active["decision_time"]
    assert controller_poll_delay(active, 10.0, 11.0) == 0.1
    assert controller_poll_delay(active, 10.0, 10.04) == pytest.approx(0.06)


def test_precommand_validation_is_not_a_postcommand_held_clock_read():
    # Scheduling arithmetic, not a fabricated public engine observation. An ACK
    # can arrive after the tick deadline even if the preceding read was held.
    active = {
        "next_decision": 10.1,
        "decision_time": 3.0,
        "last_time": 3.0,
        "command_pending_read": True,
    }
    assert controller_poll_delay(active, 10.0, 10.04) == pytest.approx(0.06)
    assert controller_poll_delay(active, 10.0, 10.12) == 0.0
    active["command_pending_read"] = False
    assert controller_poll_delay(active, 10.0, 10.12) == 0.1


def test_measured_acquisition_precedes_the_strict_calculation_slot():
    # Scheduler arithmetic only. No wall or engine clock is replaced. Input work
    # must finish near the deadline rather than begin after the full interval.
    active = {
        "cadence": 10.0,
        "last_decision_wall": 10.02,
        "decision_time": 1.0,
        "last_time": 1.1,
    }
    advance_controller_tick(active, 10.1, 10.12)
    assert active["input_prepare_wall_s"] == pytest.approx(0.02)
    assert active["next_poll"] == pytest.approx(10.2)
    assert active["next_decision"] == pytest.approx(10.22)
    assert controller_poll_delay(active, 10.0, 10.16) == pytest.approx(0.04)
    # Preparation ends at .215; the .005 cadence hold is not acquisition cost.
    advance_controller_tick(active, 10.2, 10.22, prepared_wall_s=10.215)
    assert active["last_poll_wall"] == 10.2
    assert active["input_prepare_wall_s"] == pytest.approx(0.015)
    assert active["next_poll"] == pytest.approx(10.305)
    assert active["next_decision"] == pytest.approx(10.32)


@pytest.mark.parametrize("previous", [None, 9.0])
def test_initial_and_overrun_input_work_never_schedule_a_catchup_calculation(previous):
    active = {"cadence": 10.0, "last_decision_wall": previous}
    advance_controller_tick(active, 10.0, 10.12)
    assert active["last_tick_wall"] == 10.12
    assert active["next_poll"] == pytest.approx(10.12)
    assert active["next_decision"] == pytest.approx(10.22)


def test_running_timed_acquisition_does_not_sleep_through_the_renewal_window():
    # Scheduling arithmetic only. Repeated real reads may precede a calculation,
    # but none advances or bypasses the 100 ms calculation floor.
    active = {"cadence": 10.0, "continuous_source_reads": True}
    advance_controller_tick(active, 10.1, 10.12)
    deadline = active["next_decision"]
    for now in (10.14, 10.16, 10.2, 10.219, 10.23):
        assert controller_poll_delay(active, 10.0, now) == 0.0
        assert active["next_decision"] == deadline
    assert deadline == pytest.approx(10.22)
    assert controller_poll_delay(active, 10.0, 10.14, can_decide=False) == 0.1
    active["awaiting_play"] = True
    assert controller_poll_delay(active, 10.0, 10.14) == 0.1


def test_acquisition_does_not_replace_the_previous_actual_calculation_ceiling():
    active = {"cadence": 10.0, "continuous_source_reads": True}
    advance_controller_tick(active, 10.1, 10.12)
    # An input acquired before the next floor does not permit a catch-up burst.
    assert active["next_decision"] == pytest.approx(10.22)
    advance_controller_tick(active, 10.215, 10.225, prepared_wall_s=10.224)
    assert active["last_decision_interval_wall_s"] == pytest.approx(0.105)
    assert active["next_decision"] == pytest.approx(10.325)


@pytest.mark.parametrize("authority_slack", [0.0, -0.005, -0.03])
def test_tight_installed_authority_budget_never_adds_an_acquisition_sleep(authority_slack):
    # Projected budgets, not fabricated flight observations. Reading a newer
    # source can improve its own age but cannot improve the installed fence.
    budget = {"source_slack_wall_s": 0.12, "authority_slack_wall_s": authority_slack}
    assert not controller_source_can_wait(budget)
    active = {
        "cadence": 10.0,
        "continuous_source_reads": True,
        "next_decision": 10.1,
        "next_poll": 10.09,
    }
    assert controller_poll_delay(active, 10.0, 10.1) == 0.0
    assert active["next_decision"] == 10.1


@pytest.mark.parametrize(
    "source_time, lease, within",
    [
        (12.0, 0.75, True),
        (31.0, 0.75, True),
        (31.25, 0.75, False),
        (31.3, 0.75, False),
        (31.9, 0.05, True),
        (32.0, 0.75, False),
        (math.inf, 0.75, False),
    ],
)
def test_original_native_source_deadline_proves_dispatch_precedes_the_run_limit(
    source_time, lease, within
):
    # Deadline arithmetic only, not replacement engine observations or clocks.
    active = {"origin": 12.0, "duration": 20.0}
    original = dict(active)
    assert controller_lease_within_run(active, source_time, lease) is within
    assert active == original


def test_hot_duration_changes_do_not_rebase_the_native_source_deadline_proof():
    active = {"origin": 12.0, "duration": 20.0}
    assert controller_lease_within_run(active, 31.0, 0.75)
    active["duration"] = 19.5
    assert not controller_lease_within_run(active, 31.0, 0.75)
    assert active["origin"] == 12.0


def test_local_command_clock_does_not_rebuild_an_unchanged_nonroute_preview():
    # Scheduling arithmetic only; no fabricated sensor eligibility or flight.
    initial = {"time_s": 1.0}
    previous = _guidance_key(initial, 1)
    later = {"time_s": 2.0}
    assert guidance_due(previous, later, 1, 0.25)
    assert not guidance_due(previous, later, 1, 0.25, local_motion=True)
    assert guidance_due(previous, later, 1, 0.25, local_motion=True, requires_replan=True)
    assert guidance_due(previous, later, 2, 0.25, local_motion=True)
    assert guidance_due(previous, {"time_s": 0.0}, 1, 0.25, local_motion=True)


def _diagnostics(state):
    """Actual public lifecycle fields, without dumping 500 radar rows or credentials."""
    latest = state.get("latest") or state
    observation = latest.get("observation", {})
    peer = state.get("standby") or {}
    return {
        **{
            key: state.get(key)
            for key in (
                "state",
                "worker_active",
                "controller_running",
                "error",
                "notice",
                "primary_controller_id",
                "command_count",
                "nonzero_command_count",
                "expired_decision_count",
                "command_rejection",
                "ownership_renewal_count",
                "last_ownership_renewal",
                "last_ownership_renewal_ack_wall_s",
                "ownership_renewal_rejection",
                "last_command_time_s",
                "last_decision_duration_wall_s",
                "last_decision_interval_wall_s",
                "last_command_validation_age_sim_s",
                "last_command_validation_kind",
                "acquisition_schedule",
                "planning_schedule",
                "policy_callback_counts",
                "primary_last_command_time_s",
                "primary_last_command_fence",
                "control_phase",
                "control_phase_started_wall_s",
                "last_poll_started_wall_s",
                "last_poll_duration_wall_s",
                "last_tick_work_wall_s",
                "last_poll_slack_wall_s",
                "last_decision_started_wall_s",
                "last_command_ack_wall_s",
                "last_command_ack_interval_wall_s",
                "command_discard_count",
                "preview_request_count",
                "advisory_schedule",
                "source_hold_budget",
                "dispatch_budget",
                "last_precommand_observation_time_s",
                "last_precommand_validation_kind",
                "last_precommand_source_age_sim_s",
                "last_precommand_source_slack_sim_s",
                "last_precommand_authority_status",
                "last_precommand_authority_term",
                "last_precommand_authority_slack_sim_s",
            )
        },
        "time_s": observation.get("time_s"),
        "mission": observation.get("mission"),
        "authority": observation.get("control_authority"),
        "command_watchdog": observation.get("command_watchdog"),
        "navigation_time_s": observation.get("navigation_sample_time_s"),
        "runtime": latest.get("runtime"),
        "renderer": latest.get("renderer"),
        "standby": {
            key: peer.get(key)
            for key in (
                "state",
                "alive",
                "error",
                "notice",
                "controller_id",
                "decision_schedule",
                "plan_count",
                "command_count",
                "nonzero_command_count",
                "expired_decision_count",
                "command_rejection",
                "ownership_renewal_count",
                "last_ownership_renewal",
                "last_ownership_renewal_ack_wall_s",
                "ownership_renewal_rejection",
                "last_decision_source_time_s",
                "last_decision_duration_wall_s",
                "last_decision_interval_wall_s",
                "last_command_validation_age_sim_s",
                "last_command_validation_kind",
                "acquisition_schedule",
                "planning_schedule",
                "control_phase",
                "last_poll_started_wall_s",
                "last_poll_duration_wall_s",
                "last_tick_work_wall_s",
                "last_poll_slack_wall_s",
                "last_command_ack_wall_s",
                "last_command_ack_interval_wall_s",
                "next_poll_wall_s",
                "next_decision_wall_s",
                "advisory_schedule",
                "source_hold_budget",
                "dispatch_budget",
                "last_precommand_observation_time_s",
                "last_precommand_validation_kind",
                "last_precommand_source_age_sim_s",
                "last_precommand_source_slack_sim_s",
                "last_precommand_authority_status",
                "last_precommand_authority_term",
                "last_precommand_authority_slack_sim_s",
            )
        },
    }


def _native_primary_thrust(snapshot, identity):
    """Require native ownership, the installed fence and actual applied force."""
    observation = snapshot["observation"]
    physics = observation.get("applied_physics") or {}
    application = physics.get("last_actuator_application") or {}
    watchdog = observation.get("command_watchdog") or {}
    fence = watchdog.get("controller_fence") or {}
    authority = control_authority(snapshot)
    force = application.get("force_body_n")
    return (
        authority["controller_id"] == identity
        and authority["status"] == "active"
        and watchdog.get("status") == "active"
        and fence == {key: authority[key] for key in ("epoch", "term", "run_id", "controller_id")}
        and isinstance(force, list)
        and len(force) == 3
        and any(value != 0 for value in force)
    )


def _until(read, predicate, *, timeout_s=10, diagnostic=None, poll_s=0.05):
    deadline = time.monotonic() + timeout_s
    value = {}
    while True:
        try:
            value = read()
            if predicate(value):
                return value
        except LocalApiError as error:
            if not known_busy(error):
                if diagnostic is not None:
                    diagnostic(value)
                raise
        except Exception:
            if diagnostic is not None:
                diagnostic(value)
            raise
        if time.monotonic() >= deadline and diagnostic is not None:
            diagnostic(value)
        assert time.monotonic() < deadline, json.dumps(_diagnostics(value), indent=2)
        time.sleep(poll_s)


def test_bounded_angular_vector_passes_the_actual_javascript_and_native_lease_boundary(headless):
    sim, _ = headless
    before = sim.snapshot()
    angular = _bounded(
        (0.022299939842492084, -0.04462785567072277, -0.0033267373287309302),
        0.05,
    )
    sim.set_leased_command((0.0, 0.0, 0.0), angular, before, lease_sim_s=0.75)
    actual = sim.snapshot()["observation"]
    assert actual["command_watchdog"]["status"] == "active"
    assert actual["command_watchdog"]["source_time_s"] == before["observation"]["time_s"]
    assert actual["command_watchdog"]["expires_at_sim_time_s"] == pytest.approx(
        before["observation"]["time_s"] + 0.75,
    )
    np_angular = actual["applied_physics"]["requested"]["angular_accel_body_rad_s2"]
    assert math.hypot(*np_angular) <= 0.05
    assert np_angular == pytest.approx(angular.tolist(), abs=1e-15)
    assert actual["manual_interventions"] == before["observation"]["manual_interventions"]


@pytest.fixture
def headless(tmp_path):
    node = shutil.which("node")
    glue = ROOT / "worker/public/sim/wsorb_bevy.js"
    if node is None or not glue.is_file():
        pytest.skip("Built Node/WASM assets are required for actual process-failover checks")
    if "headless_start" not in glue.read_text():
        pytest.skip("Rebuild the native headless engine to run actual process-failover checks")
    tmp_path.chmod(0o700)
    connection = tmp_path / "connection.json"
    with (tmp_path / "server.log").open("w+") as output:
        server = subprocess.Popen(
            [node, "worker/scripts/local.mjs"],
            cwd=ROOT,
            env={
                **os.environ,
                "WSORB_LAB": "1",
                "WSORB_HEADLESS": "1",
                "PORT": "0",
                "WSORB_LOCAL_CONNECTION": str(connection),
            },
            stdout=output,
            stderr=output,
        )
        try:
            deadline = time.monotonic() + 30
            while not connection.is_file():
                assert server.poll() is None, "The actual private headless server failed to start"
                assert time.monotonic() < deadline, "Private headless connection was not published"
                time.sleep(0.05)
            sim = LocalSimulation.from_file(connection)
            readiness = sim.status()
            assert readiness["backend"] == "native_headless"
            assert readiness["physics"]["started"] and readiness["physics_ready"]
            before = sim.snapshot()
            assert not before["renderer"]["started"] and not before["renderer"]["ready"]
            # Keep the original 500-body broad-stream cohort for process/lease
            # regressions. Transit arrival has separate real-flight coverage,
            # not conflated with ownership failover.
            assert before["observation"]["experiment_settings"]["debris"]["count"] == 500
            settings = deepcopy(before["observation"]["experiment_settings"])
            settings["debris"]["distribution"] = {"kind": "directed_stream"}
            configured = sim.configure_flight(settings, "far_side_l2", 100)
            assert configured["observation"]["geometry"]["debris_count"] == 500
            yield sim, connection
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def _primary_process(connection, channel):
    """The primary owns notebook-like closures, which must never be spawn arguments."""
    settings = ResilienceConfig().planner
    delay_start_plan = False
    callback_counts = {"prediction": 0, "search": 0}

    def predict(observation, config):
        nonlocal delay_start_plan
        callback_counts["prediction"] += 1
        if delay_start_plan and runner.status().get("controller_running"):
            delay_start_plan = False
            # A bounded primary algorithm outage, not a fabricated prediction.
            # The peer does not inherit this notebook closure or its wait.
            time.sleep(3)
        return predict_obstacles(observation, config)

    def search(frame, config):
        callback_counts["search"] += 1
        return plan_route(frame, config)

    def baseline_command(observation, route, mode, gains):
        return coast_command(observation)

    policy = LivePolicy(
        predict,
        search,
        baseline_command,
        settings,
        controller="resilient_waypoint_pd",
    )
    runner = ExperimentRunner()
    sim = LocalSimulation.from_file(connection)

    def status():
        return {**runner.status(), "policy_callback_counts": dict(callback_counts)}

    def failure(operation, error):
        result = {
            "primary_failure": type(error).__name__,
            "operation": operation,
            "runner": _diagnostics(runner.status()),
        }
        if isinstance(error, (FlightStateError, LocalApiError)):
            result["message"] = str(error)
        try:
            result["transport"] = sim.status()
        except LocalApiError as diagnostic_error:
            result["transport_error"] = type(diagnostic_error).__name__
        return result

    try:
        runner.watch(sim, policy, poll_hz=10, replan_s=0.25)
        while True:
            try:
                operation, arguments = channel.recv()
            except EOFError:
                break  # An orderly parent disconnect still stops its own cohort.
            try:
                if operation == "status":
                    result = status()
                elif operation in ("run", "run_paused_outage"):
                    delay_start_plan = operation == "run_paused_outage"
                    runner.run_controller(
                        duration_s=arguments.get("duration_s", 20),
                        cadence_hz=arguments.get("cadence_hz", 4),
                    )
                    result = status()
                elif operation == "outage":
                    result = runner.inject_primary_outage(**arguments)
                elif operation == "edit":
                    policy = replace(policy, gains={"velocity_gain_s": 0.035})
                    policy_revision = runner.update_policy(policy)
                    limits_revision = runner.update_run_limits(duration_s=30, cadence_hz=5)
                    result = {"policy": policy_revision, "limits": limits_revision}
                elif operation == "terminate_peer":
                    # Crash only our own peer using its actual Process handle.
                    runner._standby._process.terminate()
                    runner._standby.join(5)
                    result = runner.status()
                elif operation == "stop":
                    result = runner.stop()
                else:
                    raise ValueError("Unknown test-process instruction")
            except Exception as error:
                result = failure(operation, error)
            try:
                channel.send(result)
            except (EOFError, OSError):
                break  # The parent is gone; do not mask an earlier failure with a second send.
            if operation == "stop":
                break
    except Exception as error:
        try:
            channel.send(failure("watch", error))
        except (EOFError, OSError):
            raise error from None
    finally:
        try:
            runner.stop()
        finally:
            channel.close()


class _Primary:
    def __init__(self, connection):
        self.failure_path = Path(connection).with_name("controller-failure.json")
        context = multiprocessing.get_context("spawn")
        self.channel, child = context.Pipe()
        self.process = context.Process(target=_primary_process, args=(str(connection), child))
        self.stopped = False
        self.usable = True
        self.last = {}
        self.closed = False
        try:
            self.process.start()
        except BaseException:
            self.channel.close()
            raise
        finally:
            child.close()

    def call(self, operation="status", **arguments):
        try:
            self.channel.send((operation, arguments))
            assert self.channel.poll(15), self.last
            result = self.channel.recv()
        except (EOFError, OSError) as error:
            self.usable = False
            self.channel.close()
            raise AssertionError(
                {"primary_transport_error": type(error).__name__, "last_runner": self.last}
            ) from error
        self.last = result.get("runner", _diagnostics(result))
        if "primary_failure" in result:
            if result.get("operation") == "watch":
                self.usable = False
            raise AssertionError(json.dumps(result, indent=2))
        if operation == "stop":
            self.stopped = True
        return result

    def crash(self):
        self.process.terminate()
        self.process.join(5)
        assert not self.process.is_alive()

    def record_failure(self, native):
        """Retain actual public failure data even if devenv truncates pytest's output."""
        try:
            state = self.call()
        except (AssertionError, OSError) as error:
            state = {"diagnostic_read_error": type(error).__name__, "last_primary": self.last}
        report = {
            "measurement": "actual asynchronous transport failure; no replay or injected clocks",
            "native": _diagnostics(native),
            "primary": _diagnostics(state),
            "primary_resilience": state.get("primary_resilience"),
            "control_phase_history": state.get("control_phase_history"),
            "diagnostic_read_error": state.get("diagnostic_read_error"),
        }
        try:
            self.failure_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except OSError as error:
            report["artifact_write_error"] = type(error).__name__
        print(
            "controller transport failure "
            + json.dumps(
                {
                    "artifact": str(self.failure_path),
                    "error": report["primary"].get("error"),
                    "phase": report["primary"].get("control_phase"),
                    "diagnostic_read_error": report["diagnostic_read_error"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return report

    def close(self):
        if self.closed:
            return
        try:
            if self.process.is_alive() and self.usable and not self.stopped:
                self.call("stop")
        finally:
            self.process.join(10)
            if self.process.is_alive():
                self.crash()
            self.channel.close()
            self.process.close()
            self.closed = True


@pytest.fixture
def primary(headless):
    sim, connection = headless
    process = _Primary(connection)
    try:
        _until(
            process.call,
            lambda state: state.get("latest") is not None and state.get("accepted_policy_revision"),
        )
        yield process
    finally:
        # A primary killed by a test cannot run Python cleanup. Native manual
        # Pause revokes its original cohort; the surviving peer then exits.
        try:
            if not process.process.is_alive():
                sim.pause()
        finally:
            process.close()


def _run_warm(primary, **limits):
    primary.call("run", **limits)
    return _until(
        primary.call,
        lambda state: (
            state.get("standby") is not None
            and state["standby"]["alive"]
            and state["standby"].get("resilience") is not None
            and state.get("last_command_time_s") is not None
            and state.get("leader_controller_id") == state.get("primary_controller_id")
            and state["latest"]["runtime"]["running"]
            and _native_primary_thrust(state["latest"], state["primary_controller_id"])
        ),
        timeout_s=20,
    )


def test_watch_only_does_not_claim_or_start_a_peer(headless, primary):
    sim, _ = headless
    before = sim.snapshot()
    state = primary.call()
    assert state["standby"] is None
    assert not state["controller_running"]
    assert state["latest"]["runtime"]["running"] is False
    time.sleep(0.3)
    state = primary.call()
    assert state["worker_active"] and state["error"] is None, json.dumps(
        _diagnostics(state), indent=2
    )
    assert state["controller"] == "resilient_waypoint_pd"
    assert state["preview_controller_family"] == "planned_waypoint_pd"
    after = sim.snapshot()
    assert after["navigation"]["plan"]["execution"]["controller"] == "planned_waypoint_pd"
    assert control_authority(after) == control_authority(before)
    assert after["observation"]["time_s"] == before["observation"]["time_s"]


def test_ownership_only_renewal_keeps_the_native_owner_while_old_actuation_expires_at_five_x(
    headless,
):
    """Real native playback and receipts, with no injected clocks or controller outcomes."""
    sim, _ = headless
    initial = sim.snapshot()
    assert initial["observation"]["control_capabilities"]["ownership_renewal"] == {
        "schema_version": 1,
        "max_lease_sim_s": 0.75,
    }
    # Capture the original decision in the actual running context. A paused
    # source would test playback-context rejection instead of source expiry.
    sim.command({"op": "play", "rate": 5, "expected_context": control_context(initial)})
    running = _until(sim.snapshot, lambda value: value["runtime"]["running"])
    grant = sim.claim_control("ownership_only", "primary", running, lease_sim_s=0.75)
    fence = controller_fence(grant)
    sim.set_leased_command([0, 0, 0.1], [0, 0, 0], grant, lease_sim_s=0.75, controller_fence=fence)
    installed = sim.snapshot()
    old_watchdog = installed["observation"]["command_watchdog"]
    assert old_watchdog["status"] == "active"
    assert old_watchdog["source_time_s"] == grant["observation"]["time_s"]
    assert old_watchdog["expires_at_sim_time_s"] == grant["observation"]["time_s"] + 0.75
    source_deadline_bits = struct.pack(
        "!dd", old_watchdog["source_time_s"], old_watchdog["expires_at_sim_time_s"]
    )
    fresh = _until(
        sim.snapshot,
        lambda value: value["observation"]["time_s"] >= old_watchdog["source_time_s"] + 0.35,
        poll_s=0.005,
    )
    assert controller_fence(fresh) == fence
    active = {
        "origin": initial["observation"]["time_s"],
        "duration": 20,
        "consent_epoch": fence["epoch"],
        "run_id": fence["run_id"],
    }
    assert controller_ownership_renewal_lease(active, fresh, fence, 0.75) == 0.75
    near_limit = {**active, "duration": fresh["observation"]["time_s"] + 0.75 - active["origin"]}
    assert controller_ownership_renewal_lease(near_limit, fresh, fence, 0.75) is None
    older_schema = deepcopy(fresh)
    del older_schema["observation"]["control_capabilities"]
    assert controller_ownership_renewal_lease(active, older_schema, fence, 0.75) is None
    with pytest.raises(ValueError, match="does not advertise"):
        sim.renew_control(older_schema, controller_fence=fence)
    receipt = sim.renew_control(fresh, controller_fence=fence)
    assert receipt["source_time_s"] == fresh["observation"]["time_s"]
    assert receipt["control_authority"]["expires_at_sim_time_s"] == receipt["source_time_s"] + 0.75
    assert {name: receipt["control_authority"][name] for name in fence} == fence
    owner_deadline = receipt["control_authority"]["expires_at_sim_time_s"]

    def original_watchdog_unchanged(value):
        watchdog = value["observation"]["command_watchdog"]
        assert (
            struct.pack("!dd", watchdog["source_time_s"], watchdog["expires_at_sim_time_s"])
            == source_deadline_bits
        )
        assert watchdog["source_time_s"] == old_watchdog["source_time_s"]
        assert watchdog["expires_at_sim_time_s"] == old_watchdog["expires_at_sim_time_s"]
        assert watchdog["controller_fence"] == fence
        assert value["runtime"]["rate"] == 5
        return watchdog

    def old_actuation_expired(value):
        assert controller_fence(value) == fence
        assert control_authority(value)["expires_at_sim_time_s"] == owner_deadline
        watchdog = original_watchdog_unchanged(value)
        # Native watchdog expiry and the independently acquired physics report
        # can be observed on adjacent frames. Wait for both genuine reports.
        return watchdog["status"] == "expired" and value["observation"]["applied_physics"][
            "requested"
        ]["accel_body_mps2"] == [0, 0, 0]

    coast = _until(sim.snapshot, old_actuation_expired, poll_s=0.005)
    assert coast["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
    assert coast["observation"]["control_authority"]["status"] == "active"
    with pytest.raises(CommandExpiredError):
        sim.set_leased_command(
            [0, 0, 0.1], [0, 0, 0], grant, lease_sim_s=0.75, controller_fence=fence
        )

    def ownership_expired_at_its_receipt_deadline(value):
        authority = control_authority(value)
        assert {name: authority[name] for name in fence} == fence
        assert authority["expires_at_sim_time_s"] == owner_deadline
        assert original_watchdog_unchanged(value)["status"] == "expired"
        assert value["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
        # The unchanged private real-runtime guard can expire ownership before
        # the public simulation deadline if native frames are delayed.
        if authority["status"] == "expired":
            return True
        assert authority["status"] == "active"
        assert value["observation"]["time_s"] < owner_deadline
        return False

    expired_owner = _until(sim.snapshot, ownership_expired_at_its_receipt_deadline, poll_s=0.005)
    # Only actual expiry permits a new claim. Its fence is read from the native
    # grant, never assembled by changing the old term or receipt.
    replacement = sim.claim_control(
        fence["run_id"],
        "replacement",
        expired_owner,
        expected_epoch=fence["epoch"],
        lease_sim_s=0.75,
    )
    replacement_fence = controller_fence(replacement)
    assert replacement_fence["epoch"] == fence["epoch"]
    assert replacement_fence["run_id"] == fence["run_id"]
    assert replacement_fence["term"] == fence["term"] + 1
    assert replacement_fence["controller_id"] == "replacement"
    with pytest.raises(StaleContextError):
        sim.set_leased_command(
            [0, 0, 0], [0, 0, 0], replacement, lease_sim_s=0.75, controller_fence=fence
        )
    # A fresh zero decision uses ordinary actuation validation and the actual
    # new grant. The ownership-only receipt is never an actuation source.
    fresh_command = sim.snapshot()
    sim.set_leased_command(
        [0, 0, 0],
        [0, 0, 0],
        fresh_command,
        lease_sim_s=0.75,
        controller_fence=replacement_fence,
    )
    after = sim.snapshot()
    assert controller_fence(after) == replacement_fence
    assert after["observation"]["command_watchdog"]["controller_fence"] == replacement_fence
    assert after["observation"]["command_watchdog"]["status"] == "active"
    assert (
        after["observation"]["command_watchdog"]["source_time_s"]
        == fresh_command["observation"]["time_s"]
    )
    assert after["observation"]["command_watchdog"]["expires_at_sim_time_s"] == (
        fresh_command["observation"]["time_s"] + 0.75
    )


def test_ownership_heartbeats_remain_inside_original_run_duration_and_stop_with_the_cohort(
    headless, primary
):
    sim, _ = headless
    started = _run_warm(primary, duration_s=3, cadence_hz=10)
    ended = _until(
        primary.call,
        lambda state: not state["controller_running"] and not state["standby"]["alive"],
        timeout_s=15,
    )
    assert ended["ownership_renewal_count"] > 0
    receipt = ended["last_ownership_renewal"]
    assert receipt["control_authority"]["expires_at_sim_time_s"] < started["run_origin_time_s"] + 3
    assert control_authority(sim.snapshot())["status"] != "active"
    count = ended["ownership_renewal_count"]
    time.sleep(0.2)
    after = primary.call()
    assert after["ownership_renewal_count"] == count
    assert after["last_ownership_renewal"] == receipt


def test_broad_stream_controller_reaches_checkpoint_over_real_headless_transport(headless, primary):
    """Full real-controller flight at native 5x playback, not injected clock ticks."""
    sim, _ = headless
    before = sim.snapshot()
    origin = before["observation"]
    assert origin["geometry"]["debris_count"] == 500
    assert origin["seed"] == 100
    assert origin["mission"]["status"] == "running"
    assert origin["mission"]["target_distance_m"] > origin["geometry"]["arrival_radius_m"]
    _run_warm(primary, duration_s=1800, cadence_hz=10)
    running = sim.snapshot()
    # This is an actual supported playback action on this fixture's own flight.
    # The native checked path preserves its cohort, physics and source clocks.
    sim.command({"op": "play", "rate": 5, "expected_context": control_context(running)})
    final = _until(
        sim.snapshot,
        lambda value: (
            value["observation"]["mission"]["status"] != "running"
            or value["observation"]["time_s"] - origin["time_s"] >= 1800
        ),
        timeout_s=420,
    )
    actual = final["observation"]
    print(
        "checkpoint transport result "
        + json.dumps(
            {
                "measurement": "real asynchronous controller/server flight at native 5x playback",
                "seed": actual["seed"],
                "debris_count": actual["geometry"]["debris_count"],
                "initial_range_m": origin["mission"]["target_distance_m"],
                "time_s": actual["time_s"],
                "mission": actual["mission"],
                "runtime": final["runtime"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    assert actual["mission"]["status"] == "success", json.dumps(_diagnostics(final), indent=2)
    assert actual["mission"]["target_distance_m"] <= actual["geometry"]["arrival_radius_m"]
    assert actual["mission"]["damage"] <= 209200
    assert actual["time_s"] > origin["time_s"]
    assert actual["mission"]["fuel_used_kg"] > origin["mission"]["fuel_used_kg"]
    assert actual["seed"] == origin["seed"]
    assert actual["manual_interventions"] == origin["manual_interventions"]
    assert actual["geometry"]["debris_count"] == 500
    assert actual["experiment_settings"] == origin["experiment_settings"]
    assert actual["ship_tuning"] == origin["ship_tuning"]
    assert final["renderer"]["started"] is False


def test_transit_primary_reaches_its_first_native_decision_without_standby_masking(
    headless, primary
):
    """Real primary execution, separate from the full transit-arrival regression."""
    sim, _ = headless
    before = sim.snapshot()
    settings = deepcopy(before["observation"]["experiment_settings"])
    settings["debris"]["distribution"] = {"kind": "transit_stream"}
    configured = sim.configure_flight(settings, "far_side_l2", 100)
    initial = configured["observation"]
    assert initial["geometry"]["debris_count"] == 500
    assert initial["experiment_settings"]["debris"]["speed_mps"] == 20.0
    assert initial["experiment_settings"]["debris"]["speed_spread_mps"] == 4.0
    assert initial["max_accel_mps2"] == 0.5
    assert initial["max_angular_accel_rad_s2"] == 0.05
    # Consent must describe the newly configured flight, not the previous
    # paused watch snapshot. Wait for its actual observed revision before Run.
    _until(
        primary.call,
        lambda current: (
            current.get("latest", {}).get("observation", {}).get("trial_revision")
            == initial["trial_revision"]
        ),
    )
    watched = _until(
        sim.snapshot,
        lambda value: (
            value["navigation"]["plan"]["status"] == "no_candidate"
            and value["navigation"]["plan"].get("execution", {}).get("trial_revision")
            == initial["trial_revision"]
        ),
    )["navigation"]["plan"]
    state = _run_warm(primary, duration_s=20, cadence_hz=10)
    identity = state["primary_controller_id"]
    callbacks = state["policy_callback_counts"]
    preview_requests = state["preview_request_count"]
    actual = _until(
        sim.snapshot,
        lambda result: _native_primary_thrust(result, identity),
        timeout_s=15,
        diagnostic=primary.record_failure,
    )
    assert actual["observation"]["time_s"] > initial["time_s"]
    assert actual["observation"]["manual_interventions"] == initial["manual_interventions"]
    assert actual["observation"]["trial_revision"] == initial["trial_revision"]
    assert actual["observation"]["experiment_settings"] == settings
    assert actual["renderer"]["started"] is False
    consent = control_authority(actual)
    started = actual["observation"]["time_s"]
    sim.command({"op": "play", "rate": 5, "expected_context": control_context(actual)})
    commands = set()

    def primary_keeps_its_native_lease(value):
        observation = value["observation"]
        authority = control_authority(value)
        watchdog = observation["command_watchdog"]
        diagnostic = json.dumps(_diagnostics(value), sort_keys=True)
        assert authority["epoch"] == consent["epoch"], diagnostic
        assert authority["run_id"] == consent["run_id"], diagnostic
        assert authority["controller_id"] == identity, diagnostic
        assert authority["term"] == consent["term"], diagnostic
        assert authority["status"] == "active", diagnostic
        assert watchdog["status"] == "active", diagnostic
        assert watchdog["controller_fence"] == controller_fence(value), diagnostic
        assert watchdog["expires_at_sim_time_s"] - watchdog["source_time_s"] >= 0.05 - 1e-9
        assert watchdog["expires_at_sim_time_s"] - watchdog["source_time_s"] <= 0.75 + 1e-9
        assert watchdog["source_time_s"] <= observation["time_s"]
        assert observation["manual_interventions"] == initial["manual_interventions"]
        assert observation["trial_revision"] == initial["trial_revision"]
        assert observation["experiment_settings"] == settings
        assert value["runtime"]["rate"] == 5
        assert value["renderer"]["started"] is False
        if _native_primary_thrust(value, identity):
            commands.add(watchdog["source_time_s"])
        return observation["time_s"] >= started + 5.0

    after = _until(
        sim.snapshot,
        primary_keeps_its_native_lease,
        timeout_s=15,
        diagnostic=primary.record_failure,
    )
    assert initial["mission"]["phase"] == "on_pad"
    assert after["observation"]["mission"]["phase"] == "ascending"
    assert len(commands) >= 2
    state = primary.call()
    peer = state["standby"]
    assert state["error"] is None, json.dumps(_diagnostics(state), sort_keys=True)
    assert state["nonzero_command_count"] >= 2
    assert state["run_limits"]["requested_cadence_hz"] == 10
    assert state["last_decision_interval_wall_s"] >= 0.1 - 1e-9
    assert state["acquisition_schedule"] == "fresh_public_reads_between_strict_calculations"
    assert state["last_command_validation_kind"] == (
        "original_public_source_then_atomic_native_validation"
    )
    assert state["planning_schedule"] == "timed_command_without_global_preview"
    assert state["policy_callback_counts"] == callbacks
    assert state["preview_request_count"] == preview_requests
    assert state["advisory_schedule"] == "no_advisory_http_during_timed_control"
    assert state["ownership_renewal_count"] >= 2
    assert state["last_ownership_renewal"]["control_authority"]["controller_id"] == identity
    assert state["last_ownership_renewal"]["control_authority"]["term"] == consent["term"]
    assert set(state["last_ownership_renewal"]) == {
        "source_time_s",
        "lease_sim_s",
        "sensor_revision",
        "control_authority",
    }
    assert peer["state"] == "following", json.dumps(_diagnostics(state), sort_keys=True)
    assert peer["ownership_renewal_count"] == 0
    assert peer["resilience"]["ownship_history_samples"] >= 2
    assert peer["plan_count"] == 0
    assert peer["last_decision_source_time_s"] is None
    assert peer["command_count"] == 0
    assert peer["resilience"]["decision_source_time_s"] is None
    published = sim.snapshot()["navigation"]["plan"]
    assert published == watched
    assert published["status"] == "no_candidate"
    assert published["points_world_m"] == []
    assert published["expanded_nodes"] == 0
    assert published["source_seed"] == initial["seed"]
    assert "no complete route is claimed" in published["detail"]


@pytest.mark.parametrize(
    "trajectory_adjustment",
    ["off", "toward_lander"],
    ids=["attraction_off", "attraction_on"],
)
def test_transit_primary_reaches_checkpoint_over_real_headless_transport(
    headless, primary, trajectory_adjustment
):
    """Full async transit flight, with native clocks, leases and arrival assessment."""
    sim, _ = headless
    before = sim.snapshot()
    settings = deepcopy(before["observation"]["experiment_settings"])
    settings["debris"]["distribution"] = {"kind": "transit_stream"}
    settings["debris"]["trajectory_adjustment"] = trajectory_adjustment
    configured = sim.configure_flight(settings, "far_side_l2", 100)
    origin = configured["observation"]
    assert configured["runtime"]["running"] is False
    assert origin["trial_revision"] != before["observation"]["trial_revision"]
    assert origin["time_s"] == 0
    assert origin["seed"] == 100
    assert origin["flight_preset"] == "far_side_l2"
    assert origin["geometry"]["debris_count"] == 500
    assert origin["debris_distribution"] == {"kind": "transit_stream"}
    # Explicit off normalizes to omission in the versioned native settings.
    assert origin["experiment_settings"] == validate_experiment_settings(settings)
    assert (
        origin["experiment_settings"]["debris"].get("trajectory_adjustment", "off")
        == trajectory_adjustment
    )
    assert origin["experiment_settings"]["debris"]["speed_mps"] == 20.0
    assert origin["experiment_settings"]["debris"]["speed_spread_mps"] == 4.0
    assert origin["max_accel_mps2"] == 0.5
    assert origin["max_angular_accel_rad_s2"] == 0.05
    assert origin["geometry"]["arrival_radius_m"] == 50.0
    assert origin["mission"]["status"] == "running"
    assert origin["mission"]["target_distance_m"] > 50.0
    # Capture Run consent only after the primary observes this actual new trial.
    _until(
        primary.call,
        lambda state: (
            state.get("latest", {}).get("observation", {}).get("trial_revision")
            == origin["trial_revision"]
        ),
    )
    max_sim_s, max_wall_s = 1800, 420
    started = time.monotonic()
    state = _run_warm(primary, duration_s=max_sim_s, cadence_hz=10)
    consent = state["control_authority"]
    identity = state["primary_controller_id"]
    running = sim.snapshot()
    # Use the same supported 5x playback as the broad-stream arrival regression.
    # Do not inject steps, extend leases or write any test-owned thrust command.
    sim.command({"op": "play", "rate": 5, "expected_context": control_context(running)})
    nonzero_sources = set()

    def flight_finished(value):
        observation = value["observation"]
        for field in (
            "seed",
            "trial_revision",
            "tuning_revision",
            "manual_interventions",
            "flight_preset",
            "experiment_settings",
            "ship_tuning",
            "debris_flow",
            "debris_distribution",
            "max_accel_mps2",
            "max_angular_accel_rad_s2",
        ):
            assert observation[field] == origin[field], field
        assert observation["geometry"]["debris_count"] == 500
        assert observation["geometry"]["arrival_radius_m"] == 50.0
        assert value["renderer"]["started"] is False
        authority = control_authority(value)
        ownership = json.dumps(_diagnostics(value), sort_keys=True)
        if (
            authority["epoch"] != consent["epoch"]
            or authority["run_id"] != consent["run_id"]
            or authority["controller_id"] != identity
        ):
            # Read the actual primary failure only on a failed ownership check.
            # Successful ticks incur no extra controller IPC or HTTP request.
            failed = primary.call()
            ownership = json.dumps(
                {
                    "native": _diagnostics(value),
                    "primary": _diagnostics(failed),
                    "primary_resilience": failed.get("primary_resilience"),
                },
                sort_keys=True,
            )
        assert authority["epoch"] == consent["epoch"], ownership
        assert authority["run_id"] == consent["run_id"], ownership
        # A peer takeover must not hide a primary transit-guidance failure.
        assert authority["controller_id"] == identity, ownership
        # Native terminal assessment clears the installed command and watchdog.
        # Preserve the ownership/context assertions above, then let the strict
        # native success/damage assertions below assess the actual outcome.
        if observation["mission"]["status"] != "running":
            return True
        watchdog = observation["command_watchdog"]
        assert watchdog["expires_at_sim_time_s"] - watchdog["source_time_s"] <= 0.75 + 1e-9
        assert watchdog["source_time_s"] <= observation["time_s"]
        if _native_primary_thrust(value, identity):
            nonzero_sources.add(watchdog["source_time_s"])
        return (
            observation["mission"]["status"] != "running"
            or observation["time_s"] - origin["time_s"] >= max_sim_s
        )

    final = _until(
        sim.snapshot,
        flight_finished,
        timeout_s=max_wall_s,
        diagnostic=primary.record_failure,
    )
    actual = final["observation"]
    primary_state = primary.call()
    range_m = math.dist(actual["shuttle"]["position"], actual["target"]["position"])
    result = {
        "measurement": "real asynchronous primary/server flight at native 5x playback",
        "trajectory_adjustment": trajectory_adjustment,
        "seed": actual["seed"],
        "debris_count": actual["geometry"]["debris_count"],
        "initial_range_m": origin["mission"]["target_distance_m"],
        "range_m": range_m,
        "time_s": actual["time_s"],
        "wall_s": time.monotonic() - started,
        "mission": actual["mission"],
        "runtime": final["runtime"],
        "authority": control_authority(final),
        "primary": _diagnostics(primary_state),
        "primary_resilience": primary_state.get("primary_resilience"),
        "observed_nonzero_command_sources": len(nonzero_sources),
    }
    print("transit checkpoint transport result " + json.dumps(result, sort_keys=True), flush=True)
    assert actual["mission"]["status"] == "success", json.dumps(result, indent=2)
    assert range_m <= 50.0, result
    assert actual["mission"]["target_distance_m"] <= 50.0, result
    assert actual["mission"]["damage"] <= 209200, result
    assert actual["time_s"] > origin["time_s"]
    assert actual["mission"]["fuel_used_kg"] > origin["mission"]["fuel_used_kg"]
    assert final["runtime"]["rate"] == 5
    assert result["primary"]["error"] is None, result
    assert len(nonzero_sources) >= 2, result
    assert primary_state["nonzero_command_count"] >= 2, result
    assert primary_state["standby"]["last_decision_source_time_s"] is None, result
    assert primary_state["standby"]["command_count"] == 0, result


def test_new_explicit_run_retains_the_actual_grant_after_a_different_expired_cohort(
    headless, primary
):
    sim, _ = headless
    prior = sim.claim_control(
        "expired_prior_run", "prior_controller", sim.snapshot(), lease_sim_s=0.1
    )
    sim.set_leased_command(
        [0, 0, 0], [0, 0, 0], prior, lease_sim_s=0.1, controller_fence=controller_fence(prior)
    )
    expired = _until(sim.snapshot, lambda value: control_authority(value)["status"] == "expired")
    assert expired["runtime"]["running"] is False
    state = _run_warm(primary)
    actual = state["control_authority"]
    assert actual["run_id"] != control_authority(prior)["run_id"]
    assert actual["epoch"] >= control_authority(prior)["epoch"]
    assert state["standby"]["consent_epoch"] == actual["epoch"]
    assert state["standby"]["run_id"] == actual["run_id"]


def test_actual_primary_process_death_allows_only_expired_same_consent_takeover(headless, primary):
    sim, _ = headless
    state = _run_warm(primary)
    peer = state["standby"]
    assert peer["pid"] != primary.process.pid != os.getpid()
    assert peer["algorithm_source"] == ALGORITHM_SOURCE
    assert "not replicated" in peer["algorithm_source"]
    before = sim.snapshot()
    old_fence = controller_fence(before)
    primary.crash()
    after = _until(
        sim.snapshot,
        lambda result: (
            control_authority(result)["controller_id"] == peer["controller_id"]
            and control_authority(result)["status"] == "active"
            and result["observation"]["command_watchdog"]
            .get("controller_fence", {})
            .get("controller_id")
            == peer["controller_id"]
        ),
    )
    authority = control_authority(after)
    # Native authority can expire on its private real-runtime clock before the
    # public simulation deadline, including during paused or delayed frames.
    assert after["observation"]["time_s"] >= before["observation"]["time_s"]
    assert authority["epoch"] == old_fence["epoch"] == peer["consent_epoch"]
    assert authority["run_id"] == old_fence["run_id"] == peer["run_id"]
    assert authority["term"] > old_fence["term"]
    assert after["runtime"]["running"]
    # Even fresh data cannot make a late write from the dead primary's old term valid.
    with pytest.raises(StaleContextError):
        sim.set_leased_command([0, 0, 0.1], [0, 0, 0], sim.snapshot(), controller_fence=old_fence)
    assert control_authority(sim.snapshot())["controller_id"] == peer["controller_id"]


def test_primary_death_before_checked_play_can_fail_over_on_native_runtime_expiry(
    headless, primary
):
    sim, _ = headless
    primary.call("run_paused_outage")
    starting = _until(
        primary.call,
        lambda state: (
            state.get("standby") is not None
            and state["standby"]["alive"]
            and not state["latest"]["runtime"]["running"]
        ),
    )
    peer = starting["standby"]
    before = sim.snapshot()
    assert before["runtime"]["running"] is False
    old = control_authority(before)
    primary.crash()
    resumed = _until(
        sim.snapshot,
        lambda result: (
            result["runtime"]["running"]
            and result["observation"]["time_s"] > before["observation"]["time_s"]
            and control_authority(result)["controller_id"] == peer["controller_id"]
            and result["observation"]["command_watchdog"]
            .get("controller_fence", {})
            .get("controller_id")
            == peer["controller_id"]
        ),
    )
    authority = control_authority(resumed)
    assert authority["term"] > old["term"]
    assert authority["epoch"] == old["epoch"] == peer["consent_epoch"]
    assert authority["run_id"] == old["run_id"]
    assert resumed["observation"]["time_s"] > before["observation"]["time_s"]


def test_outage_recovery_hot_settings_and_native_pause_never_reacquire_consent(headless, primary):
    sim, _ = headless
    initial = _run_warm(primary)
    peer = initial["standby"]
    original = control_authority(sim.snapshot())
    outage = primary.call("outage", duration_sim_s=2)
    transferred = _until(
        primary.call,
        lambda state: (
            state.get("leader_controller_id") == peer["controller_id"]
            and state["standby"].get("last_command_time_s") is not None
        ),
    )
    assert transferred["control_authority"]["term"] > original["term"]
    if transferred["last_ownership_renewal"] is not None:
        assert (
            transferred["last_ownership_renewal"]["source_time_s"]
            <= outage["started_at_sim_time_s"]
        )
    recovered = _until(
        primary.call,
        lambda state: state["latest"]["observation"]["time_s"] >= outage["until_sim_time_s"] + 0.5,
    )
    assert recovered["leader_controller_id"] == peer["controller_id"]
    assert recovered["resilience"] == recovered["standby"]["resilience"]
    assert recovered["leader_algorithm_source"] == ALGORITHM_SOURCE
    assert recovered["ownership_renewal_count"] == transferred["ownership_renewal_count"]
    assert recovered["standby"]["ownership_renewal_count"] > 0
    revisions = primary.call("edit")
    updated = _until(
        primary.call,
        lambda state: (
            state["standby"].get("policy_revision") == revisions["policy"]
            and state["standby"].get("run_limits_revision") == revisions["limits"]
        ),
    )
    assert updated["standby"]["pid"] == peer["pid"]
    assert updated["standby"]["run_origin_time_s"] == initial["run_origin_time_s"]
    assert updated["run_origin_time_s"] == initial["run_origin_time_s"]
    assert updated["control_authority"]["epoch"] == original["epoch"]
    # Same native Pause action as P, without automating or claiming a browser key test.
    sim.pause()
    paused = sim.snapshot()
    paused_authority = control_authority(paused)
    assert paused_authority["epoch"] != original["epoch"]
    _until(primary.call, lambda state: not state["standby"]["alive"])
    time.sleep(0.4)
    assert sim.snapshot()["runtime"]["running"] is False
    with pytest.raises(StaleContextError):
        sim.claim_control(
            peer["run_id"], peer["controller_id"], sim.snapshot(), expected_epoch=original["epoch"]
        )
    assert control_authority(sim.snapshot()) == paused_authority


def test_manual_command_and_guarded_stop_do_not_erase_the_new_owner(headless, primary):
    sim, _ = headless
    state = _run_warm(primary)
    old = control_authority(sim.snapshot())
    sim.SetCommand([0.02, 0, 0], [0, 0, 0])
    manual = sim.snapshot()
    assert control_authority(manual)["epoch"] != old["epoch"]
    primary.call("stop")
    with pytest.raises(StaleContextError):
        sim.release_control(old["run_id"], old["epoch"], sim.snapshot())
    after = sim.snapshot()
    assert after["runtime"]["running"] == manual["runtime"]["running"]
    assert after["observation"]["shuttle"]["commanded_accel_mps2"] == [0.02, 0, 0]
    assert control_authority(after)["epoch"] != state["standby"]["consent_epoch"]


def test_total_controller_process_loss_is_bounded_by_the_real_native_watchdog(headless, primary):
    sim, _ = headless
    _run_warm(primary)
    primary.call("terminate_peer")
    before = sim.snapshot()
    primary.crash()
    expired = _until(
        sim.snapshot,
        lambda result: (
            control_authority(result)["status"] == "expired"
            and result["observation"]["command_watchdog"]["status"] == "expired"
        ),
    )
    observation = expired["observation"]
    assert observation["time_s"] > before["observation"]["time_s"]
    assert expired["runtime"]["running"]
    assert observation["shuttle"]["commanded_accel_mps2"] == [0, 0, 0]
    assert observation["shuttle"]["commanded_angular_accel_rad_s2"] == [0, 0, 0]
    watchdog = observation["command_watchdog"]
    assert watchdog["expires_at_sim_time_s"] - watchdog["source_time_s"] <= 1
    # The genuine expired authority/watchdog reports cover either native clock.
    # Runtime expiry must not be relabelled as reaching the simulation deadline.
