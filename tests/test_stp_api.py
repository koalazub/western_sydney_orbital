"""Pure numeric/config contracts, not SDK, sensor or flight validation.

STPState instances and radar arrays below are constructed mathematical inputs.
They are not captured observations, SDK substitutes or simulated trajectories.
No fake client, monkeypatched SDK, network, physics or graphics are used.
"""

import ast
import json
import math
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, fields, replace
from importlib.util import find_spec
from pathlib import Path

import numpy as np
import pytest

from wsorb.control_contract import (
    STP_PHOBOS_FRAME,
    ControlSample,
    body_to_reference,
    checked_sample,
    reference_to_body,
)
from wsorb.stp_api import (
    OFFICIAL_SIM_COMMIT,
    OFFICIAL_SOLUTION_COMMIT,
    STPState,
    _finite_real_vector,
    connect_from_argv,
)
from wsorb.stp_policy import (
    CONFIG_FILE_ENV,
    CONFIG_JSON_ENV,
    CONFIG_SCHEMA,
    MAX_CONFIG_BYTES,
    STPCheckpointController,
    STPPolicyConfig,
    _command_bounded,
    decide_command,
    decision_is_current,
    load_config_from_environment,
    parse_config,
    sample_from_stp_state,
)


def test_command_vector_validation_returns_an_independent_real_three_vector():
    original = np.array([0.0, 0.0, 0.5])
    result = _finite_real_vector(original, 3, "accelCommand")
    np.testing.assert_array_equal(result, original)
    assert result.dtype == np.float64
    result[0] = 0.25
    np.testing.assert_array_equal(original, [0.0, 0.0, 0.5])


@pytest.mark.parametrize(
    "value",
    [
        [0.0, 0.0],
        [[0.0, 0.0, 0.5]],
        [0.0, np.nan, 0.0],
        [0.0, np.inf, 0.0],
        [0.0, 0.0, 1j],
        [True, False, True],
        ["0", "0", "0.5"],
    ],
)
def test_invalid_command_vectors_are_rejected_without_an_sdk(value):
    with pytest.raises(ValueError, match="finite real vector of shape"):
        _finite_real_vector(value, 3, "accelCommand")


def test_missing_official_sdk_fails_clearly_instead_of_using_a_local_substitute():
    if find_spec("spaceteams") is not None:
        pytest.skip("A licensed SDK is installed; connecting is not part of this test")
    with pytest.raises(RuntimeError, match="real 'spaceteams' SDK could not be loaded"):
        connect_from_argv([])


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
    return replace(STPPolicyConfig(enabled=True, radar_clock_offset_s=90.0), **changes)


def config_document(**changes):
    return {
        "schema": CONFIG_SCHEMA,
        "schema_version": 1,
        **asdict(enabled_config()),
        **changes,
    }


def assert_coast(command):
    for value in command:
        assert value.shape == (3,)
        assert value.dtype == np.float64
        np.testing.assert_array_equal(value, np.zeros(3))


def test_unconfigured_disabled_and_missing_fresh_clock_only_coast():
    state, radar = numeric_state(), numeric_radar()
    assert_coast(decide_command(state, radar))
    assert_coast(decide_command(state, radar, STPPolicyConfig(), now_elapsed_time_s=100.1))
    assert_coast(decide_command(state, radar, enabled_config()))
    assert_coast(decide_command(None, None, enabled_config(), now_elapsed_time_s=100.1))


def test_enabled_valid_target_pd_is_not_a_zero_placeholder():
    linear, angular = decide_command(
        numeric_state(), numeric_radar(), enabled_config(), now_elapsed_time_s=100.1
    )
    np.testing.assert_allclose(linear, [0.12, 0.0, 0.0])
    np.testing.assert_array_equal(angular, [0.0, 0.0, 0.0])


def test_velocity_and_spin_damping_use_phobos_relative_state():
    linear, angular = decide_command(
        numeric_state(
            velocity_phobos_m_s=np.array([1.0, 2.0, 0.0]),
            angular_velocity_phobos_rad_s=np.array([0.0, 0.0, 0.1]),
        ),
        numeric_radar(),
        enabled_config(),
        now_elapsed_time_s=100.1,
    )
    np.testing.assert_allclose(linear, [0.05, -0.14, 0.0])
    np.testing.assert_allclose(angular, [0.0, 0.0, -0.01])


@pytest.mark.parametrize("scale", [1.0, -1.0, 1e300])
def test_official_raw_passive_xyzw_rotates_reference_x_to_body_negative_y(scale):
    quaternion = np.array([0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)]) * scale
    linear, angular = decide_command(
        numeric_state(
            passive_quaternion_phobos_to_body_xyzw=quaternion,
            angular_velocity_phobos_rad_s=np.array([0.1, 0.0, 0.0]),
        ),
        numeric_radar(),
        enabled_config(),
        now_elapsed_time_s=100.1,
    )
    np.testing.assert_allclose(linear, [0.0, -0.12, 0.0], atol=1e-15)
    np.testing.assert_allclose(angular, [0.0, 0.01, 0.0], atol=1e-15)


@pytest.mark.parametrize("linear_limit,angular_limit", [(0.5, 0.05), (0.03, 0.002), (0.0, 0.0)])
def test_command_norms_obey_public_caps_and_stricter_reviewed_limits(linear_limit, angular_limit):
    command = decide_command(
        numeric_state(
            target_position_phobos_m=np.array([1e8, 3e8, -2e8]),
            angular_velocity_phobos_rad_s=np.array([100.0, 200.0, 300.0]),
        ),
        numeric_radar(),
        enabled_config(max_accel_mps2=linear_limit, max_angular_accel_rad_s2=angular_limit),
        now_elapsed_time_s=100.1,
    )
    for value, limit in zip(command, (linear_limit, angular_limit), strict=True):
        assert np.isfinite(value).all()
        assert math.hypot(*value) <= limit
        if limit:
            assert math.hypot(*value) > 0.0


