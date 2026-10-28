"""Pure consent/parser checks and an opt-in real owned-process/API trial. No mocks."""

from __future__ import annotations

import ast
import inspect
import json
import math
import os
import secrets
from dataclasses import asdict

import numpy as np
import pytest

from wsorb.checkpoint import (
    CheckpointError,
    CheckpointOptions,
    _checkpoint_deadline,
    _guard,
    _owner_read_retryable,
    _owner_status_gate,
    _owner_wait_deadline,
    _readiness_flag,
    _record_command,
    _summary,
    _usage,
    _wait_remaining,
    decision_lease,
    main,
    native_measurement,
    native_success,
    parse_args,
    policy_config,
    policy_decision_eligible,
    run_checkpoint,
    run_trial,
    sample_from_snapshot,
    world_metadata,
)
from wsorb.local_api import (
    CommandExpiredError,
    LocalApiError,
    LocalBusyError,
    LocalPageMismatchError,
    LocalSimulation,
    StaleContextError,
)
from wsorb.room_api import RoomApiError, RoomSimulation


def test_parser_defaults_are_read_only_and_do_not_use_an_implicit_owner_connection():
    options = parse_args(["--isolated"])
    assert options.run is False
    assert options.policy == "baseline"
    assert options.connection_file is None
    assert options.room_file is None
    assert options.playback_rate == 1
    assert options.duration_s <= 1800
    assert options.wall_budget_s <= 3600
    assert options.cadence_hz >= 10
    assert options.wait_for_owner_s == 0
    assert options.await_owner is False
    assert options.diagnostics is False
    with pytest.raises(CheckpointError, match="arguments"):
        parse_args([])


def test_owner_wait_is_explicit_bounded_external_and_does_not_grant_run_consent():
    options = parse_args(["--room-file", "private.json", "--wait-for-owner-s", "3600"])
    assert options.wait_for_owner_s == 3600 and options.run is False
    for arguments in (
        ["--isolated", "--wait-for-owner-s", "1"],
        ["--room-file", "private.json", "--wait-for-owner-s", "-1"],
        ["--room-file", "private.json", "--wait-for-owner-s", "3601"],
        ["--room-file", "private.json", "--wait-for-owner-s", "nan"],
    ):
        with pytest.raises(CheckpointError):
            parse_args(arguments)


@pytest.mark.parametrize("scope", ["--connection-file", "--room-file"])
def test_persistent_owner_standby_is_explicit_external_and_does_not_grant_run(scope):
    options = parse_args([scope, "private.json", "--await-owner"])
    assert options.await_owner is True
    assert options.run is False
    assert options.wait_for_owner_s == 0
    assert options.duration_s == 1800 and options.wall_budget_s == 3600
    assert parse_args([scope, "private.json", "--await-owner", "--run"]).run is True
    assert parse_args([scope, "private.json", "--await-owner", "--wait-for-owner-s", "0"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["--await-owner"],
        ["--isolated", "--await-owner"],
        ["--room-file", "private.json", "--await-owner", "--wait-for-owner-s", "1"],
        ["--connection-file", "private.json", "--await-owner", "--policy", "compare", "--run"],
    ],
)
def test_persistent_standby_cannot_be_implicit_isolated_compared_or_finitely_rearmed(arguments):
    with pytest.raises(CheckpointError):
        parse_args(arguments)


@pytest.mark.parametrize("value", [1, "yes", np.bool_(True), None])
def test_persistent_standby_options_require_an_actual_boolean(value):
    with pytest.raises(CheckpointError, match="booleans"):
        CheckpointOptions(connection_file="private.json", await_owner=value)


