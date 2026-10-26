"""Actual controller process loss against the private headless native/WASM server.

No browser, renderer, SDK, network, sensor or physics result is replaced. Each
test starts its own server, flight and controllers. Pause uses the real native
Pause operation, not an automated keyboard/browser claim. This proves only
same-host process isolation, not cross-region or licensed-runtime availability.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from wsorb.experiment import ExperimentRunner, FlightStateError, LivePolicy
from wsorb.flight import ResilienceConfig, coast_command, plan_route, predict_obstacles
from wsorb.local_api import LocalApiError, LocalSimulation, StaleContextError, control_context
from wsorb.standby import ALGORITHM_SOURCE, control_authority, controller_fence, known_busy

ROOT = Path(__file__).resolve().parents[1]


def _diagnostics(state):
    """Actual public lifecycle fields, without dumping 500 radar rows or credentials."""
    latest = state.get("latest") or state
    observation = latest.get("observation", {})
    peer = state.get("standby") or {}
    return {
        **{
            key: state.get(key)
            for key in ("state", "worker_active", "controller_running", "error", "notice")
        },
        "time_s": observation.get("time_s"),
        "mission": observation.get("mission"),
        "authority": observation.get("control_authority"),
        "runtime": latest.get("runtime"),
        "renderer": latest.get("renderer"),
        "standby": {key: peer.get(key) for key in ("state", "alive", "error", "notice")},
    }


def _until(read, predicate, *, timeout_s=10):
    deadline = time.monotonic() + timeout_s
    value = {}
    while True:
        try:
            value = read()
            if predicate(value):
                return value
        except LocalApiError as error:
            if not known_busy(error):
                raise
        assert time.monotonic() < deadline, json.dumps(_diagnostics(value), indent=2)
        time.sleep(0.05)


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
            # Only this fixture's fresh engine is configured. Preserve its real
            # default body count and sensors rather than making a toy world.
            assert before["observation"]["experiment_settings"]["debris"]["count"] == 500
            configured = sim.configure_flight(
                before["observation"]["experiment_settings"], "far_side_l2", 100
            )
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

    def predict(observation, config):
        nonlocal delay_start_plan
        if delay_start_plan and runner.status().get("controller_running"):
            delay_start_plan = False
            # A bounded primary algorithm outage, not a fabricated prediction.
            # The peer does not inherit this notebook closure or its wait.
            time.sleep(3)
        return predict_obstacles(observation, config)

    def search(frame, config):
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
                    result = runner.status()
                elif operation in ("run", "run_paused_outage"):
                    delay_start_plan = operation == "run_paused_outage"
                    runner.run_controller(
                        duration_s=arguments.get("duration_s", 20),
                        cadence_hz=arguments.get("cadence_hz", 4),
                    )
                    result = runner.status()
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


def test_stock_controller_reaches_checkpoint_over_real_headless_transport(headless, primary):
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
    recovered = _until(
        primary.call,
        lambda state: state["latest"]["observation"]["time_s"] >= outage["until_sim_time_s"] + 0.5,
    )
    assert recovered["leader_controller_id"] == peer["controller_id"]
    assert recovered["resilience"] == recovered["standby"]["resilience"]
    assert recovered["leader_algorithm_source"] == ALGORITHM_SOURCE
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
