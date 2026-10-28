"""Numeric BODY-frame contracts, not SDK, collision or measured-fuel evidence."""

from __future__ import annotations

import ast
import math
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import numpy as np
import pytest

import wsorb.maneuvers as maneuvers
from wsorb.maneuvers import BodyCommandCandidate, ManeuverKind, axis_preferred_candidates


def test_exact_exports_and_candidate_fields() -> None:
    assert maneuvers.__all__ == [
        "ManeuverKind",
        "BodyCommandCandidate",
        "axis_preferred_candidates",
    ]
    assert {kind.value for kind in ManeuverKind} == {"baseline", "body_axis"}
    assert [field.name for field in fields(BodyCommandCandidate)] == [
        "accel_body_mps2",
        "kind",
        "axis_index",
        "predicted_velocity_error_mps",
        "allocation_effort_l1_mps2",
        "resultant_effort_l2_mps2",
    ]


def test_signed_body_components_and_prediction_share_the_declared_frame() -> None:
    command = np.array([0.2, -0.3, 0.1])
    velocity = np.array([1.0, 2.0, -3.0])
    target = np.array([1.1, 1.7, -2.9])
    duration = 0.5
    result = axis_preferred_candidates(
        command, velocity, target, duration, velocity_error_slack_mps=1.0
    )

    assert isinstance(result, tuple)
    assert [candidate.axis_index for candidate in result] == [2, 0, 1, None]
    for candidate in result[:-1]:
        assert candidate.kind is ManeuverKind.BODY_AXIS
        expected = np.zeros(3)
        expected[candidate.axis_index] = command[candidate.axis_index]
        np.testing.assert_array_equal(candidate.accel_body_mps2, expected)
    assert result[-1].kind is ManeuverKind.BASELINE
    np.testing.assert_array_equal(result[-1].accel_body_mps2, command)
    for candidate in result:
        expected_error = math.hypot(*(velocity + candidate.accel_body_mps2 * duration - target))
        expected_l1 = sum(abs(candidate.accel_body_mps2))
        expected_l2 = math.hypot(*candidate.accel_body_mps2)
        assert candidate.predicted_velocity_error_mps == pytest.approx(expected_error)
        assert candidate.allocation_effort_l1_mps2 == pytest.approx(expected_l1)
        assert candidate.resultant_effort_l2_mps2 == pytest.approx(expected_l2)


def test_caller_rotates_all_vectors_before_body_projection_and_errors_match() -> None:
    # A declared passive 45-degree rotation. BODY axes are not world axes.
    cosine = math.sqrt(0.5)
    body_from_world = np.array([[cosine, cosine, 0], [-cosine, cosine, 0], [0, 0, 1]])
    command_world = np.array([0.4, 0.0, 0.0])
    velocity_world = np.array([0.5, -0.2, 0.1])
    target_world = np.array([0.8, 0.1, 0.2])
    result = axis_preferred_candidates(
        body_from_world @ command_world,
        body_from_world @ velocity_world,
        body_from_world @ target_world,
        2.0,
        velocity_error_slack_mps=1.0,
    )
    assert {candidate.axis_index for candidate in result[:-1]} == {0, 1}
    for candidate in result[:-1]:
        assert np.count_nonzero(candidate.accel_body_mps2) == 1
        projected_world = body_from_world.T @ candidate.accel_body_mps2
        assert np.count_nonzero(projected_world) == 2
        expected_error = math.hypot(*(velocity_world + projected_world * 2.0 - target_world))
        assert candidate.predicted_velocity_error_mps == pytest.approx(expected_error)
    np.testing.assert_allclose(
        body_from_world.T @ result[-1].accel_body_mps2, command_world, atol=1e-16
    )


@pytest.mark.parametrize("axis", range(3))
@pytest.mark.parametrize("sign", [-1.0, 1.0])
def test_cardinal_baseline_is_preserved_without_a_duplicate_projection(axis, sign) -> None:
    command = np.zeros(3)
    command[axis] = sign * 0.4
    result = axis_preferred_candidates(command, [0, 0, 0], [0, 0, 0], 1.0)
    assert len(result) == 1
    assert result[0].kind is ManeuverKind.BASELINE
    assert result[0].axis_index is None
    np.testing.assert_array_equal(result[0].accel_body_mps2, command)
    assert result[0].allocation_effort_l1_mps2 == 0.4
    assert result[0].resultant_effort_l2_mps2 == 0.4