def test_only_persistent_readiness_defers_the_flight_budget_and_finite_defaults_stay_bounded():
    # Pure budget calculations. No clock, engine, graphics or transport is replaced.
    default = CheckpointOptions(connection_file="private.json", wall_budget_s=10)
    assert _checkpoint_deadline(default, now=100) == 110
    assert _owner_wait_deadline(default, now=100, deadline=110) == 100
    assert _wait_remaining(100, now=100) == 0

    finite = CheckpointOptions(room_file="private.json", wait_for_owner_s=30, wall_budget_s=10)
    assert _checkpoint_deadline(finite, now=100) == 110
    end = _owner_wait_deadline(finite, now=100, deadline=110)
    assert end == 110
    assert _wait_remaining(end, now=109.5) == 0.5
    assert _wait_remaining(end, now=110) == _wait_remaining(end, now=1000) == 0
    assert _owner_wait_deadline(finite, now=100, deadline=None) == 130

    persistent = CheckpointOptions(
        room_file="private.json", await_owner=True, duration_s=1, wall_budget_s=10
    )
    assert _checkpoint_deadline(persistent, now=100) is None
    end = _owner_wait_deadline(persistent, now=100, deadline=110)
    assert end is None and math.isinf(_wait_remaining(end, now=1_000_000))
    flight_end = _checkpoint_deadline(persistent, now=1_000_000, owner_ready=True)
    assert flight_end == 1_000_010
    assert _wait_remaining(flight_end, now=1_000_010) == 0
    assert persistent.duration_s == 1 and persistent.wall_budget_s == 10


def test_standby_only_retries_explicit_read_unavailability_not_auth_protocol_or_page_failures():
    for code in ("owner_unavailable", "transport_unavailable"):
        error = RoomApiError("Read unavailable")
        error.code = code
        assert _owner_read_retryable(error)
    for error in (
        LocalApiError("Invalid JSON"),
        RoomApiError("HTTP authorization failed"),
        LocalPageMismatchError("Page changed"),
        LocalBusyError("Handled separately as pre-dispatch read contention"),
        StaleContextError("Native context changed"),
        CommandExpiredError("Native lease expired"),
    ):
        assert not _owner_read_retryable(error)


def test_status_read_gate_blocks_explicit_hidden_pages_without_fabricating_browser_readiness():
    # Pure protocol policy. These flags do not stand in for actual readiness evidence.
    status = {"backend": "browser", "connected": True, "busy": False}
    assert _owner_status_gate(status) == (False, True)  # Legacy permission to read only.
    assert _owner_status_gate({**status, "page_visible": False}) == (False, False)
    assert _owner_status_gate({**status, "page_visible": True}) == (False, True)
    assert _owner_status_gate({**status, "busy": True}) == (False, False)
    assert _owner_status_gate({**status, "connected": False}) == (False, False)
    native = {"backend": "native_headless", "busy": False, "connected": True}
    assert _owner_status_gate(native) == (False, False)  # No browser fallback for native.
    assert _owner_status_gate({**native, "physics_ready": True}) == (False, False)
    assert _owner_status_gate({**native, "physics_ready": True, "physics_status_cached": True}) == (
        False,
        False,
    )


@pytest.mark.parametrize("name", ["page_visible", "control_ready"])
def test_optional_readiness_is_boolean_or_absent_never_coerced_or_filled(name):
    assert _readiness_flag({}, name) is None
    assert _readiness_flag({name: False}, name) is False
    assert _readiness_flag({name: True}, name) is True
    for value in (None, 0, 1, "false", np.bool_(False)):
        with pytest.raises(CheckpointError, match="booleans"):
            _readiness_flag({name: value}, name)


def test_readiness_wait_has_no_actuation_or_whole_flight_retry_and_keeps_traces_separate():
    from wsorb.checkpoint import _bind_observed_page, _run_checkpoint, wait_for_owner

    tree = ast.parse(inspect.getsource(wait_for_owner))
    client_methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "client"
    }
    assert client_methods == {"status", "snapshot", "control_transaction"}
    helper = inspect.getsource(_bind_observed_page)
    assert "RoomSimulation" in helper and "require=False" in helper
    source = inspect.getsource(_run_checkpoint)
    # The only invocation loop is over policies. External comparison is forbidden,
    # so readiness is followed by one flight or inspection, not a retry loop.
    invocation = ast.parse(source)
    loops = [node for node in ast.walk(invocation) if isinstance(node, (ast.For, ast.While))]
    assert len(loops) == 1 and isinstance(loops[0], ast.For)
    assert isinstance(loops[0].iter, ast.Name) and loops[0].iter.id == "policies"
    assert "diagnostics_context(enabled=False)" in source
    assert source.index("wait_for_owner(client") < source.index("owner_ready=True")


