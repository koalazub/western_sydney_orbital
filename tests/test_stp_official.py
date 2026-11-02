"""Pure numeric/config/source contracts for the STP official-profile port.

Every STPState, ControlSample and radar matrix below is constructed mathematical
input, not a captured SDK observation, a fake SDK backend or a simulated flight.
No test connects to the real spaceteams SDK and no timing behavior is asserted.
The module is a source port, not licensed-runtime evidence.
"""

import ast
import json
import math
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pytest

from wsorb.control_contract import STP_PHOBOS_FRAME
from wsorb.stp_api import (
    OFFICIAL_CONTROLLER_URL,
    OFFICIAL_RADAR_URL,
    OFFICIAL_SCORING_URL,
    OFFICIAL_SIM_COMMIT,
    OFFICIAL_SOLUTION_COMMIT,
    OFFICIAL_USER_API_URL,
    STPState,
)
from wsorb.stp_official import (
    CONFIG_FILE_ENV,
    CONFIG_JSON_ENV,
    CONFIG_SCHEMA,
    MAX_ACCEL_MPS2,
    MAX_ANGULAR_ACCEL_RAD_S2,
    MAX_CONFIG_BYTES,
    OFFICIAL_BEELINE_SPEED_MPS,
    STPOfficialConfig,
    STPOfficialController,
    decision_is_current,
    load_config_from_environment,
    parse_config,
)
from wsorb.stp_policy import sample_from_stp_state

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "src/wsorb/stp_official.py"
ENTRYPOINT = ROOT / "examples/ali/stp_official_profile.py"
NEW_FILES = (MODULE, ENTRYPOINT, Path(__file__))


def numeric_state(**changes):
    """An actual adapter-local data class filled with arbitrary finite numbers."""
    return replace(
        STPState(
            elapsed_time_s=100.0,
            position_phobos_m=np.array([1000.0, 0.0, 0.0]),
            velocity_phobos_m_s=np.zeros(3),
            passive_quaternion_phobos_to_body_xyzw=np.array([0.0, 0.0, 0.0, 1.0]),
            angular_velocity_phobos_rad_s=np.zeros(3),
            target_position_phobos_m=np.array([1100.0, 0.0, 0.0]),
            mass_kg=1000.0,
            inertia_body_kg_m2=np.diag([10.0, 20.0, 30.0]),
            fuel_used_kg=1.0,
            damage_j=0.0,
        ),
        **changes,
    )


def numeric_sample(**changes):
    """Constructed STP-derived ControlSample only; no SDK or local API source."""
    return replace(sample_from_stp_state(numeric_state()), **changes)


def numeric_radar(
    *,
    source_time=10.0,
    relative=(0.0, 1000.0, 0.0),
    closest_range=999.0,
    range_rate=0.0,
    diameter=2.0,
):
    return np.array(
        [[relative, [closest_range, range_rate, diameter], [source_time, 0.0, 0.0]]],
        dtype=np.float64,
    )


def enabled_config(**changes):
    return replace(STPOfficialConfig(enabled=True, radar_clock_offset_s=90.0), **changes)


def run(sample=None, radar=None, config=None, *, now=100.1):
    controller = STPOfficialController(enabled_config() if config is None else config)
    result = controller.command(
        numeric_sample() if sample is None else sample,
        numeric_radar() if radar is None else radar,
        now_elapsed_time_s=now,
    )
    return controller, result


def assert_coast(result):
    for value in result:
        assert value.shape == (3,)
        assert value.dtype == np.float64
        np.testing.assert_array_equal(value, np.zeros(3))


