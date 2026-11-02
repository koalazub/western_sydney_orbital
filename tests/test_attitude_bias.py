"""Tests for the optional attitude-bias design aid; not flight evidence."""

from __future__ import annotations

import math

import numpy as np
import pytest

from wsorb.flight.attitude_bias import (
    ACTIVE_XYZW,
    REFERENCE_FRAME,
    AttitudeBiasPlan,
    AttitudeBiasRequest,
    BiasReason,
    BoundedAngularRequest,
    SlewProfile,
    bounded_angular_request,
    plan_attitude_bias,
)

IDENTITY = (0.0, 0.0, 0.0, 1.0)
BODY_X = np.array((1.0, 0.0, 0.0))
BODY_Y = np.array((0.0, 1.0, 0.0))
NEGATIVE_X = np.array((-1.0, 0.0, 0.0))


def make_request(
    target_direction: tuple[float, float, float],
    *,
    rotation_xyzw: tuple[float, ...] = IDENTITY,
    candidate_directions: tuple[tuple[float, float, float], ...] = (),
    permit_bias: bool = True,
    rotation_convention: str = ACTIVE_XYZW,
    frame: str = REFERENCE_FRAME,
    alignment_tolerance_rad: float = 1.0e-9,
) -> AttitudeBiasRequest:
    return AttitudeBiasRequest(
        rotation_xyzw=rotation_xyzw,
        rotation_convention=rotation_convention,
        frame=frame,
        target_direction=target_direction,
        candidate_directions=candidate_directions,
        permit_bias=permit_bias,
        alignment_tolerance_rad=alignment_tolerance_rad,
    )


def rotated_reference(vector: np.ndarray, quaternion: tuple[float, ...]) -> np.ndarray:
    """Apply the explicit active Hamilton rotation R(q) used by the module."""
    x, y, z, w = quaternion
    rotation = np.array(
        (
            (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)),
            (2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)),
            (2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)),
        )
    )
    return rotation @ vector


def test_identity_attitude_and_plus_x_target_need_no_rotation() -> None:
    plan = plan_attitude_bias(make_request((1.0, 0.0, 0.0)))

    assert plan.reason is BiasReason.ALIGNED
    assert plan.rotation_required is False
    assert plan.angle_rad == 0.0
    assert plan.axis_reference == (0.0, 0.0, 0.0)
    assert plan.delta_quaternion_xyzw == pytest.approx(IDENTITY)
    assert plan.current_body_x_reference == pytest.approx(tuple(BODY_X))
    assert plan.selected_direction_reference == pytest.approx(tuple(BODY_X))


def test_ninety_degree_bias_is_shortest_arc_about_plus_z() -> None:
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0)))

    assert plan.reason is BiasReason.BIASED
    assert plan.rotation_required is True
    assert plan.angle_rad == pytest.approx(math.pi / 2.0)
    assert plan.axis_reference == pytest.approx((0.0, 0.0, 1.0))
    assert plan.delta_quaternion_xyzw == pytest.approx((0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)))
    np.testing.assert_allclose(
        rotated_reference(BODY_X, plan.delta_quaternion_xyzw),
        BODY_Y,
        atol=1.0e-15,
    )


def test_one_hundred_eighty_degrees_uses_a_stable_perpendicular_axis() -> None:
    request = make_request((float(NEGATIVE_X[0]), float(NEGATIVE_X[1]), float(NEGATIVE_X[2])))
    plan = plan_attitude_bias(request)

    assert plan.reason is BiasReason.BIASED
    assert plan.angle_rad == pytest.approx(math.pi)
    assert plan.axis_reference == pytest.approx((0.0, 0.0, 1.0))
    assert plan.delta_quaternion_xyzw[2] == pytest.approx(1.0)
    assert plan.delta_quaternion_xyzw[3] == pytest.approx(0.0, abs=1.0e-15)
    np.testing.assert_allclose(
        rotated_reference(BODY_X, plan.delta_quaternion_xyzw),
        NEGATIVE_X,
        atol=1.0e-15,
    )
    assert plan_attitude_bias(request) == plan


def test_near_aligned_target_respects_the_caller_tolerance() -> None:
    offset = 5.0e-10
    target = (math.cos(offset), math.sin(offset), 0.0)

    aligned = plan_attitude_bias(make_request(target, alignment_tolerance_rad=1.0e-9))
    assert aligned.reason is BiasReason.ALIGNED
    assert aligned.rotation_required is False
    assert aligned.angle_rad == 0.0

    biased = plan_attitude_bias(make_request(target, alignment_tolerance_rad=0.0))
    assert biased.reason is BiasReason.BIASED
    assert biased.angle_rad == pytest.approx(offset)