@pytest.mark.parametrize("closest_range", [0.0, -1.0])
def test_close_public_radar_repels_and_suppresses_target_thrust(closest_range):
    linear, _ = decide_command(
        numeric_state(),
        numeric_radar(relative=(1.0, 0.0, 0.0), closest_range=closest_range),
        enabled_config(),
        now_elapsed_time_s=100.1,
    )
    np.testing.assert_allclose(linear, [-0.5, 0.0, 0.0])


def test_many_anonymous_rows_cannot_exceed_the_repulsion_budget():
    rows = np.repeat(numeric_radar(relative=(1.0, 0.0, 0.0), closest_range=0.0), 500, axis=0)
    linear, _ = decide_command(
        numeric_state(),
        rows,
        enabled_config(avoidance_accel_mps2=0.2),
        now_elapsed_time_s=100.1,
    )
    assert linear[0] < 0.0
    assert math.hypot(*linear) <= 0.2


def test_closing_rate_adds_radial_repulsion_but_receding_rate_does_not():
    closing, _ = decide_command(
        numeric_state(),
        numeric_radar(relative=(0.0, 101.0, 0.0), closest_range=100.0, range_rate=-20.0),
        enabled_config(),
        now_elapsed_time_s=100.1,
    )
    receding, _ = decide_command(
        numeric_state(),
        numeric_radar(relative=(0.0, 101.0, 0.0), closest_range=100.0, range_rate=20.0),
        enabled_config(),
        now_elapsed_time_s=100.1,
    )
    assert closing[1] < 0.0
    assert 0.0 < closing[0] < receding[0]
    np.testing.assert_allclose(receding, [0.12, 0.0, 0.0])


def test_finite_extreme_response_range_does_not_overflow_the_proximity_weight():
    linear, _ = decide_command(
        numeric_state(),
        numeric_radar(relative=(0.0, 1e300, 0.0), closest_range=1e300),
        enabled_config(avoidance_range_m=1e-300),
        now_elapsed_time_s=100.1,
    )
    np.testing.assert_allclose(linear, [0.12, 0.0, 0.0])


def test_declared_clock_conversion_keeps_source_rows_and_state_arrays_unchanged():
    state, rows = numeric_state(), numeric_radar()
    source_rows = rows.copy()
    originals = {}
    rows.flags.writeable = False
    for item in fields(state):
        value = getattr(state, item.name)
        if isinstance(value, np.ndarray):
            originals[item.name] = value.copy()
            value.flags.writeable = False
    offset_command = decide_command(state, rows, enabled_config(), now_elapsed_time_s=100.1)
    shared_origin_command = decide_command(
        state,
        numeric_radar(source_time=100.0),
        enabled_config(radar_clock_offset_s=0.0),
        now_elapsed_time_s=100.1,
    )
    for left, right in zip(offset_command, shared_origin_command, strict=True):
        np.testing.assert_array_equal(left, right)
    negative_offset_command = decide_command(
        state,
        numeric_radar(source_time=190.0),
        enabled_config(radar_clock_offset_s=-90.0),
        now_elapsed_time_s=100.1,
    )
    for left, right in zip(offset_command, negative_offset_command, strict=True):
        np.testing.assert_array_equal(left, right)
    np.testing.assert_array_equal(rows, source_rows)
    for name, original in originals.items():
        np.testing.assert_array_equal(getattr(state, name), original)


@pytest.mark.parametrize("offset", [0.0, 88.0, 90.01, -11.0])
def test_stale_future_or_negative_normalized_times_do_not_trigger_offset_inference(offset):
    assert_coast(
        decide_command(
            numeric_state(),
            numeric_radar(),
            enabled_config(radar_clock_offset_s=offset),
            now_elapsed_time_s=100.1,
        )
    )


def test_radar_future_relative_to_report_is_rejected_even_before_the_later_clock():
    assert_coast(
        decide_command(
            numeric_state(),
            numeric_radar(source_time=10.05),
            enabled_config(),
            now_elapsed_time_s=100.1,
        )
    )


def test_radar_age_boundary_is_not_silently_extended():
    config = enabled_config(max_radar_age_s=1.0)
    valid, _ = decide_command(
        numeric_state(), numeric_radar(source_time=9.0), config, now_elapsed_time_s=100.0
    )
    assert valid[0] > 0.0
    assert_coast(
        decide_command(
            numeric_state(),
            numeric_radar(source_time=8.999999),
            config,
            now_elapsed_time_s=100.0,
        )
    )


@pytest.mark.parametrize("now", [None, True, "100.1", math.nan, math.inf, -1.0, 99.9, 102.0])
def test_invalid_fresh_clock_or_stale_future_state_reports_only_coast(now):
    assert_coast(
        decide_command(numeric_state(), numeric_radar(), enabled_config(), now_elapsed_time_s=now)
    )


def test_report_age_is_checked_separately_from_radar_age():
    assert_coast(
        decide_command(
            numeric_state(),
            numeric_radar(),
            enabled_config(max_report_age_s=0.25, max_radar_age_s=1.5),
            now_elapsed_time_s=100.5,
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"elapsed_time_s": math.nan},
        {"elapsed_time_s": True},
        {"position_phobos_m": np.array([math.inf, 0.0, 0.0])},
        {"position_phobos_m": np.zeros((1, 3))},
        {"velocity_phobos_m_s": np.array([0.0, math.nan, 0.0])},
        {"passive_quaternion_phobos_to_body_xyzw": np.zeros(4)},
        {"passive_quaternion_phobos_to_body_xyzw": np.array([0.0, 0.0, math.nan, 1.0])},
        {"angular_velocity_phobos_rad_s": np.array([0.0, 0.0, math.inf])},
        {"target_position_phobos_m": np.array([0.0, math.nan, 0.0])},
        {"mass_kg": 0.0},
        {"mass_kg": math.inf},
        {"inertia_body_kg_m2": np.eye(2)},
        {"inertia_body_kg_m2": np.diag([1.0, 0.0, 1.0])},
        {"inertia_body_kg_m2": np.diag([1.0, -1.0, 1.0])},
        {"inertia_body_kg_m2": np.array([[1.0, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])},
        {"inertia_body_kg_m2": np.diag([1.0, math.nan, 1.0])},
        {"fuel_used_kg": -1.0},
        {"fuel_used_kg": math.nan},
        {"damage_j": math.inf},
        {"damage_j": True},
    ],
)
def test_invalid_state_cannot_fall_back_to_target_thrust(changes):
    assert_coast(
        decide_command(
            numeric_state(**changes), numeric_radar(), enabled_config(), now_elapsed_time_s=100.1
        )
    )


