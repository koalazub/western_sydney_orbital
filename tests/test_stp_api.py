"""Pure numeric/config contracts, not SDK, sensor or flight validation.

STPState instances and radar arrays below are constructed mathematical inputs.
They are not captured observations, SDK substitutes or simulated trajectories.
No fake client, monkeypatched SDK, network, physics or graphics are used.
"""

import json
import math
import sys
from dataclasses import FrozenInstanceError, asdict, fields, replace
from importlib.util import find_spec

import numpy as np
import pytest

from wsorb.stp_api import STPState, _finite_real_vector, connect_from_argv
from wsorb.stp_policy import (
    CONFIG_FILE_ENV,
    CONFIG_JSON_ENV,
    CONFIG_SCHEMA,
    MAX_CONFIG_BYTES,
    STPPolicyConfig,
    decide_command,
    load_config_from_environment,
    parse_config,
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
    "name", [item.name for item in fields(STPPolicyConfig) if item.name != "enabled"]
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