def test_bias_not_permitted_returns_an_explicit_no_rotation_plan() -> None:
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0), permit_bias=False))

    assert plan.reason is BiasReason.NOT_PERMITTED
    assert plan.rotation_required is False
    assert plan.angle_rad == 0.0
    assert plan.axis_reference == (0.0, 0.0, 0.0)
    assert plan.delta_quaternion_xyzw == pytest.approx(IDENTITY)
    request = bounded_angular_request(
        plan,
        max_angular_rate_rad_s=1.0,
        max_angular_acceleration_rad_s2=1.0,
    )
    assert request.profile is SlewProfile.NONE
    assert request.angular_rate_rad_s == (0.0, 0.0, 0.0)
    assert request.angular_acceleration_rad_s2 == (0.0, 0.0, 0.0)
    assert request.duration_s == 0.0


def test_current_rotation_is_used_and_scaled_quaternions_are_normalized() -> None:
    quarter_turn_z = (0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5))
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0), rotation_xyzw=quarter_turn_z))
    assert plan.reason is BiasReason.ALIGNED
    assert plan.current_body_x_reference == pytest.approx((0.0, 1.0, 0.0))

    scaled = plan_attitude_bias(make_request((1.0, 0.0, 0.0), rotation_xyzw=(0.0, 0.0, 0.0, -9.0)))
    assert scaled.reason is BiasReason.ALIGNED


def test_candidates_choose_the_smallest_bias_angle_then_caller_order() -> None:
    # The target is ignored when a candidate set is supplied.
    plan = plan_attitude_bias(
        make_request(
            (0.0, 0.0, 1.0),
            candidate_directions=((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        )
    )
    assert plan.selected_direction_reference == pytest.approx((0.0, 1.0, 0.0))
    assert plan.angle_rad == pytest.approx(math.pi / 2.0)

    # Equal angles retain the first candidate in caller order.
    tied = plan_attitude_bias(
        make_request(
            (1.0, 0.0, 0.0),
            candidate_directions=((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
        )
    )
    assert tied.selected_direction_reference == pytest.approx((0.0, 0.0, 1.0))
    assert tied.axis_reference == pytest.approx((0.0, -1.0, 0.0))

    # An already aligned candidate preserves the no-rotation option.
    aligned = plan_attitude_bias(
        make_request(
            (0.0, 0.0, 1.0),
            candidate_directions=((0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
        )
    )
    assert aligned.reason is BiasReason.ALIGNED
    assert aligned.selected_direction_reference == pytest.approx((1.0, 0.0, 0.0))


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"frame": "body"}, "frame"),
        ({"frame": "world"}, "frame"),
        ({"rotation_convention": "passive_xyzw"}, "rotation_convention"),
        ({"rotation_convention": "quaternion_xyzw"}, "rotation_convention"),
        ({"rotation_convention": None}, "rotation_convention"),
    ],
)
def test_frame_and_convention_must_be_explicit_and_supported(
    kwargs: dict[str, object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        make_request((1.0, 0.0, 0.0), **kwargs)


@pytest.mark.parametrize(
    "rotation_xyzw",
    [
        (0.0, 0.0, 0.0, 0.0),
        (math.nan, 0.0, 0.0, 1.0),
        (math.inf, 0.0, 0.0, 1.0),
        (0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0),
    ],
)
def test_invalid_quaternions_are_rejected(rotation_xyzw: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        make_request((1.0, 0.0, 0.0), rotation_xyzw=rotation_xyzw)


@pytest.mark.parametrize(
    "target_direction",
    [
        (0.0, 0.0, 0.0),
        (math.nan, 0.0, 0.0),
        (math.inf, 0.0, 0.0),
        (1.0, 0.0),
        (1.7e308, 1.7e308, 1.7e308),
    ],
)
def test_invalid_target_directions_are_rejected(
    target_direction: tuple[float, ...],
) -> None:
    with pytest.raises(ValueError):
        make_request(target_direction)


@pytest.mark.parametrize(
    "candidate_directions",
    [
        ((0.0, 0.0, 0.0),),
        ((math.nan, 0.0, 0.0),),
        ((0.0, math.inf, 0.0),),
        ((1.0, 0.0),),
        (1.0, 0.0, 0.0),
        "not a direction",
    ],
)
def test_invalid_candidate_directions_are_rejected(candidate_directions: object) -> None:
    with pytest.raises(ValueError):
        make_request((1.0, 0.0, 0.0), candidate_directions=candidate_directions)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"alignment_tolerance_rad": -1.0e-9}, "alignment_tolerance_rad"),
        ({"alignment_tolerance_rad": math.nan}, "alignment_tolerance_rad"),
        ({"alignment_tolerance_rad": math.pi + 0.1}, "alignment_tolerance_rad"),
        ({"permit_bias": 1}, "permit_bias"),
    ],
)
def test_invalid_bias_options_are_rejected(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        make_request((1.0, 0.0, 0.0), **kwargs)


def test_bounded_helper_returns_a_triangular_profile_inside_both_caps() -> None:
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0)))
    request = bounded_angular_request(
        plan,
        max_angular_rate_rad_s=10.0,
        max_angular_acceleration_rad_s2=1.0,
    )

    expected_peak_rate = math.sqrt(math.pi / 2.0)
    assert isinstance(request, BoundedAngularRequest)
    assert request.profile is SlewProfile.TRIANGULAR
    assert math.hypot(*request.angular_rate_rad_s) == pytest.approx(expected_peak_rate)
    assert math.hypot(*request.angular_acceleration_rad_s2) == pytest.approx(1.0)
    assert request.duration_s == pytest.approx(2.0 * expected_peak_rate)
    assert math.hypot(*request.angular_rate_rad_s) <= 10.0
    assert math.hypot(*request.angular_acceleration_rad_s2) <= 1.0
    assert request.angular_rate_rad_s[0] == 0.0
    assert request.angular_rate_rad_s[1] == 0.0
    assert request.angular_rate_rad_s[2] > 0.0
    assert request.angular_acceleration_rad_s2[2] > 0.0