def test_published_ten_mps_beeline_uses_unity_velocity_error_gain():
    controller, (accel, angular) = run(numeric_sample(velocity_world_mps=np.array([9.9, 0.0, 0.0])))
    np.testing.assert_allclose(accel, [0.1, 0.0, 0.0], atol=1e-15)
    np.testing.assert_array_equal(angular, np.zeros(3))
    guidance = controller.report["guidance"]
    assert guidance["beeline_speed_mps"] == OFFICIAL_BEELINE_SPEED_MPS == 10.0
    assert guidance["velocity_error_gain_s"] == 1.0
    np.testing.assert_array_equal(guidance["desired_velocity_phobos_mps"], [10.0, 0.0, 0.0])
    assert guidance["target_distance_m"] == 100.0

    at_speed, (zero_accel, _) = run(numeric_sample(velocity_world_mps=np.array([10.0, 0.0, 0.0])))
    np.testing.assert_allclose(zero_accel, np.zeros(3), atol=1e-15)
    assert at_speed.report["guidance"]["desired_velocity_phobos_mps"] == [
        10.0,
        0.0,
        0.0,
    ]

    diagonal, _ = run(numeric_sample(target_position_world_m=np.array([1100.0, 100.0, 0.0])))
    desired = diagonal.report["guidance"]["desired_velocity_phobos_mps"]
    assert math.hypot(*desired) == pytest.approx(10.0)
    assert desired[0] > 0.0 and desired[1] > 0.0


def test_commands_obey_public_caps_and_are_explicitly_bounded():
    controller, (accel, angular) = run(
        numeric_sample(
            velocity_world_mps=np.array([-1000.0, 0.0, 0.0]),
            angular_velocity_world_rad_s=np.array([100.0, 200.0, 300.0]),
        )
    )
    assert math.hypot(*accel) <= MAX_ACCEL_MPS2
    assert math.hypot(*angular) <= MAX_ANGULAR_ACCEL_RAD_S2
    assert math.hypot(*accel) > 0.0
    assert math.hypot(*angular) > 0.0
    assert np.isfinite(accel).all() and np.isfinite(angular).all()
    report = controller.report
    assert report["command"]["kind"] == "official_beeline"
    np.testing.assert_array_equal(report["command"]["accel_body_mps2"], accel)
    np.testing.assert_array_equal(report["command"]["angular_accel_body_rad_s2"], angular)
    assert report["command"]["written_to_any_api"] is False


def test_near_public_radar_repels_and_suppresses_the_beeline():
    baseline, (nominal, _) = run()
    np.testing.assert_allclose(nominal, [0.5, 0.0, 0.0], atol=1e-12)

    controller, (repelled, _) = run(
        radar=numeric_radar(relative=(0.0, 1.0, 0.0), closest_range=0.0),
        config=enabled_config(radar_margin_m=0.0),
    )
    np.testing.assert_allclose(repelled, [0.0, -0.5, 0.0], atol=1e-12)
    assert math.hypot(*repelled) <= MAX_ACCEL_MPS2
    assert controller.report["radar"]["in_range_rows"] == 1
    assert controller.report["radar"]["max_proximity_weight"] == 1.0
    assert baseline.report["radar"]["in_range_rows"] == 0


def test_closing_range_rate_increases_repulsion_while_receding_rate_does_not():
    closing, (closing_accel, _) = run(
        radar=numeric_radar(
            relative=(0.0, 101.0, 0.0),
            closest_range=100.0,
            range_rate=-20.0,
        ),
        config=enabled_config(radar_margin_m=0.0),
    )
    receding, (receding_accel, _) = run(
        radar=numeric_radar(
            relative=(0.0, 101.0, 0.0),
            closest_range=100.0,
            range_rate=20.0,
        ),
        config=enabled_config(radar_margin_m=0.0),
    )
    assert closing_accel[1] < 0.0
    assert 0.0 < closing_accel[0] < receding_accel[0]
    np.testing.assert_allclose(receding_accel, [0.5, 0.0, 0.0], atol=1e-12)
    assert closing.report["radar"]["max_proximity_weight"] > 0.0
    assert receding.report["radar"]["max_proximity_weight"] == 0.0