def test_explicit_hosted_control_unready_resets_frame_progress_before_any_ready_return():
    from wsorb.checkpoint import wait_for_owner

    tree = ast.parse(inspect.getsource(wait_for_owner))
    gate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and ast.unparse(node.test) == "_readiness_flag(snapshot, 'control_ready') is False"
    )
    assert len(gate.body) == 1 and ast.unparse(gate.body[0]) == "previous_frame = None"
    assert not any(isinstance(node, ast.Return) for node in ast.walk(gate.body[0]))
    # Ready returns live only in the else branch, after this explicit gate.
    assert any(isinstance(node, ast.Return) for node in ast.walk(gate.orelse[0]))


def test_run_binds_the_initial_snapshot_before_any_claim_without_widening_native_retries():
    from wsorb.checkpoint import _KNOWN_UNAPPLIED

    source = inspect.getsource(run_trial)
    assert source.index("initial = client.snapshot()") < source.index("_bind_observed_page(client)")
    assert source.index("_bind_observed_page(client)") < source.index("client.claim_control(")
    assert source.index('_readiness_flag(initial, "control_ready")') < source.index(
        "client.claim_control("
    )
    assert (LocalBusyError, StaleContextError, CommandExpiredError) == _KNOWN_UNAPPLIED
    assert not issubclass(LocalPageMismatchError, _KNOWN_UNAPPLIED)


def test_diagnostics_flag_is_explicit_and_does_not_grant_run_consent():
    options = parse_args(["--room-file", "private.json", "--diagnostics"])
    assert options.diagnostics is True and options.run is False
    with pytest.raises(CheckpointError):
        CheckpointOptions(isolated=True, diagnostics=1)


def test_external_scope_is_explicit_single_trial_and_does_not_change_playback_rate():
    options = parse_args(["--connection-file", "private.json"])
    assert str(options.connection_file) == "private.json"
    assert options.playback_rate == 1
    assert not options.run
    for argv in (
        ["--connection-file", "private.json", "--run", "--policy", "compare"],
        ["--connection-file", "private.json", "--run", "--playback-rate", "5"],
        ["--connection-file", "private.json", "--isolated"],
    ):
        with pytest.raises(CheckpointError):
            parse_args(argv)


def test_hosted_room_scope_is_explicit_read_only_and_cannot_change_rate_or_compare():
    options = parse_args(["--room-file", "private-room.json"])
    assert str(options.room_file) == "private-room.json"
    assert options.connection_file is None
    assert not options.isolated and not options.run
    assert options.playback_rate == 1
    assert parse_args(["--room-file", "private-room.json", "--run"]).run is True
    for extra in (
        ["--connection-file", "private-local.json"],
        ["--isolated"],
        ["--run", "--policy", "compare"],
        ["--run", "--playback-rate", "5"],
        ["--allow-loopback"],
    ):
        with pytest.raises(CheckpointError):
            parse_args(["--room-file", "private-room.json", *extra])
    for conflicting in (
        {"isolated": True, "room_file": "room.json"},
        {"connection_file": "local.json", "room_file": "room.json"},
    ):
        with pytest.raises(CheckpointError, match="exactly one"):
            CheckpointOptions(**conflicting)


def test_hosted_reusable_actuation_also_requires_explicit_consent_without_http():
    import uuid

    client = RoomSimulation("https://127.0.0.1:1", str(uuid.uuid4()), secrets.token_urlsafe(32))
    with pytest.raises(CheckpointError, match="--run"):
        run_trial(client, CheckpointOptions(room_file="room.json"), policy="baseline")


def test_hosted_checkpoint_never_loads_an_http_test_descriptor_or_echoes_its_credential(
    tmp_path, capsys
):
    import uuid

    path = tmp_path / "room.json"
    token = secrets.token_urlsafe(32)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "url": "http://127.0.0.1:1",
                "room_id": str(uuid.uuid4()),
                "token": token,
            }
        )
    )
    path.chmod(0o600)
    assert main(["--room-file", str(path)]) == 1
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["mode"] == "read_only"
    assert result["transport"] == "hosted_room_public_API"
    assert "hosted_room_public_API" in result["execution_kind"]
    assert result["official_server_execution"] == "not_performed_here"
    assert result["trials"][0]["error"]["code"] == "setup_failed"


def test_comparison_requires_separate_owned_worlds_and_explicit_actuation_consent():
    with pytest.raises(CheckpointError, match="--isolated and --run"):
        parse_args(["--isolated", "--policy", "compare"])
    options = parse_args(["--isolated", "--policy", "compare", "--run"])
    assert options.policy == "compare" and options.isolated and options.run