def test_bounded_helper_returns_a_trapezoidal_profile_inside_both_caps() -> None:
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0)))
    request = bounded_angular_request(
        plan,
        max_angular_rate_rad_s=0.5,
        max_angular_acceleration_rad_s2=2.0,
    )

    expected_duration = (math.pi / 2.0) / 0.5 + 0.5 / 2.0
    assert request.profile is SlewProfile.TRAPEZOIDAL
    assert math.hypot(*request.angular_rate_rad_s) == pytest.approx(0.5, rel=1.0e-12)
    assert math.hypot(*request.angular_acceleration_rad_s2) == pytest.approx(2.0, rel=1.0e-12)
    assert request.duration_s == pytest.approx(expected_duration)
    assert math.hypot(*request.angular_rate_rad_s) <= 0.5
    assert math.hypot(*request.angular_acceleration_rad_s2) <= 2.0


def test_bounded_helper_returns_no_slew_for_an_aligned_plan() -> None:
    plan = plan_attitude_bias(make_request((1.0, 0.0, 0.0)))
    request = bounded_angular_request(
        plan,
        max_angular_rate_rad_s=1.0,
        max_angular_acceleration_rad_s2=1.0,
    )

    assert request.profile is SlewProfile.NONE
    assert request.angular_rate_rad_s == (0.0, 0.0, 0.0)
    assert request.angular_acceleration_rad_s2 == (0.0, 0.0, 0.0)
    assert request.duration_s == 0.0


@pytest.mark.parametrize(
    ("rate_cap", "acceleration_cap"),
    [
        (0.0, 1.0),
        (1.0, 0.0),
        (-1.0, 1.0),
        (1.0, -1.0),
        (math.nan, 1.0),
        (1.0, math.inf),
    ],
)
def test_bounded_helper_rejects_invalid_caps(rate_cap: float, acceleration_cap: float) -> None:
    plan = plan_attitude_bias(make_request((0.0, 1.0, 0.0)))
    with pytest.raises(ValueError):
        bounded_angular_request(
            plan,
            max_angular_rate_rad_s=rate_cap,
            max_angular_acceleration_rad_s2=acceleration_cap,
        )


def test_bounded_helper_requires_its_plan_type() -> None:
    with pytest.raises(ValueError, match="AttitudeBiasPlan"):
        bounded_angular_request(
            object(),  # type: ignore[arg-type]
            max_angular_rate_rad_s=1.0,
            max_angular_acceleration_rad_s2=1.0,
        )


def test_plan_rejects_a_biased_result_without_a_rotation() -> None:
    with pytest.raises(ValueError, match="BIASED"):
        AttitudeBiasPlan(
            reason=BiasReason.BIASED,
            selected_direction_reference=(0.0, 1.0, 0.0),
            current_body_x_reference=(1.0, 0.0, 0.0),
            angle_rad=0.0,
            axis_reference=(0.0, 0.0, 1.0),
            delta_quaternion_xyzw=IDENTITY,
        )