def test_body_plus_x_alignment_uses_shortest_bounded_arc():
    _, (_, aligned) = run()
    np.testing.assert_array_equal(aligned, np.zeros(3))

    controller, (_, toward_positive_y) = run(
        numeric_sample(target_position_world_m=np.array([1000.0, 100.0, 0.0]))
    )
    assert toward_positive_y[2] > 0.0
    assert math.hypot(*toward_positive_y) <= MAX_ANGULAR_ACCEL_RAD_S2
    attitude = controller.report["attitude"]
    assert attitude["alignment_angle_rad"] == pytest.approx(math.pi / 2.0)
    np.testing.assert_allclose(attitude["body_plus_x_phobos"], [1.0, 0.0, 0.0], atol=1e-15)

    half_turn, (_, reversed_arc) = run(
        numeric_sample(target_position_world_m=np.array([900.0, 0.0, 0.0]))
    )
    assert math.hypot(*reversed_arc) <= MAX_ANGULAR_ACCEL_RAD_S2
    assert math.hypot(*reversed_arc) > 0.0
    assert half_turn.report["attitude"]["alignment_angle_rad"] == pytest.approx(math.pi)

    quaternion = np.array([0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)])
    rotated, (_, rotated_alignment) = run(
        numeric_sample(
            quaternion_xyzw=quaternion,
            target_position_world_m=np.array([1000.0, 100.0, 0.0]),
        )
    )
    np.testing.assert_allclose(
        rotated.report["attitude"]["body_plus_x_phobos"], [0.0, 1.0, 0.0], atol=1e-15
    )
    assert rotated.report["attitude"]["alignment_angle_rad"] == pytest.approx(0.0)
    np.testing.assert_array_equal(rotated_alignment, np.zeros(3))


@pytest.mark.parametrize(
    "sample,radar,now",
    [
        (numeric_sample(), numeric_radar(), 102.0),
        (numeric_sample(), numeric_radar(), 99.9),
        (numeric_sample(), numeric_radar(source_time=10.05), 100.1),
        (numeric_sample(), numeric_radar(source_time=8.0), 100.1),
        (numeric_sample(), np.zeros((0, 3, 3)), 100.1),
        (numeric_sample(), np.zeros((1, 3, 2)), 100.1),
        (numeric_sample(), np.ones((1, 3, 3), dtype=bool), 100.1),
        (numeric_sample(), numeric_radar(closest_range=math.nan), 100.1),
        (numeric_sample(), numeric_radar(diameter=0.0), 100.1),
        (numeric_sample(), numeric_radar(relative=(0.0, 0.0, 0.0)), 100.1),
        (None, numeric_radar(), 100.1),
        (numeric_sample(elapsed_time_s=math.nan), numeric_radar(), 100.1),
        (
            numeric_sample(quaternion_xyzw=np.zeros(4)),
            numeric_radar(),
            100.1,
        ),
        (numeric_sample(frame="local frame"), numeric_radar(), 100.1),
        (numeric_sample(source_kind="local_api_sample"), numeric_radar(), 100.1),
        (
            numeric_sample(rotation_convention="active_xyzw"),
            numeric_radar(),
            100.1,
        ),
    ],
)
def test_stale_future_and_malformed_data_fails_closed_to_zero(sample, radar, now):
    controller = STPOfficialController(enabled_config())
    assert_coast(controller.command(sample, radar, now_elapsed_time_s=now))
    report = controller.report
    assert report["failure_reasons"]
    assert report["coast_calls"] == 1
    assert report["command"]["kind"] == "coast"
    assert report["command"]["written_to_any_api"] is False


def test_disabled_controller_returns_zero_and_does_not_imply_permission_to_write():
    controller = STPOfficialController(STPOfficialConfig())
    assert_coast(controller.command(numeric_sample(), numeric_radar(), now_elapsed_time_s=100.1))
    report = controller.report
    assert "read-only" in report["failure_reasons"][0]
    assert report["command"]["kind"] == "coast"
    assert report["command"]["written_to_any_api"] is False
    assert report["provenance"]["licensed_runtime_evidence"] is False