def test_zero_baseline_does_not_introduce_force_or_zero_axis_alternatives() -> None:
    command = np.array([0.0, -0.0, 0.0])
    result = axis_preferred_candidates(
        command, [1, 2, 3], [4, 6, 3], 2.0, previous_axis=2, velocity_error_slack_mps=100
    )
    assert len(result) == 1
    baseline = result[0]
    assert baseline.kind is ManeuverKind.BASELINE
    np.testing.assert_array_equal(baseline.accel_body_mps2, command)
    np.testing.assert_array_equal(np.signbit(baseline.accel_body_mps2), np.signbit(command))
    assert baseline.predicted_velocity_error_mps == 5.0
    assert baseline.allocation_effort_l1_mps2 == 0.0
    assert baseline.resultant_effort_l2_mps2 == 0.0


def test_tiny_and_subnormal_components_are_not_normalized_or_rounded_to_zero() -> None:
    smallest = np.nextafter(0.0, 1.0)
    command = np.array([1e-200, -2e-200, smallest])
    result = axis_preferred_candidates(
        command, [0, 0, 0], [0, 0, 0], 1.0, velocity_error_slack_mps=0.0, previous_axis=1
    )
    assert [candidate.axis_index for candidate in result] == [2, 0, 1, None]
    for candidate in result[:-1]:
        axis = candidate.axis_index
        assert candidate.accel_body_mps2[axis] == command[axis]
        assert np.count_nonzero(candidate.accel_body_mps2) == 1
        assert candidate.resultant_effort_l2_mps2 == abs(command[axis])
    assert result[-1].resultant_effort_l2_mps2 == math.hypot(*command)


def test_candidates_own_readonly_copies_and_frozen_metrics() -> None:
    original = np.array([0.1, -0.2, 0.3])
    candidate = BodyCommandCandidate(original, ManeuverKind.BASELINE, None, 1.0, 0.6, 0.4)
    original[:] = 0.0
    np.testing.assert_array_equal(candidate.accel_body_mps2, [0.1, -0.2, 0.3])
    assert candidate.accel_body_mps2.shape == (3,)
    assert candidate.accel_body_mps2.dtype == np.float64
    assert not candidate.accel_body_mps2.flags.writeable
    with pytest.raises(ValueError):
        candidate.accel_body_mps2[0] = 0.5
    with pytest.raises(ValueError):
        candidate.accel_body_mps2.setflags(write=True)
    with pytest.raises(FrozenInstanceError):
        candidate.kind = ManeuverKind.BODY_AXIS
    with pytest.raises(FrozenInstanceError):
        candidate.predicted_velocity_error_mps = 0.0


def test_ranking_does_not_mutate_or_alias_any_input() -> None:
    command = np.array([0.3, -0.2, 0.0])
    velocity = np.array([1.0, 2.0, 3.0])
    target = np.array([1.3, 1.8, 3.0])
    before = [value.copy() for value in (command, velocity, target)]
    result = axis_preferred_candidates(command, velocity, target, 1.0, velocity_error_slack_mps=0.5)
    for value, expected in zip((command, velocity, target), before, strict=True):
        np.testing.assert_array_equal(value, expected)
        for candidate in result:
            assert not np.shares_memory(value, candidate.accel_body_mps2)
    for candidate in result:
        assert not candidate.accel_body_mps2.flags.writeable
        for other in result:
            if other is not candidate:
                assert not np.shares_memory(candidate.accel_body_mps2, other.accel_body_mps2)
    command[:] = 0.0
    velocity[:] = 0.0
    target[:] = 0.0
    np.testing.assert_array_equal(result[-1].accel_body_mps2, before[0])


def test_equal_resultant_l1_l2_ratio_is_not_a_measured_fuel_comparison() -> None:
    resultant = 0.4
    component = resultant / math.sqrt(2.0)
    cardinal = axis_preferred_candidates([resultant, 0, 0], [0, 0, 0], [0, 0, 0], 1)[-1]
    diagonal = axis_preferred_candidates([component, -component, 0], [0, 0, 0], [0, 0, 0], 1)[-1]
    assert cardinal.resultant_effort_l2_mps2 == pytest.approx(resultant)
    assert diagonal.resultant_effort_l2_mps2 == pytest.approx(resultant)
    assert cardinal.allocation_effort_l1_mps2 == pytest.approx(resultant)
    assert diagonal.allocation_effort_l1_mps2 == pytest.approx(resultant * math.sqrt(2.0))