@pytest.mark.parametrize(
    "radar",
    [
        None,
        [],
        np.zeros((0, 3, 3)),
        np.zeros((3, 3)),
        np.zeros((1, 3, 2)),
        np.ones((1, 3, 3), dtype=bool),
        np.ones((1, 3, 3), dtype=complex),
        np.full((1, 3, 3), "1"),
        numeric_radar(source_time=-1.0),
        numeric_radar(source_time=math.nan),
        numeric_radar(closest_range=math.inf),
        numeric_radar(range_rate=math.nan),
        numeric_radar(diameter=0.0),
        numeric_radar(relative=(0.0, 0.0, 0.0)),
        [[[1.0, 0.0, 0.0], [1.0, 0.0, 2.0], [10.0, 1.0, 0.0]]],
        np.concatenate([numeric_radar(), numeric_radar(source_time=math.inf)]),
        np.concatenate([numeric_radar(), numeric_radar(source_time=5.0)]),
    ],
)
def test_invalid_or_empty_radar_cannot_fall_back_to_target_thrust(radar):
    assert_coast(decide_command(numeric_state(), radar, enabled_config(), now_elapsed_time_s=100.1))


def test_nonrepresentable_pd_radial_lookahead_and_spin_damping_only_coast():
    assert_coast(
        decide_command(
            numeric_state(
                position_phobos_m=np.array([-1e308, 0.0, 0.0]),
                target_position_phobos_m=np.array([1e308, 0.0, 0.0]),
            ),
            numeric_radar(),
            enabled_config(),
            now_elapsed_time_s=100.1,
        )
    )
    assert_coast(
        decide_command(
            numeric_state(),
            numeric_radar(range_rate=-1e308),
            enabled_config(avoidance_horizon_s=30.0),
            now_elapsed_time_s=100.1,
        )
    )
    assert_coast(
        decide_command(
            numeric_state(angular_velocity_phobos_rad_s=np.array([1e308, 0.0, 0.0])),
            numeric_radar(),
            enabled_config(angular_velocity_gain_s=10.0),
            now_elapsed_time_s=100.1,
        )
    )


def test_enabled_configuration_requires_an_explicit_offset_and_is_frozen():
    with pytest.raises(ValueError, match="explicit radar_clock_offset_s"):
        STPPolicyConfig(enabled=True)
    assert STPPolicyConfig(enabled=True, radar_clock_offset_s=0.0).enabled
    with pytest.raises(FrozenInstanceError):
        enabled_config().enabled = False


@pytest.mark.parametrize("enabled", [1, 0, "true", None, np.bool_(True)])
def test_operator_enable_is_a_real_boolean_not_truthiness(enabled):
    with pytest.raises(ValueError, match="explicit boolean"):
        STPPolicyConfig(enabled=enabled, radar_clock_offset_s=0.0)


@pytest.mark.parametrize(
    "name",
    [
        item.name
        for item in fields(STPPolicyConfig)
        if item.name not in ("enabled", "policy", "body_axis_preference", "tracking_diagnostics")
    ],
)
@pytest.mark.parametrize("value", [True, np.bool_(False), "1", math.nan, math.inf, 1j])
def test_all_numeric_configuration_fields_reject_nonfinite_boolean_and_nonreal_values(name, value):
    with pytest.raises(ValueError):
        replace(STPPolicyConfig(), **{name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_wall_s": 0.0},
        {"duration_wall_s": 300.001},
        {"max_report_age_s": -0.1},
        {"max_report_age_s": 5.401},
        {"max_radar_age_s": 5.401},
        {"position_gain_s2": -0.1},
        {"position_gain_s2": 1.001},
        {"velocity_gain_s": 10.001},
        {"angular_velocity_gain_s": 10.001},
        {"avoidance_range_m": 0.0},
        {"avoidance_range_m": 1e6 + 1.0},
        {"avoidance_horizon_s": 30.001},
        {"avoidance_accel_mps2": 0.500001},
        {"max_accel_mps2": 0.500001},
        {"max_angular_accel_rad_s2": 0.050001},
        {"radar_clock_offset_s": 1e9 + 1.0},
    ],
)
def test_configuration_review_budgets_and_command_caps_are_enforced(changes):
    with pytest.raises(ValueError):
        replace(STPPolicyConfig(), **changes)


def test_versioned_configuration_roundtrip_and_explicit_disabled_document():
    assert parse_config(json.dumps(config_document())) == enabled_config()
    disabled = '{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false}'
    assert parse_config(disabled) == STPPolicyConfig()


@pytest.mark.parametrize(
    "source",
    [
        "{}",
        "[]",
        "null",
        '{"schema":"wsorb.stp_policy","schema_version":1}',
        json.dumps(config_document(schema="different_policy")),
        json.dumps(config_document(schema_version=True)),
        json.dumps(config_document(schema_version=1.0)),
        json.dumps(config_document(schema_version=2)),
        json.dumps(config_document(unknown_field=1)),
        json.dumps(config_document(enabled=1)),
        json.dumps(config_document(radar_clock_offset_s=None)),
        '{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false,"enabled":true}',
        '{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false,"duration_wall_s":NaN}',
        '{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false,'
        '"duration_wall_s":Infinity}',
        '{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false,"duration_wall_s":1e309}',
        b"\xff",
        "[" * 2000 + "]" * 2000,
        " " * (MAX_CONFIG_BYTES + 1),
    ],
)
def test_configuration_json_rejects_wrong_schema_duplicates_nonfinite_and_invalid_input(source):
    with pytest.raises(ValueError):
        parse_config(source)


def test_explicit_environment_data_and_local_file_do_not_touch_sdk_argv(tmp_path):
    argv_before = tuple(sys.argv)
    source = json.dumps(config_document())
    assert load_config_from_environment({CONFIG_JSON_ENV: source}) == enabled_config()
    path = tmp_path / "policy.json"
    path.write_text(source, encoding="utf-8")
    assert load_config_from_environment({CONFIG_FILE_ENV: str(path)}) == enabled_config()
    assert load_config_from_environment({}) == STPPolicyConfig()
    assert tuple(sys.argv) == argv_before