def test_report_provenance_preserves_source_times_and_invents_no_identity():
    rows = np.concatenate(
        [
            numeric_radar(source_time=10.0),
            numeric_radar(source_time=9.5, closest_range=500.0),
        ]
    )
    original = rows.copy()
    controller, _ = run(radar=rows)
    report = controller.report
    freshness = report["source_freshness"]
    assert freshness["radar_source_times_s"] == [10.0, 9.5]
    assert freshness["radar_measurement_times_s"] == [100.0, 99.5]
    assert freshness["source_time_spread_s"] == pytest.approx(0.5)
    assert freshness["observed_max_radar_age_s"] == pytest.approx(0.6)
    assert report["radar"]["rows"] == 2
    assert report["radar"]["anonymous"] is True
    provenance = report["provenance"]
    assert provenance["kind"] == "source_port_of_published_official_profile"
    assert provenance["licensed_runtime_evidence"] is False
    assert provenance["probe_validated_on_real_server"] is False
    assert provenance["official_solution_commit"] == OFFICIAL_SOLUTION_COMMIT
    assert provenance["official_sim_commit"] == OFFICIAL_SIM_COMMIT
    assert provenance["user_api_url"] == OFFICIAL_USER_API_URL
    assert provenance["controller_url"] == OFFICIAL_CONTROLLER_URL
    assert provenance["radar_url"] == OFFICIAL_RADAR_URL
    assert provenance["scoring_url"] == OFFICIAL_SCORING_URL
    assert provenance["state_frame"] == STP_PHOBOS_FRAME
    assert provenance["state_source_kind"] == "stp_adapter_numeric_report"
    assert provenance["rotation_convention"] == "passive_xyzw"
    assert "not published" in provenance["identities"]
    assert "not published" in provenance["covariances"]
    assert "licensed-runtime evidence" in report["limitations"]
    np.testing.assert_array_equal(rows, original)

    keys = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(key)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(report)
    assert "id" not in keys
    assert "covariance" not in keys
    assert "covariance_matrix" not in keys
    json.dumps(report, allow_nan=False)

    report["source_freshness"].clear()
    assert controller.report["source_freshness"]["radar_source_times_s"] == [
        10.0,
        9.5,
    ]


def test_decision_deadline_is_not_extended_by_later_clock_reads():
    controller, _ = run()
    report = controller.report
    assert report["decision_valid_until_elapsed_s"] == pytest.approx(101.0)
    assert decision_is_current(report, now_elapsed_time_s=101.0)
    assert not decision_is_current(report, now_elapsed_time_s=101.000001)
    assert not decision_is_current(report, now_elapsed_time_s=100.0)
    assert not decision_is_current(report, now_elapsed_time_s=True)
    assert not decision_is_current(report, now_elapsed_time_s=math.nan)

    short, _ = run(config=enabled_config(max_report_age_s=0.2))
    assert short.report["decision_valid_until_elapsed_s"] == pytest.approx(100.2)
    assert decision_is_current(short.report, now_elapsed_time_s=100.2)
    assert not decision_is_current(short.report, now_elapsed_time_s=100.200001)


def test_config_is_disabled_by_default_and_requires_an_explicit_offset_to_enable():
    assert STPOfficialConfig() == STPOfficialConfig(
        enabled=False, radar_clock_offset_s=None, probe=False
    )
    with pytest.raises(ValueError, match="explicit radar_clock_offset_s"):
        STPOfficialConfig(enabled=True)
    assert STPOfficialConfig(enabled=True, radar_clock_offset_s=0.0).enabled
    assert STPOfficialConfig(probe=True).probe
    for name in ("enabled", "probe"):
        with pytest.raises(ValueError, match="explicit boolean"):
            STPOfficialConfig(**{name: 1})
    for changes in (
        {"duration_wall_s": 0.0},
        {"duration_wall_s": 3600.001},
        {"max_report_age_s": -0.1},
        {"max_radar_age_s": 5.401},
        {"attitude_response_time_s": 0.001},
        {"avoidance_range_m": 0.0},
        {"avoidance_accel_mps2": 0.500001},
        {"radar_margin_m": -1.0},
        {"avoidance_horizon_s": 30.001},
        {"radar_clock_offset_s": 1e9 + 1.0},
        {"max_report_age_s": True},
        {"avoidance_range_m": math.nan},
        {"avoidance_accel_mps2": math.inf},
    ):
        with pytest.raises(ValueError):
            replace(STPOfficialConfig(), **changes)