@pytest.mark.parametrize("name", ["duration-s", "wall-budget-s", "cadence-hz"])
@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
def test_limits_must_be_real_bounded_values_before_any_connection(name, value):
    with pytest.raises(CheckpointError):
        parse_args(["--isolated", f"--{name}", value])


@pytest.mark.parametrize(
    "name,value", [("duration-s", "1800.01"), ("wall-budget-s", "3600.01"), ("cadence-hz", "9.9")]
)
def test_native_lease_cadence_and_time_budgets_cannot_be_weakened(name, value):
    with pytest.raises(CheckpointError):
        parse_args(["--isolated", f"--{name}", value])


def test_reusable_actuation_function_rejects_read_only_options_without_network_io():
    # A real client object, not a fake. The consent guard precedes all calls/imports.
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    options = CheckpointOptions(connection_file="unused.json")
    with pytest.raises(CheckpointError, match="--run"):
        run_trial(client, options, policy="baseline")
    with pytest.raises(CheckpointError, match="Validated"):
        run_checkpoint({"run": True})


@pytest.mark.parametrize("value", [True, 2**2048, math.nan, math.inf])
def test_reusable_options_validate_numbers_without_overflow_or_boolean_consent(value):
    with pytest.raises(CheckpointError):
        CheckpointOptions(isolated=True, duration_s=value)


@pytest.mark.parametrize("value", [1, "yes", np.bool_(True), None])
def test_reusable_options_require_actual_boolean_consent(value):
    with pytest.raises(CheckpointError):
        CheckpointOptions(isolated=True, run=value)


def test_explicit_isolated_options_cannot_relabel_an_external_client_as_owned():
    client = LocalSimulation("http://127.0.0.1:1", secrets.token_urlsafe(32))
    with pytest.raises(CheckpointError, match="isolated"):
        run_trial(client, CheckpointOptions(isolated=True, run=True), policy="baseline", owned=True)


def test_original_granted_epoch_term_and_world_are_required_by_pure_consent_guard():
    # Control-plane metadata only. This is not a client or a simulated flight.
    fence = {"epoch": 7, "term": 9, "run_id": "consented_run", "controller_id": "writer"}
    metadata = {
        "observation": {
            "time_s": 4.0,
            "trial_revision": 1,
            "tuning_revision": 2,
            "manual_interventions": 0,
            "seed": 100,
            "control_authority": {**fence, "status": "active"},
        },
        "runtime": {"running": True},
    }
    original = world_metadata(metadata)
    _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)
    metadata["observation"]["control_authority"]["epoch"] = 8
    with pytest.raises(CheckpointError, match="Original flight/cohort"):
        _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)
    metadata["observation"]["control_authority"]["epoch"] = 7
    metadata["observation"]["control_authority"]["term"] = 10
    with pytest.raises(CheckpointError):
        _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)
    metadata["observation"]["control_authority"]["term"] = 9
    metadata["runtime"]["running"] = False
    with pytest.raises(CheckpointError, match="pause"):
        _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)
    metadata["runtime"]["running"] = True
    metadata["observation"]["manual_interventions"] = 1
    with pytest.raises(CheckpointError):
        _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)


def test_expected_engine_build_is_part_of_the_original_world_guard():
    from wsorb.checkpoint import _engine_build_matches_expected

    fence = {"epoch": 7, "term": 9, "run_id": "consented_run", "controller_id": "writer"}
    metadata = {
        "observation": {
            "time_s": 4.0,
            "trial_revision": 1,
            "tuning_revision": 2,
            "manual_interventions": 0,
            "seed": 100,
            "control_authority": {**fence, "status": "active"},
        },
        "runtime": {"running": True},
        "engine_build": {"sha256": "a" * 64, "bytes": 1},
    }
    original = world_metadata(metadata)
    metadata["engine_build"] = {"sha256": "b" * 64, "bytes": 1}
    with pytest.raises(CheckpointError, match="Original flight/cohort"):
        _guard(metadata, original, fence, last_time_s=4.0, expect_running=True)
    assert not _engine_build_matches_expected({})
    assert not _engine_build_matches_expected(metadata)