@pytest.mark.parametrize(
    "environ",
    [
        {CONFIG_FILE_ENV: "", CONFIG_JSON_ENV: ""},
        {CONFIG_FILE_ENV: " "},
        {CONFIG_JSON_ENV: ""},
        {CONFIG_JSON_ENV: "not JSON"},
        {CONFIG_FILE_ENV: "https://example.invalid/policy.json"},
    ],
)
def test_invalid_environment_configuration_aborts_instead_of_silently_disabling(environ):
    with pytest.raises(ValueError):
        load_config_from_environment(environ)


def test_configuration_file_must_be_regular_and_size_bounded(tmp_path):
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_FILE_ENV: str(tmp_path)})
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_FILE_ENV: str(tmp_path / "missing.json")})
    path = tmp_path / "oversize.json"
    path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
    with pytest.raises(ValueError):
        load_config_from_environment({CONFIG_FILE_ENV: str(path)})


def checkpoint_config(**changes):
    return replace(
        enabled_config(policy="checkpoint_velocity", checkpoint_radius_m=50.0, max_radar_age_s=2.5),
        **changes,
    )


def numeric_sample(**changes):
    """Constructed numeric data only, never an SDK or local API observation."""
    return replace(sample_from_stp_state(numeric_state()), **changes)


def test_tracking_anchors_ego_motion_to_the_state_epoch_not_a_later_clock_read():
    """Numeric time/frame contract, without a substituted SDK or sensor backend."""
    sample = numeric_sample(velocity_world_mps=np.array([2.0, 0.0, 0.0]))
    controller = STPCheckpointController(checkpoint_config())
    controller.command(sample, numeric_radar(), now_elapsed_time_s=100.5)
    report = controller.report["track_summary"]
    assert report["now_s"] == report["ego_state_time_s"] == sample.elapsed_time_s
    assert report["control_read_time_s"] == 100.5
    assert report["tracks"][0]["state_time_s"] == sample.elapsed_time_s
    assert report["tracks"][0]["state"][0] == pytest.approx(sample.position_world_m[0])


def test_control_sample_mandatory_fields_and_stp_conversion_preserve_raw_provenance():
    assert {item.name for item in fields(ControlSample)} == {
        "elapsed_time_s",
        "position_world_m",
        "velocity_world_mps",
        "quaternion_xyzw",
        "rotation_convention",
        "angular_velocity_world_rad_s",
        "target_position_world_m",
        "mass_kg",
        "inertia_body_kg_m2",
        "fuel_used_kg",
        "damage_j",
        "frame",
        "source_kind",
    }
    state = numeric_state(passive_quaternion_phobos_to_body_xyzw=np.array([0.0, 0.0, 2.0, 2.0]))
    sample = sample_from_stp_state(state)
    assert sample.frame == STP_PHOBOS_FRAME
    assert sample.source_kind == "stp_adapter_numeric_report"
    assert sample.rotation_convention == "passive_xyzw"
    np.testing.assert_array_equal(sample.quaternion_xyzw, [0.0, 0.0, 2.0, 2.0])
    assert not sample.quaternion_xyzw.flags.writeable
    with pytest.raises(ValueError):
        sample.quaternion_xyzw.setflags(write=True)
    state.position_phobos_m[0] = 1234.0
    assert sample.position_world_m[0] == 1000.0
    with pytest.raises(FrozenInstanceError):
        sample.frame = "different frame"
    with pytest.raises(ValueError, match="actual adapter-local STPState"):
        sample_from_stp_state(sample)


def test_integer_numeric_samples_use_float_arithmetic_without_silent_coordinate_wrap():
    sample = numeric_sample(
        position_world_m=np.array([2**62, 0, 0], dtype=np.int64),
        target_position_world_m=np.array([-(2**62) - 1, 0, 0], dtype=np.int64),
    )
    assert sample.position_world_m.dtype == np.float64
    assert (sample.target_position_world_m - sample.position_world_m)[0] < 0.0


@pytest.mark.parametrize("convention", ["active_xyzw", "passive_xyzw"])
@pytest.mark.parametrize("scale", [1.0, -1.0, 1e300])
def test_portable_frame_math_uses_declared_convention_and_roundtrips(convention, scale):
    quaternion = np.array([0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)]) * scale
    body = reference_to_body([1.0, 0.0, 0.0], quaternion, convention)
    np.testing.assert_allclose(body, [0.0, -1.0, 0.0], atol=1e-15)
    np.testing.assert_allclose(body_to_reference(body, quaternion, convention), [1.0, 0.0, 0.0])


@pytest.mark.parametrize("convention", ["wxyz", "", None])
def test_missing_or_unsupported_quaternion_declaration_is_not_guessed(convention):
    with pytest.raises(ValueError, match="rotation_convention"):
        checked_sample(numeric_sample(rotation_convention=convention))


def test_checkpoint_geometry_is_declared_and_the_new_policy_remains_explicitly_opt_in():
    assert STPPolicyConfig().policy == "legacy_pd_radar"
    assert STPPolicyConfig().duration_wall_s == 30.0
    assert not STPPolicyConfig().enabled
    assert STPPolicyConfig().checkpoint_radius_m == 50.0
    assert STPPolicyConfig().checkpoint_inset_m == 1.0
    assert STPPolicyConfig().max_speed_mps == 8.0
    assert STPPolicyConfig().control_horizon_s == 0.75
    config = checkpoint_config(duration_wall_s=3600.0)
    assert (
        parse_config(json.dumps({"schema": CONFIG_SCHEMA, "schema_version": 1, **asdict(config)}))
        == config
    )
    with pytest.raises(ValueError):
        replace(config, duration_wall_s=3600.001)
    with pytest.raises(ValueError):
        replace(config, control_horizon_s=3.001)


@pytest.mark.parametrize("name", ["policy"])
@pytest.mark.parametrize("value", [True, 0, None, "unknown", ["baseline"]])
def test_policy_selection_is_not_truthiness_or_an_unknown_mode(name, value):
    with pytest.raises(ValueError):
        replace(STPPolicyConfig(), **{name: value})