def test_versioned_config_roundtrip_and_strict_json_rejection():
    disabled = '{"schema":"wsorb.stp_official","schema_version":1,"enabled":false}'
    assert parse_config(disabled) == STPOfficialConfig()
    config = enabled_config()
    document = {
        "schema": CONFIG_SCHEMA,
        "schema_version": 1,
        **asdict(config),
    }
    assert parse_config(json.dumps(document)) == config

    for source in (
        "{}",
        "[]",
        "null",
        '{"schema":"wsorb.stp_official","schema_version":1}',
        json.dumps({**document, "schema": "different"}),
        json.dumps({**document, "schema_version": True}),
        json.dumps({**document, "schema_version": 1.0}),
        json.dumps({**document, "schema_version": 2}),
        json.dumps({**document, "unknown_field": 1}),
        json.dumps({**document, "enabled": 1}),
        json.dumps({**document, "radar_clock_offset_s": None}),
        ('{"schema":"wsorb.stp_official","schema_version":1,"enabled":false,"enabled":true}'),
        (
            '{"schema":"wsorb.stp_official","schema_version":1,"enabled":false,'
            '"max_report_age_s":NaN}'
        ),
        (
            '{"schema":"wsorb.stp_official","schema_version":1,"enabled":false,'
            '"max_report_age_s":1e309}'
        ),
        b"\xff",
        " " * (MAX_CONFIG_BYTES + 1),
    ):
        with pytest.raises(ValueError):
            parse_config(source)


def test_environment_config_uses_only_explicit_local_data(tmp_path):
    assert load_config_from_environment({}) == STPOfficialConfig()
    source = json.dumps(
        {
            "schema": CONFIG_SCHEMA,
            "schema_version": 1,
            **asdict(enabled_config()),
        }
    )
    assert load_config_from_environment({CONFIG_JSON_ENV: source}) == enabled_config()
    path = tmp_path / "official.json"
    path.write_text(source, encoding="utf-8")
    assert load_config_from_environment({CONFIG_FILE_ENV: str(path)}) == enabled_config()
    with pytest.raises(ValueError, match="Select only one"):
        load_config_from_environment({CONFIG_FILE_ENV: str(path), CONFIG_JSON_ENV: source})
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_FILE_ENV: str(tmp_path)})
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_FILE_ENV: "https://example.invalid/a.json"})
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_JSON_ENV: ""})


def _function(tree, name):
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node):
    return [ast.unparse(item) for item in ast.walk(node) if isinstance(item, ast.Call)]


def test_new_files_are_python310_syntax_and_do_not_import_a_fake_sdk():
    for path in NEW_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert "spaceteams" not in name
                assert "flight" not in name.split(".")


def test_source_port_has_no_command_writes_and_entrypoint_has_one_writer():
    module_source = MODULE.read_text(encoding="utf-8")
    assert "SetCommand" not in module_source
    assert "SetParam" not in module_source

    tree = ast.parse(ENTRYPOINT.read_text(encoding="utf-8"), feature_version=(3, 10))
    writer = _function(tree, "_write_command")
    writer_calls = _calls(writer)
    assert "client.SetCommand(accel, angular_accel)" in writer_calls

    set_command_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".SetCommand")
    ]
    assert len(set_command_calls) == 1
    assert set_command_calls[0] in list(ast.walk(writer))

    main = _function(tree, "main")
    connect_calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "connect_from_argv"
    ]
    assert len(connect_calls) == 1
    assert connect_calls[0].args == []
    assert connect_calls[0].keywords == []

    with_statement = next(node for node in ast.walk(main) if isinstance(node, ast.With))
    assert ast.unparse(with_statement.items[0].context_expr) == "connect_from_argv()"
    before = main.body[main.body.index(with_statement) - 1]
    assert isinstance(before, ast.If)
    assert isinstance(before.test, ast.BoolOp)
    assert isinstance(before.test.op, ast.And)
    assert [ast.unparse(value) for value in before.test.values] == [
        "not config.enabled",
        "not config.probe",
    ]
    assert isinstance(before.body[-1], ast.Return)
    assert not any("_write_command" in call for call in _calls(before))
    assert not any("SetCommand" in call for call in _calls(before))

    probe_branch = next(
        node
        for node in with_statement.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "not config.enabled"
    )
    assert isinstance(probe_branch.body[-1], ast.Return)
    probe_calls = _calls(probe_branch)
    assert any("client.read_only_probe" in call for call in probe_calls)
    assert not any("_write_command" in call for call in probe_calls)
    assert not any("SetCommand" in call for call in probe_calls)

    assert not any("_write_command" in call for call in _calls(main))
    assert any("_write_command" in call for call in _calls(_function(tree, "_run_enabled")))
    assert not any("parse_args" in call for call in _calls(main))
    assert "argparse" not in ENTRYPOINT.read_text(encoding="utf-8")