def test_native_decision_lease_is_bounded_by_original_policy_validity():
    assert decision_lease({"decision_valid_until_elapsed_s": 12.0}, 10.0) == 0.75
    assert decision_lease({"decision_valid_until_elapsed_s": 10.25}, 10.0) == 0.25
    for until in (None, True, math.nan, math.inf, 10.01, 9.9):
        with pytest.raises(CheckpointError):
            decision_lease({"decision_valid_until_elapsed_s": until}, 10.0)
    with pytest.raises(CheckpointError):
        decision_lease({"decision_valid_until_elapsed_s": 1.0}, -1.0)


def test_command_axis_usage_labels_l1_proxy_separately_from_actual_l2_fuel():
    usage = _usage()
    _record_command(usage, np.array([0.3, 0.4, 0.0]), np.zeros(3))
    _record_command(usage, np.array([0.0, 0.0, 0.5]), np.zeros(3))
    assert usage["multi_axis_translation_samples"] == 1
    assert usage["single_axis_translation_samples"] == 1
    assert usage["active_body_axis_samples_xyz"] == [1, 1, 1]
    assert usage["sum_sampled_l1_accel_mps2"] == pytest.approx(1.2)
    assert usage["sum_sampled_l2_accel_mps2"] == pytest.approx(1.0)
    assert "not native fuel" in usage["l1_label"]
    assert "L2" in usage["actual_fuel_model"]


def test_comparison_configs_use_the_shared_policy_and_differ_only_in_axis_preference():
    baseline = asdict(policy_config("baseline"))
    axis = asdict(policy_config("body-axis"))
    assert baseline["body_axis_preference"] is False
    assert axis["body_axis_preference"] is True
    assert baseline["radar_clock_offset_s"] == axis["radar_clock_offset_s"] == 0.0
    assert baseline["policy"] == axis["policy"] == "checkpoint_velocity"
    assert baseline["max_speed_mps"] == axis["max_speed_mps"] == 8.0
    assert baseline["max_radar_age_s"] == axis["max_radar_age_s"] == 2.5
    assert baseline["tracking_diagnostics"] is axis["tracking_diagnostics"] is True
    baseline.pop("body_axis_preference")
    axis.pop("body_axis_preference")
    assert baseline == axis


def test_eligibility_requires_the_actual_reported_selected_and_accepted_command():
    # Pure report-contract validation. No client, SDK or engine response is substituted.
    report = {
        "failure_reasons": [],
        "decision_valid_until_elapsed_s": 5.0,
        "source_freshness": {"now_elapsed_time_s": 4.0},
        "command": {
            "kind": "coast",
            "written_to_any_api": False,
            "accel_body_mps2": [0, 0, 0],
            "angular_accel_body_rad_s2": [0, 0, 0],
        },
        "candidate_choice": {
            "selected_kind": "coast",
            "selected_axis_index": None,
            "candidates": [
                {
                    "kind": "coast",
                    "axis_index": None,
                    "accepted": True,
                    "eligible_for_maneuver_policy": True,
                    "accel_body_mps2": [0, 0, 0],
                }
            ],
        },
    }
    zero = np.zeros(3)
    assert policy_decision_eligible(report, zero, zero, 4.0)
    assert not policy_decision_eligible(report, np.array([0.1, 0, 0]), zero, 4.0)
    assert not policy_decision_eligible(report, zero, zero, 5.1)
    report["candidate_choice"]["candidates"][0]["accepted"] = False
    assert not policy_decision_eligible(report, zero, zero, 4.0)
    report["candidate_choice"]["candidates"][0]["accepted"] = True
    report["failure_reasons"] = ["Invalid coast"]
    assert not policy_decision_eligible(report, zero, zero, 4.0)


def test_parser_failures_do_not_echo_argv_or_credentials(capsys):
    token = secrets.token_urlsafe(32)
    assert main(["--isolated", "--unknown", token]) != 0
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_report_summary_is_bounded_and_json_finite():
    value = {"numbers": [float("nan"), float("inf"), 3.0], "rows": list(range(10000))}
    output = _summary(value)
    assert output["numbers"] == [None, None, 3.0]
    assert output["rows"][-1] == {"omitted_items": 9992}
    assert len(json.dumps(output, allow_nan=False)) < 500