def test_equal_components_are_not_equal_resultants_or_a_universal_sqrt_two_cost() -> None:
    cardinal = axis_preferred_candidates([0.2, 0, 0], [0, 0, 0], [0, 0, 0], 1)[-1]
    diagonal = axis_preferred_candidates([0.2, 0.2, 0], [0, 0, 0], [0, 0, 0], 1)[-1]
    assert diagonal.allocation_effort_l1_mps2 == 2.0 * cardinal.allocation_effort_l1_mps2
    assert diagonal.resultant_effort_l2_mps2 == pytest.approx(
        math.sqrt(2.0) * cardinal.resultant_effort_l2_mps2
    )


@pytest.mark.parametrize(("slack", "expected_axes"), [(0.02, []), (0.3, [1]), (0.4, [0, 1])])
def test_declared_slack_bounds_projection_error_and_baseline_stays_last(slack, expected_axes):
    command = [0.3, 0.4, 0.0]
    result = axis_preferred_candidates(
        command, [0, 0, 0], command, 1.0, velocity_error_slack_mps=slack, previous_axis=0
    )
    assert [candidate.axis_index for candidate in result[:-1]] == expected_axes
    baseline = result[-1]
    assert baseline.kind is ManeuverKind.BASELINE
    assert baseline.axis_index is None
    assert baseline.predicted_velocity_error_mps == 0.0
    np.testing.assert_array_equal(baseline.accel_body_mps2, command)
    for candidate in result[:-1]:
        assert candidate.predicted_velocity_error_mps <= slack


def test_default_slack_rejects_axes_without_losing_the_baseline() -> None:
    result = axis_preferred_candidates([0.3, 0.4, 0], [0, 0, 0], [0.3, 0.4, 0], 1)
    assert len(result) == 1
    assert result[-1].kind is ManeuverKind.BASELINE


def test_effort_precedes_error_even_when_baseline_has_the_best_error() -> None:
    result = axis_preferred_candidates(
        [0.1, 0.2, 0], [0, 0, 0], [0.1, 0.2, 0], 1, velocity_error_slack_mps=0.2, previous_axis=1
    )
    assert [candidate.axis_index for candidate in result] == [0, 1, None]
    assert [candidate.predicted_velocity_error_mps for candidate in result] == [0.2, 0.1, 0.0]


def test_error_precedes_previous_axis_for_equal_effort() -> None:
    result = axis_preferred_candidates(
        [0.2, -0.2, 0], [0, 0, 0], [0.2, 0, 0], 1, velocity_error_slack_mps=0.1, previous_axis=1
    )
    assert [candidate.axis_index for candidate in result] == [0, 1, None]
    assert result[0].predicted_velocity_error_mps == 0.0
    assert result[1].predicted_velocity_error_mps == pytest.approx(math.hypot(0.2, 0.2))