@pytest.mark.parametrize("name", ["body_axis_preference", "tracking_diagnostics"])
@pytest.mark.parametrize("value", [0, 1, "true", None, np.bool_(True)])
def test_optional_modes_require_actual_explicit_booleans(name, value):
    with pytest.raises(ValueError, match="explicit boolean"):
        replace(STPPolicyConfig(), **{name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"checkpoint_radius_m": 0.0},
        {"checkpoint_inset_m": 0.0},
        {"checkpoint_inset_m": 50.0},
        {"max_speed_mps": 0.0},
        {"max_speed_mps": 1000.001},
        {"control_horizon_s": 0.0},
        {"axis_velocity_error_slack_mps": 1.001},
        {"radar_clearance_m": -1.0},
        {"radar_position_allowance_m": -1.0},
        {"radar_rate_allowance_mps": -1.0},
        {"unknown_tangential_speed_mps": -1.0},
    ],
)
def test_checkpoint_review_budgets_reject_out_of_range_settings(changes):
    with pytest.raises(ValueError):
        checkpoint_config(**changes)


def test_checkpoint_velocity_targets_an_interior_point_and_reports_own_radius_measurement():
    controller = STPCheckpointController(checkpoint_config(max_speed_mps=2.0))
    sample = numeric_sample()
    accel, angular = controller.command(sample, numeric_radar(), now_elapsed_time_s=100.1)
    assert 0.0 < accel[0] <= 0.5
    np.testing.assert_array_equal(angular, np.zeros(3))
    report = controller.report
    checkpoint = report["checkpoint"]
    np.testing.assert_array_equal(checkpoint["interior_waypoint_world_m"], [1051.0, 0.0, 0.0])
    np.testing.assert_array_equal(checkpoint["desired_velocity_world_mps"], [2.0, 0.0, 0.0])
    assert checkpoint["desired_speed_mps"] <= 2.0
    assert not checkpoint["within_configured_radius"]
    assert checkpoint["official_scoring_status"] is None
    assert report["source_freshness"]["source_kind"] == "stp_adapter_numeric_report"
    assert report["command"]["written_to_any_api"] is False
    report["model_assumptions"]["tracker"] = "changed outside controller"
    report["source_freshness"].clear()
    assert controller.report["source_freshness"]["frame"] == STP_PHOBOS_FRAME
    assert controller.report["model_assumptions"]["tracker"] != "changed outside controller"
    inside = replace(sample, elapsed_time_s=100.2, position_world_m=np.array([1060.0, 0.0, 0.0]))
    controller.command(inside, numeric_radar(source_time=10.2), now_elapsed_time_s=100.2)
    assert controller.report["checkpoint"]["within_configured_radius"]
    assert controller.report["checkpoint"]["official_scoring_status"] is None


def test_same_checkpoint_math_accepts_distinct_explicit_numeric_source_conventions():
    quaternion = np.array([0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)])
    passive = numeric_sample(quaternion_xyzw=quaternion)
    active = replace(
        passive,
        rotation_convention="active_xyzw",
        frame="arbitrary numeric local frame, not an SDK frame",
        source_kind="constructed_numeric_test",
    )
    commands = []
    for sample in (passive, active):
        controller = STPCheckpointController(checkpoint_config())
        commands.append(controller.command(sample, numeric_radar(), now_elapsed_time_s=100.1))
        assert controller.report["source_freshness"]["frame"] == sample.frame
        assert controller.report["source_freshness"]["source_kind"] == sample.source_kind
    for left, right in zip(*commands, strict=True):
        np.testing.assert_array_equal(left, right)
    assert commands[0][0][1] < 0.0


@pytest.mark.parametrize(
    "radar",
    [
        [],
        None,
        np.zeros((3, 3)),
        numeric_radar(source_time=5.0),
        numeric_radar(source_time=10.05),
        numeric_radar(diameter=-1.0),
        numeric_radar(closest_range=math.nan),
    ],
)
def test_persistent_controller_bad_radar_coasts_with_failure_reasons(radar):
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(controller.command(numeric_sample(), radar, now_elapsed_time_s=100.1))
    assert controller.report["failure_reasons"]
    assert controller.report["coast_calls"] == 1


def test_persistent_controller_missing_clock_disabled_and_source_changes_do_not_command():
    sample, radar = numeric_sample(), numeric_radar()
    controller = STPCheckpointController(STPPolicyConfig())
    assert_coast(controller.command(sample, radar, now_elapsed_time_s=100.1))
    assert "read-only" in controller.report["failure_reasons"][0]
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(controller.command(sample, radar, now_elapsed_time_s=None))
    controller.command(sample, radar, now_elapsed_time_s=100.1)
    assert_coast(
        controller.command(
            replace(sample, frame="different frame"), radar, now_elapsed_time_s=100.1
        )
    )
    assert "frame/source/convention changed" in controller.report["failure_reasons"][0]


def test_mixed_publication_age_is_configured_not_clipped_or_made_atomic():
    rows = np.concatenate([numeric_radar(source_time=10.0), numeric_radar(source_time=8.33)])
    original = rows.copy()
    rows.flags.writeable = False
    controller = STPCheckpointController(checkpoint_config(max_radar_age_s=2.5))
    accel, _ = controller.command(numeric_sample(), rows, now_elapsed_time_s=100.1)
    assert accel[0] > 0.0
    report = controller.report
    freshness = report["source_freshness"]
    assert freshness["configured_max_radar_age_s"] == 2.5
    assert freshness["observed_max_radar_age_s"] == pytest.approx(1.77)
    assert freshness["measurement_time_spread_s"] == pytest.approx(1.67)
    assert report["track_summary"] is not None
    np.testing.assert_array_equal(rows, original)
    strict = STPCheckpointController(checkpoint_config(max_radar_age_s=1.5))
    assert_coast(strict.command(numeric_sample(), rows, now_elapsed_time_s=100.1))
    assert "Stale radar" in strict.report["failure_reasons"][0]