def test_native_success_is_not_a_distance_or_score_substitute():
    # Pure interpretation of a numeric mission document, not a simulated response.
    observation = {
        "time_s": 200.0,
        "geometry": {"arrival_radius_m": 50.0},
        "mission": {"status": "running", "target_distance_m": 1.0, "damage": 0.0, "score": 1.0},
    }
    assert not native_success({"observation": observation})
    observation["mission"]["status"] = "success"
    assert native_success({"observation": observation})
    observation["mission"]["target_distance_m"] = 50.001
    assert not native_success({"observation": observation})
    observation["mission"]["target_distance_m"] = 50.0
    observation["mission"]["damage"] = 209200.001
    assert not native_success({"observation": observation})
    observation["mission"]["damage"] = 209200.0
    assert native_success({"observation": observation})
    observation["geometry"]["arrival_radius_m"] = 500.0
    assert not native_success({"observation": observation})
    values = native_measurement({"observation": observation})
    assert values["fuel_used_kg"] is None
    assert values["relative_speed_mps"] is None


def test_missing_navigation_cannot_become_a_fabricated_control_sample():
    with pytest.raises(CheckpointError):
        sample_from_snapshot({"observation": {"shuttle": None, "applied_physics": None}})


@pytest.mark.skipif(
    os.environ.get("WSORB_CHECKPOINT_INTEGRATION") != "1",
    reason="Opt in after the shared policy handoff to run one real owned Node/WASM server.",
)
def test_one_real_owned_stock_trial_is_read_only_until_explicit_run():
    from wsorb.checkpoint import inspect_trial, isolated_simulation, policy_config, wait_for_owner

    options = CheckpointOptions(isolated=True, run=True, duration_s=1.0, wall_budget_s=30.0)
    with isolated_simulation() as client:
        before = client.snapshot()
        # Existing owned native fixture, not a fabricated browser-ready receipt.
        # A stale invocation deadline cannot expire explicit persistent standby.
        wait_for_owner(
            client,
            CheckpointOptions(connection_file="owned-fixture.json", await_owner=True),
            deadline=0,
        )
        assert client.last_observed_page_id is client.bound_page_id is None
        inspected = inspect_trial(client)
        after = client.snapshot()
        assert inspected["state"] == "read_only"
        assert inspected["actuation_permitted"] is False
        assert inspected["stock_world"] is True
        assert after["runtime"]["running"] is False
        assert after["observation"]["time_s"] == before["observation"]["time_s"] == 0
        assert (
            after["observation"]["control_authority"] == before["observation"]["control_authority"]
        )
        sample = sample_from_snapshot(after)
        assert sample.source_kind == "local_public_snapshot"
        assert sample.rotation_convention == "active_xyzw"
        assert sample.frame == after["observation"]["frame"]
        assert sample.elapsed_time_s == after["observation"]["navigation_sample_time_s"]
        assert sample.position_world_m.tolist() == after["observation"]["shuttle"]["position"]
        expired = run_trial(client, options, policy="baseline", owned=True, deadline=0)
        assert expired["state"] == "wall_budget_limit"
        assert expired["decision_count"] == 0
        assert expired["release_state"] == "not_claimed"
        assert expired["actuation_outcome"] == "not_attempted"
        after_expired = client.snapshot()
        assert after_expired["runtime"]["running"] is False
        assert after_expired["observation"]["time_s"] == 0
        assert (
            after_expired["observation"]["control_authority"]
            == before["observation"]["control_authority"]
        )
        baseline, axis = policy_config("baseline"), policy_config("body-axis")
        assert baseline.radar_clock_offset_s == axis.radar_clock_offset_s == 0
        result = run_trial(client, options, policy="baseline", owned=True)
        assert "error" not in result, result.get("error")
        assert result["final"]["time_s"] >= 1
        assert result["world_metadata_unchanged"] is True
        assert result["release_state"] == "acknowledged"
        usage = result["command_usage"]
        assert usage["acknowledged_command_samples"] > 0
        assert usage["max_linear_norm_mps2"] <= 0.5
        assert usage["max_angular_norm_rad_s2"] <= 0.05
        assert math.isfinite(result["final"]["range_m"])
        final = client.snapshot()
        assert final["observation"]["control_authority"]["status"] != "active"
        assert final["observation"]["applied_physics"]["requested"]["accel_body_mps2"] == [0, 0, 0]