@pytest.mark.parametrize(("previous", "expected"), [(None, [0, 1]), (0, [0, 1]), (1, [1, 0])])
def test_previous_axis_breaks_exact_eligible_ties(previous, expected) -> None:
    result = axis_preferred_candidates(
        [0.2, -0.2, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=previous
    )
    assert [candidate.axis_index for candidate in result[:-1]] == expected
    assert result[-1].kind is ManeuverKind.BASELINE


def test_hysteresis_is_limited_to_numerical_ties() -> None:
    rounded = np.nextafter(0.2, 1.0)
    tied = axis_preferred_candidates([0.2, rounded, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=1)
    assert [candidate.axis_index for candidate in tied[:-1]] == [1, 0]
    distinct = axis_preferred_candidates(
        [0.2, 0.2 + 1e-10, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=1
    )
    assert [candidate.axis_index for candidate in distinct[:-1]] == [0, 1]
    tiny = axis_preferred_candidates([1e-200, 2e-200, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=1)
    assert [candidate.axis_index for candidate in tiny[:-1]] == [0, 1]


def test_hysteresis_has_an_absolute_error_bound_even_at_large_velocity_errors() -> None:
    result = axis_preferred_candidates([0.2, 0.2, 0], [1e8, 0, 0], [0, 0, 0], 1e-7, previous_axis=0)
    assert [candidate.axis_index for candidate in result] == [1, 0, None]
    assert result[1].predicted_velocity_error_mps - result[0].predicted_velocity_error_mps > 1e-12


def test_an_absent_or_ineligible_previous_axis_cannot_be_restored() -> None:
    result = axis_preferred_candidates(
        [0.3, 0.4, 0], [0, 0, 0], [0.3, 0.4, 0], 1, velocity_error_slack_mps=0.3, previous_axis=0
    )
    assert [candidate.axis_index for candidate in result] == [1, None]
    absent = axis_preferred_candidates([0.2, 0.2, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=2)
    assert [candidate.axis_index for candidate in absent] == [0, 1, None]


@pytest.mark.parametrize("name", ["accel_body", "velocity_body", "target_velocity_body"])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        0.0,
        [],
        [0, 0],
        [0, 0, 0, 0],
        [[0, 0, 0]],
        [[0], [0], [0]],
        [0, [0], 0],
        [True, 0.0, 0.0],
        np.array([False, False, False]),
        np.array([0.0, np.bool_(False), 0.0], dtype=object),
        [0j, 0, 0],
        [0.0, 0.0, 1j],
        ["0", "0", "0"],
        [math.nan, 0, 0],
        [0, math.inf, 0],
        [0, 0, -math.inf],
        [10**400, 0, 0],
    ],
)
def test_invalid_vectors_are_rejected_before_coercion(name, invalid) -> None:
    arguments = {
        "accel_body": [0.1, -0.2, 0.0],
        "velocity_body": [0, 0, 0],
        "target_velocity_body": [0, 0, 0],
        "duration_s": 1.0,
    }
    arguments[name] = invalid
    with pytest.raises(ValueError, match=name):
        axis_preferred_candidates(**arguments)


@pytest.mark.parametrize("name", ["duration_s", "max_accel_mps2", "velocity_error_slack_mps"])
@pytest.mark.parametrize(
    "invalid",
    [True, False, np.bool_(False), 1j, "0.1", None, math.nan, math.inf, -math.inf, 10**400],
)
def test_scalar_inputs_must_be_finite_real_nonbooleans(name, invalid) -> None:
    arguments = {
        "accel_body": [0, 0, 0],
        "velocity_body": [0, 0, 0],
        "target_velocity_body": [0, 0, 0],
        "duration_s": 1.0,
    }
    arguments[name] = invalid
    with pytest.raises(ValueError, match=name):
        axis_preferred_candidates(**arguments)


@pytest.mark.parametrize(
    ("name", "invalid"),
    [
        ("duration_s", 0.0),
        ("duration_s", -1.0),
        ("max_accel_mps2", 0.0),
        ("max_accel_mps2", -0.1),
        ("max_accel_mps2", np.nextafter(0.5, 1.0)),
        ("velocity_error_slack_mps", -1e-300),
    ],
)
def test_duration_cap_and_slack_bounds(name, invalid) -> None:
    arguments = {"duration_s": 1.0, name: invalid}
    with pytest.raises(ValueError, match=name):
        axis_preferred_candidates([0, 0, 0], [0, 0, 0], [0, 0, 0], **arguments)


@pytest.mark.parametrize("invalid", [True, False, np.bool_(True), -1, 3, 1.0, 1j, "1", math.nan])
def test_previous_axis_is_an_optional_nonboolean_integer(invalid) -> None:
    with pytest.raises(ValueError, match="previous_axis"):
        axis_preferred_candidates([0, 0, 0], [0, 0, 0], [0, 0, 0], 1, previous_axis=invalid)


def test_numpy_real_scalars_and_integer_axis_are_supported() -> None:
    result = axis_preferred_candidates(
        np.array([0.125, 0.125, 0.0], dtype=np.float32),
        (0, 0, 0),
        np.zeros(3, dtype=np.int64),
        np.float32(1.0),
        max_accel_mps2=np.float64(0.5),
        velocity_error_slack_mps=np.int64(0),
        previous_axis=np.int64(1),
    )
    assert [candidate.axis_index for candidate in result] == [1, 0, None]
    for candidate in result:
        assert type(candidate.predicted_velocity_error_mps) is float
        assert type(candidate.allocation_effort_l1_mps2) is float
        assert type(candidate.resultant_effort_l2_mps2) is float


@pytest.mark.parametrize("command", [[0.5, 0.5, 0], [0.500001, 0, 0], [1e308, -1e308, 1e308]])
def test_over_cap_baseline_is_rejected_without_normalization(command) -> None:
    with pytest.raises(ValueError, match="exceeds max_accel_mps2"):
        axis_preferred_candidates(command, [0, 0, 0], [0, 0, 0], 1)


def test_stricter_cap_is_a_resultant_bound() -> None:
    result = axis_preferred_candidates(
        [0.12, -0.16, 0], [0, 0, 0], [0, 0, 0], 1, max_accel_mps2=0.2
    )
    assert result[-1].resultant_effort_l2_mps2 == 0.2
    with pytest.raises(ValueError, match="exceeds max_accel_mps2"):
        axis_preferred_candidates([0.16, 0.16, 0], [0, 0, 0], [0, 0, 0], 1, max_accel_mps2=0.2)
    with pytest.raises(ValueError, match="exceeds max_accel_mps2"):
        axis_preferred_candidates([1e-200, 0, 0], [0, 0, 0], [0, 0, 0], 1, max_accel_mps2=1e-201)


def test_one_ulp_cap_rounding_is_retained_without_clipping() -> None:
    command = np.array([np.nextafter(0.5, 1.0), 0, 0])
    result = axis_preferred_candidates(command, [0, 0, 0], [0, 0, 0], 1)
    assert len(result) == 1
    np.testing.assert_array_equal(result[-1].accel_body_mps2, command)
    assert result[-1].resultant_effort_l2_mps2 == command[0]


def test_large_finite_predictions_use_stable_norms_and_no_overflowing_slack_sum() -> None:
    with np.errstate(all="raise"):
        result = axis_preferred_candidates(
            [0.3, 0.4, 0], [1e308, 0, 0], [0, 0, 0], 1, velocity_error_slack_mps=1e308
        )
        long_burn = axis_preferred_candidates([0.3, 0.4, 0], [0, 0, 0], [0, 0, 0], 1e308)
    assert [candidate.axis_index for candidate in result] == [0, 1, None]
    assert all(candidate.predicted_velocity_error_mps == 1e308 for candidate in result)
    assert long_burn[-1].predicted_velocity_error_mps == pytest.approx(5e307)


def test_unrepresentable_axis_errors_do_not_remove_a_valid_baseline_fallback() -> None:
    with np.errstate(all="raise"):
        result = axis_preferred_candidates(
            [-0.3, -0.4, 0],
            [1.5e308, 1.5e308, 0],
            [0, 0, 0],
            1e308,
            velocity_error_slack_mps=sys.float_info.max,
        )
    assert len(result) == 1
    assert result[0].kind is ManeuverKind.BASELINE
    assert math.isfinite(result[0].predicted_velocity_error_mps)
    assert result[0].predicted_velocity_error_mps == pytest.approx(math.hypot(1.2e308, 1.1e308))


@pytest.mark.parametrize(
    ("command", "velocity", "target", "duration"),
    [
        ([0.5, 0, 0], [sys.float_info.max, 0, 0], [0, 0, 0], sys.float_info.max),
        ([0, 0, 0], [sys.float_info.max, 0, 0], [-sys.float_info.max, 0, 0], 1),
        ([0, 0, 0], [sys.float_info.max] * 3, [0, 0, 0], 1),
    ],
)
def test_unrepresentable_predictions_fail_cleanly_without_infinite_metrics(
    command, velocity, target, duration
) -> None:
    with np.errstate(all="raise"), pytest.raises(ValueError, match="predicted.*finite"):
        axis_preferred_candidates(command, velocity, target, duration)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("accel_body_mps2", [False, 0, 0]),
        ("kind", "baseline"),
        ("axis_index", True),
        ("axis_index", 3),
        ("predicted_velocity_error_mps", math.inf),
        ("allocation_effort_l1_mps2", -1.0),
        ("resultant_effort_l2_mps2", False),
    ],
)
def test_candidate_constructor_preserves_numeric_record_types(field, invalid) -> None:
    arguments = {
        "accel_body_mps2": [0.1, 0, 0],
        "kind": ManeuverKind.BASELINE,
        "axis_index": None,
        "predicted_velocity_error_mps": 0.1,
        "allocation_effort_l1_mps2": 0.1,
        "resultant_effort_l2_mps2": 0.1,
    }
    arguments[field] = invalid
    with pytest.raises(ValueError, match=field):
        BodyCommandCandidate(**arguments)


def test_source_parses_as_python_310() -> None:
    source = Path(maneuvers.__file__).read_text(encoding="utf-8")
    ast.parse(source, filename="maneuvers.py", feature_version=(3, 10))


def test_direct_sibling_import_needs_no_wsorb_native_flight_or_sdk_import() -> None:
    sibling_directory = Path(maneuvers.__file__).resolve().parent
    script = f"""
import sys
sys.path.insert(0, {str(sibling_directory)!r})
from maneuvers import ManeuverKind, axis_preferred_candidates
result = axis_preferred_candidates([0.2, -0.1, 0], [0, 0, 0], [0, 0, 0], 1)
assert result[-1].kind is ManeuverKind.BASELINE
assert not any(
    name.split('.')[0] in {{'wsorb', 'flight', 'spaceteams'}}
    for name in sys.modules
)
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