def test_tracker_diagnostics_cannot_change_commands_or_raw_prediction_envelopes():
    sample, rows = numeric_sample(), numeric_radar()
    results, reports = [], []
    for tracking in (False, True):
        controller = STPCheckpointController(checkpoint_config(tracking_diagnostics=tracking))
        results.append(controller.command(sample, rows, now_elapsed_time_s=100.1))
        reports.append(controller.report)
    for left, right in zip(*results, strict=True):
        np.testing.assert_array_equal(left, right)
    assert reports[0]["track_summary"] == {"enabled": False}
    assert reports[0]["candidate_choice"] == reports[1]["candidate_choice"]


@pytest.mark.parametrize("linear_cap,angular_cap", [(0.5, 0.05), (0.03, 0.002), (0.0, 0.0)])
def test_persistent_commands_obey_lowered_caps_and_only_damp_spin(linear_cap, angular_cap):
    controller = STPCheckpointController(
        checkpoint_config(max_accel_mps2=linear_cap, max_angular_accel_rad_s2=angular_cap)
    )
    accel, angular = controller.command(
        numeric_sample(angular_velocity_world_rad_s=np.array([0.0, 0.0, 10.0])),
        numeric_radar(),
        now_elapsed_time_s=100.1,
    )
    assert math.hypot(*accel) <= linear_cap
    assert math.hypot(*angular) <= angular_cap
    assert angular[2] <= 0.0


def test_unrepresentable_frame_speed_still_has_a_strict_json_failure_report():
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(
        controller.command(
            numeric_sample(velocity_world_mps=np.full(3, 1.7e308)),
            numeric_radar(),
            now_elapsed_time_s=100.1,
        )
    )
    assert controller.report["failure_reasons"]


def test_body_axis_alternative_is_selected_only_as_a_checked_actual_body_command():
    sample = numeric_sample(target_position_world_m=np.array([1100.0, 0.1, 0.0]))
    controller = STPCheckpointController(
        checkpoint_config(body_axis_preference=True, control_horizon_s=0.01)
    )
    accel, _ = controller.command(sample, numeric_radar(), now_elapsed_time_s=100.1)
    assert np.count_nonzero(accel) == 1
    choice = controller.report["candidate_choice"]
    assert choice["selected_kind"] == "body_axis"
    checked = next(
        item for item in choice["candidates"] if np.array_equal(item["accel_body_mps2"], accel)
    )
    assert checked["accepted"]
    assert checked["checkpoint_stopping_margin_m"] is not None
    np.testing.assert_array_equal(checked["accel_world_mps2_checked"], accel)
    assert any(item["kind"] == "baseline" for item in choice["candidates"])


def test_axis_progress_guard_does_not_drop_the_main_correction_for_a_tiny_l1_axis():
    controller = STPCheckpointController(checkpoint_config(body_axis_preference=True))
    accel, _ = controller.command(
        numeric_sample(
            velocity_world_mps=np.array([7.9, 0.0, 0.0]),
            target_position_world_m=np.array([2000.0, 0.01, 0.0]),
        ),
        numeric_radar(),
        now_elapsed_time_s=100.1,
    )
    assert accel[0] > 0.0
    choice = controller.report["candidate_choice"]
    assert choice["effective_axis_error_slack_mps"] < 0.001
    assert choice["effective_axis_error_slack_mps"] <= 0.1 * choice["baseline_error_reduction_mps"]
    selected = next(
        item
        for item in choice["candidates"]
        if item["kind"] == choice["selected_kind"]
        and item["axis_index"] == choice["selected_axis_index"]
    )
    actual_reduction = (
        choice["current_velocity_error_mps"] - selected["predicted_velocity_error_mps"]
    )
    assert actual_reduction >= 0.9 * choice["baseline_error_reduction_mps"]


def test_unsafe_axis_projections_fall_back_to_the_actual_checked_baseline():
    # Two tiny mathematical obstacle envelopes. The diagonal command misses
    # both; each of its axis projections hits one. No physics/client is faked.
    sample = numeric_sample(
        velocity_world_mps=np.array([10.0, 10.0, 0.0]),
        target_position_world_m=np.array([1750.0, 750.0, 0.0]),
    )
    rows = []
    for relative in (np.array([30.0, 30.9, 0.0]), np.array([30.9, 30.0, 0.0])):
        length = math.hypot(*relative)
        rate = -float(relative / length @ sample.velocity_world_mps)
        rows.append(
            numeric_radar(
                relative=relative, closest_range=length - 0.05, range_rate=rate, diameter=0.1
            )[0]
        )
    controller = STPCheckpointController(
        enabled_config(
            body_axis_preference=True,
            avoidance_range_m=0.01,
            radar_clearance_m=0.0,
            radar_position_allowance_m=0.0,
            radar_rate_allowance_mps=0.0,
            unknown_tangential_speed_mps=0.0,
        )
    )
    accel, _ = controller.command(sample, rows, now_elapsed_time_s=100.0)
    np.testing.assert_allclose(accel, [0.2, 0.2, 0.0])
    choice = controller.report["candidate_choice"]
    assert choice["baseline_fallback"]
    assert choice["selected_kind"] == "baseline"
    assert all(not item["accepted"] for item in choice["candidates"] if item["kind"] == "body_axis")
    assert any(item["accepted"] for item in choice["candidates"] if item["kind"] == "baseline")


def test_checkpoint_stopping_check_applies_to_actual_candidate_end_velocity():
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(
        controller.command(
            numeric_sample(velocity_world_mps=np.array([100.0, 0.0, 0.0])),
            numeric_radar(),
            now_elapsed_time_s=100.1,
        )
    )
    report = controller.report
    attempts = report["safety_filter"]["attempts"]
    assert report["safety_filter"]["selected_source"] is None
    assert report["safety_filter"]["criteria_relaxed"] is False
    assert len(attempts) == 2
    assert all(item["checkpoint_stopping_margin_m"] < 0.0 for item in attempts)
    assert all(not item["accepted"] for item in attempts)
    assert report["candidate_choice"]["candidates"] == []


@pytest.mark.parametrize("body_axis", [False, True])
@pytest.mark.parametrize("range_m,speed_mps", [(100.0, 5.05), (150.0, 7.1)])
def test_weak_approach_pd_uses_admissible_braking_without_relaxing_reserve(
    body_axis, range_m, speed_mps
):
    # Mathematical approach states, not a replaced simulation or fabricated
    # API trajectory. Both profiles see the same pre-ranking braking filter.
    sample = numeric_sample(
        target_position_world_m=np.array([1000.0 + range_m, 0.0, 0.0]),
        velocity_world_mps=np.array([speed_mps, 0.0, 0.0]),
    )
    initial_reserve = range_m - 49.0 + 1.0 - speed_mps**2 / (2.0 * 0.25)
    assert initial_reserve > 0.0  # Correct before the next weak nominal hold fails.
    controller = STPCheckpointController(checkpoint_config(body_axis_preference=body_axis))
    accel, _ = controller.command(sample, numeric_radar(), now_elapsed_time_s=100.0)
    report = controller.report
    safety = report["safety_filter"]
    nominal, braking = safety["attempts"]
    assert safety["activated"]
    assert not safety["criteria_relaxed"]
    assert safety["selected_source"] == "bounded_velocity_braking"
    assert nominal["checkpoint_stopping_margin_m"] < 0.0
    assert not nominal["accepted"]
    assert braking["checkpoint_stopping_margin_m"] > 0.0
    assert braking["accepted"]
    assert braking["min_raw_radar_prediction_margin_m"] > 0.0
    assert -0.5 <= accel[0] < nominal["accel_body_mps2"][0] < 0.0
    assert math.hypot(*accel) <= 0.5
    np.testing.assert_array_equal(accel, braking["accel_body_mps2"])
    assert report["failure_reasons"] == []
    assert report["candidate_choice"]["selected_kind"] == "baseline"
    candidate = next(
        item for item in report["candidate_choice"]["candidates"] if item["kind"] == "baseline"
    )
    assert candidate["accepted"] and candidate["checkpoint_stopping_margin_m"] > 0.0


def test_rounding_headroom_keeps_recorded_brakes_within_actual_javascript_limit():
    # Recorded numeric command values, not a replayed observation, SDK/client or
    # substituted network/physics. Exercise the actual installed Node Math.hypot.
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the cross-language numeric contract")
    vectors = [
        [0.01649631864831007, 0.0, -0.49972779737678535],
        [0.017082962593491433, 0.0, -0.4997080871759325],
    ]
    bounded_vectors = [_command_bounded(value, 0.5).tolist() for value in vectors]
    for raw, prepared in zip(vectors, bounded_vectors, strict=True):
        assert math.hypot(*prepared) < 0.5
        np.testing.assert_allclose(prepared, raw, rtol=3e-15, atol=0.0)
    process = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(v=>Math.hypot(...v))))",
            json.dumps(bounded_vectors),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert all(value <= 0.5 for value in json.loads(process.stdout))


def test_stopping_filter_does_not_approve_a_brake_blocked_by_raw_radar():
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(
        controller.command(
            numeric_sample(velocity_world_mps=np.array([5.05, 0.0, 0.0])),
            numeric_radar(relative=(1.0, 0.0, 0.0), closest_range=0.0),
            now_elapsed_time_s=100.0,
        )
    )
    # A radar-only failure is not promoted to accepted by the stop filter.
    assert controller.report["failure_reasons"]
    assert not controller.report["safety_filter"]["attempts"][0]["accepted"]


def test_bounded_tracking_subset_never_subsamples_the_raw_control_guard():
    rows = np.repeat(numeric_radar(), 200, axis=0)
    rows[-1] = numeric_radar(relative=(1.0, 0.0, 0.0), closest_range=0.0)[0]
    original = rows.copy()
    controller = STPCheckpointController(checkpoint_config())
    assert_coast(controller.command(numeric_sample(), rows, now_elapsed_time_s=100.0))
    report = controller.report
    work = report["tracking_work"]
    assert work["raw_control_radar_rows"] == 200
    assert work["tracker_selected_rows"] == 16
    assert work["omitted_publication_rows"] == 184
    assert work["publication_subsampled"]
    assert "omitted rows may contain competing rivals" in work["association_scope"]
    assert report["safety_filter"]["attempts"][0]["radar_rows_checked"] == 200
    assert all(
        item["radar_rows_checked"] == 200 for item in report["candidate_choice"]["candidates"]
    )
    np.testing.assert_array_equal(rows, original)


def test_live_tracking_has_explicit_small_work_and_report_bounds():
    controller = STPCheckpointController(checkpoint_config())
    rows = np.repeat(numeric_radar(), 200, axis=0)
    rows[:, 0, 1] += np.arange(200) * 300.0
    rows[:, 1, 0] += np.arange(200) * 300.0
    for tick in range(4):
        elapsed = 100.0 + tick * 0.2
        rows[:, 2, 0] = elapsed - 90.0
        controller.command(numeric_sample(elapsed_time_s=elapsed), rows, now_elapsed_time_s=elapsed)
        report = controller.report
        summary = report["track_summary"]
        assert summary["diagnostic_only"] and not summary["physical_identity_proven"]
        assert summary["counts"]["active"] <= 32
        assert summary["counts"]["reported"] <= 4
        assert summary["counts"]["fingerprints"] <= 1024
        assert summary["event"]["input_rows"] == 16
        assert summary["event"]["candidate_pairs"] <= 512
        assert report["tracking_work"]["limits"]["max_rows_per_update"] == 16
        assert summary["ego_state_time_s"] == elapsed
        assert all(
            item["radar_rows_checked"] == 200 for item in report["candidate_choice"]["candidates"]
        )


def test_original_source_deadline_is_not_extended_by_candidate_or_tracker_computation():
    controller = STPCheckpointController(
        checkpoint_config(max_report_age_s=1.0, max_radar_age_s=2.5)
    )
    rows = numeric_radar(source_time=8.0)
    controller.command(numeric_sample(), rows, now_elapsed_time_s=100.1)
    report = controller.report
    assert report["decision_valid_until_elapsed_s"] == 100.5
    assert decision_is_current(report, now_elapsed_time_s=100.5)
    assert not decision_is_current(report, now_elapsed_time_s=100.500001)
    assert not decision_is_current(report, now_elapsed_time_s=100.0)
    assert not decision_is_current(report, now_elapsed_time_s=True)
    controller = STPCheckpointController(checkpoint_config(max_report_age_s=0.2))
    controller.command(numeric_sample(), numeric_radar(), now_elapsed_time_s=100.1)
    assert controller.report["decision_valid_until_elapsed_s"] == pytest.approx(100.2)


ROOT = Path(__file__).resolve().parents[1]
HANDOFF_FILES = [
    ROOT / "src/wsorb" / name
    for name in (
        "stp_api.py",
        "stp_policy.py",
        "control_contract.py",
        "maneuvers.py",
        "tracking.py",
    )
] + [ROOT / "examples/wso/stp_controller.py"]


@pytest.mark.parametrize("path", HANDOFF_FILES, ids=lambda path: path.name)
def test_handoff_subset_has_python310_syntax_and_no_native_flight_dependency(path):
    # Parser compatibility is evidence about syntax only. It does not execute a
    # different interpreter, install an SDK or certify licensed binary support.
    tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 10))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module is None or "flight" not in node.module.split(".")
        elif isinstance(node, ast.Import):
            assert all("flight" not in alias.name.split(".") for alias in node.names)


def test_direct_colleague_bundle_imports_without_loading_historical_package_or_sdk():
    script = """
import sys
sys.path.insert(0, sys.argv[1])
import control_contract, stp_api, stp_policy, maneuvers, tracking
assert 'wsorb' not in sys.modules
assert 'flight' not in sys.modules
assert 'spaceteams' not in sys.modules
assert not stp_policy.STPPolicyConfig().enabled
print(sys.version.split()[0])
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(ROOT / "src/wsorb")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == sys.version.split()[0]


def _method_ast(tree, name):
    return next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _calls(node):
    return [ast.unparse(item) for item in ast.walk(node) if isinstance(item, ast.Call)]


def test_source_contract_keeps_pinned_bootstrap_frames_units_and_authentic_writes():
    tree = ast.parse((ROOT / "src/wsorb/stp_api.py").read_text(encoding="utf-8"))
    assert OFFICIAL_SOLUTION_COMMIT == "8545c085426a71c39cbe1d544a844b3edb757007"
    assert OFFICIAL_SIM_COMMIT == "c1e3fc8f9ee3b189bba64d8bd87818a8003639cc"
    bootstrap = _calls(_method_ast(tree, "connect_from_argv"))
    assert "importlib.import_module('spaceteams')" in bootstrap
    assert "st.connect_to_sim(sys.argv if argv is None else list(argv))" in bootstrap
    writes = _calls(_method_ast(tree, "SetCommand"))
    assert (
        "self.shuttle.SetParam(self._st.VarType.doubleV3, "
        "['JetSelection', 'CommandedAccel'], accel)" in writes
    )
    assert (
        "self.shuttle.SetParam(self._st.VarType.doubleV3, "
        "['JetSelection', 'CommandedAngAccel'], angular_accel)" in writes
    )
    reads = _calls(_method_ast(tree, "read_state"))
    for call in (
        "loc_vel_acc.loc_WRT_ExprIn(self.phobosFrame)",
        "loc_vel_acc.vel_WRT_ExprIn(self.phobosFrame)",
        "self.shuttle.getRotation().Quat_WRT(self.phobosFrame)",
        "self.shuttle.getAngVelocity().WRT_ExprIn(self.phobosFrame)",
        "self.shuttle.getMass()",
        "self.shuttle.getInertiaTensor(self.shuttleFrame)",
        "self.shuttle.GetParam(self._st.VarType.double, ['Stats', 'FuelUsed'])",
        "self.shuttle.GetParam(self._st.VarType.double, ['Stats', 'Damage'])",
    ):
        assert call in reads
    radar = _calls(_method_ast(tree, "GetNearestDebrisLocations"))
    assert (
        "self.shuttle.GetParamArray(self._st.VarType.doubleM3x3, "
        "['Radar', 'SortedDebrisData'])" in radar
    )
    probe_calls = _calls(_method_ast(tree, "read_only_probe"))
    assert not any("SetCommand" in call or ".SetParam" in call for call in probe_calls)
    assert "platform.python_version()" in probe_calls
    assert "getattr(self._st, '__version__', None)" in probe_calls
    assert "version('spaceteams')" in probe_calls
    camera_calls = _calls(_method_ast(tree, "PanTiltCamera"))
    assert "np.deg2rad(yaw_deg)" in camera_calls
    assert "np.deg2rad(pitch_deg)" in camera_calls
    assert not any("SetCommand" in call for call in _calls(_method_ast(tree, "close")))


def test_example_probe_returns_before_enabled_finally_and_rechecks_real_clock_after_compute():
    tree = ast.parse((ROOT / "examples/wso/stp_controller.py").read_text(encoding="utf-8"))
    main = _method_ast(tree, "main")
    context = next(node for node in main.body if isinstance(node, ast.With))
    disabled = context.body[0]
    assert isinstance(disabled, ast.If) and ast.unparse(disabled.test) == "not config.enabled"
    assert isinstance(disabled.body[-1], ast.Return)
    assert any("client.read_only_probe" in call for call in _calls(disabled))
    assert not any("SetCommand" in call for call in _calls(disabled))
    enabled_try = next(node for node in context.body if isinstance(node, ast.Try))
    loop = next(node for node in enabled_try.body if isinstance(node, ast.While))
    statements = [ast.unparse(node) for node in loop.body]
    compute = next(i for i, value in enumerate(statements) if "controller.command(" in value)
    clock = statements.index("fresh_now = client.elapsed_time_s()")
    guard = next(i for i, value in enumerate(statements) if "decision_is_current(" in value)
    write = statements.index("client.SetCommand(accel, angular_accel)")
    assert compute < clock < guard < write
    assert any(isinstance(node, ast.Break) for node in ast.walk(loop.body[guard]))
    assert any(
        call == "client.SetCommand(np.zeros(3), np.zeros(3))"
        for node in enabled_try.finalbody
        for call in _calls(node)
    )
    assert not any("parse_args" in call or "wsorb.cli" in call for call in _calls(main))
