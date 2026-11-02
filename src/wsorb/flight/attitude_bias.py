"""Optional initial-attitude-bias design aid from the 26 and 28 October minutes.

This module is an optional design aid, not an agreed requirement, not a flight
controller and not validated in flight. It answers one geometric question: which
shortest-arc rotation would align the shuttle's body +X axis with a caller-
supplied direction? It does not decide whether a bias is permitted, certify a
translation as safe, command thrusters, or change translation. Those decisions
remain at the caller's integration boundary.

Convention
----------
Quaternions are Hamilton ``xyzw`` and use the local active body-to-reference
convention (``active_xyzw``). The current body +X axis expressed in the reference
frame is ``R(q)[:, 0]``. The reported delta quaternion is an active rotation
expressed in the reference frame. Composing it with the current attitude and
applying the result to a vehicle belong to the caller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import scalar, vector

type Vec3 = tuple[float, float, float]
type QuatXyzw = tuple[float, float, float, float]

REFERENCE_FRAME = "reference"
ACTIVE_XYZW = "active_xyzw"
DEFAULT_ALIGNMENT_TOLERANCE_RAD = 1.0e-9

_AMBIGUITY_SINE = 1.0e-12

__all__ = [
    "ACTIVE_XYZW",
    "DEFAULT_ALIGNMENT_TOLERANCE_RAD",
    "REFERENCE_FRAME",
    "AttitudeBiasPlan",
    "AttitudeBiasRequest",
    "BiasReason",
    "BoundedAngularRequest",
    "SlewProfile",
    "bounded_angular_request",
    "plan_attitude_bias",
]


def _normalized_quaternion(value: Any) -> QuatXyzw:
    """Copy and normalize a nonzero finite Hamilton ``xyzw`` quaternion."""
    quaternion = vector(value, "rotation_xyzw", length=4)
    scale = float(np.max(np.abs(quaternion)))
    if scale == 0.0:
        raise ValueError("rotation_xyzw must be a nonzero quaternion")
    quaternion /= scale
    length = float(math.hypot(*quaternion))
    if not math.isfinite(length) or length == 0.0:
        raise ValueError("rotation_xyzw must be a finite, nonzero quaternion")
    quaternion /= length
    return (
        float(quaternion[0]),
        float(quaternion[1]),
        float(quaternion[2]),
        float(quaternion[3]),
    )


def _normalized_direction(value: Any, name: str) -> Vec3:
    """Copy and normalize a finite, nonzero three-vector."""
    direction = vector(value, name)
    length = float(math.hypot(*direction))
    if not math.isfinite(length) or length == 0.0:
        raise ValueError(f"{name} must be a finite, nonzero direction")
    return (
        float(direction[0] / length),
        float(direction[1] / length),
        float(direction[2] / length),
    )


def _candidate_directions(value: Any) -> tuple[Vec3, ...]:
    if value is None:
        return ()
    try:
        items = tuple(value)
    except TypeError as error:
        raise ValueError("candidate_directions must be a sequence of 3-vectors") from error
    return tuple(
        _normalized_direction(item, f"candidate_directions[{index}]")
        for index, item in enumerate(items)
    )


def _body_x_reference(quaternion: QuatXyzw) -> npt.NDArray[np.float64]:
    """Return ``R(q)[:, 0]`` for the active body-to-reference convention."""
    x, y, z, w = quaternion
    return np.array(
        (
            1.0 - 2.0 * (y * y + z * z),
            2.0 * (x * y + z * w),
            2.0 * (x * z - y * w),
        ),
        dtype=np.float64,
    )


def _angle_and_cross(
    left: npt.ArrayLike, right: npt.ArrayLike
) -> tuple[float, npt.NDArray[np.float64], float, float]:
    cross = np.cross(np.asarray(left), np.asarray(right))
    sine = float(math.hypot(*cross))
    cosine = float(np.clip(np.dot(np.asarray(left), np.asarray(right)), -1.0, 1.0))
    return math.atan2(sine, cosine), cross, sine, cosine


class BiasReason(Enum):
    """Why a plan requests, or declines, an initial attitude bias."""

    BIASED = "biased"
    ALIGNED = "aligned"
    NOT_PERMITTED = "not_permitted"


@dataclass(frozen=True, slots=True)
class AttitudeBiasRequest:
    """Validated inputs for one optional initial-attitude-bias calculation.

    ``target_direction`` and every candidate are directions in the declared
    reference frame. Candidate directions are assumed to have been certified
    safe by the caller; this module does not evaluate clearance, route deviation
    or fuel. When candidates are supplied, the one requiring the smallest bias
    angle from the current body +X is selected, and ties retain caller order.
    Otherwise the target direction is used.

    A bias is only proposed when ``permit_bias`` is true. This request is an
    optional design aid, not an agreed requirement, and is not validated in
    flight.
    """

    rotation_xyzw: QuatXyzw
    rotation_convention: str
    frame: str
    target_direction: Vec3
    candidate_directions: tuple[Vec3, ...] = ()
    permit_bias: bool = True
    alignment_tolerance_rad: float = DEFAULT_ALIGNMENT_TOLERANCE_RAD

    def __post_init__(self) -> None:
        if self.rotation_convention != ACTIVE_XYZW:
            raise ValueError(
                f"rotation_convention must be '{ACTIVE_XYZW}'; convert other readings "
                "at the caller boundary"
            )
        if self.frame != REFERENCE_FRAME:
            raise ValueError(
                f"frame must be '{REFERENCE_FRAME}'; this aid does not accept body-frame "
                "targets or candidates"
            )
        if not isinstance(self.permit_bias, bool):
            raise ValueError("permit_bias must be a bool")
        tolerance = scalar(
            self.alignment_tolerance_rad,
            "alignment_tolerance_rad",
            minimum=0.0,
        )
        if tolerance > math.pi:
            raise ValueError("alignment_tolerance_rad must be at most pi")
        rotation = _normalized_quaternion(self.rotation_xyzw)
        target = _normalized_direction(self.target_direction, "target_direction")
        candidates = _candidate_directions(self.candidate_directions)
        object.__setattr__(self, "rotation_xyzw", rotation)
        object.__setattr__(self, "target_direction", target)
        object.__setattr__(self, "candidate_directions", candidates)
        object.__setattr__(self, "alignment_tolerance_rad", tolerance)


@dataclass(frozen=True, slots=True)
class AttitudeBiasPlan:
    """Geometric shortest-arc bias, not an applied attitude command.

    ``axis_reference`` is a unit right-handed axis in the reference frame and
    ``angle_rad`` is the unsigned shortest-arc angle from the current body +X to
    ``selected_direction_reference``. ``delta_quaternion_xyzw`` is the matching
    active Hamilton rotation in the reference frame.

    A no-rotation plan has reason ``ALIGNED`` or ``NOT_PERMITTED``, zero angle,
    a zero axis and the identity quaternion. This is an optional design aid, not
    an agreed requirement, and is not validated in flight.
    """

    reason: BiasReason
    selected_direction_reference: Vec3
    current_body_x_reference: Vec3
    angle_rad: float
    axis_reference: Vec3
    delta_quaternion_xyzw: QuatXyzw

    def __post_init__(self) -> None:
        if not isinstance(self.reason, BiasReason):
            raise ValueError("reason must be a BiasReason")
        selected = _normalized_direction(
            self.selected_direction_reference,
            "selected_direction_reference",
        )
        current = _normalized_direction(
            self.current_body_x_reference,
            "current_body_x_reference",
        )
        angle = scalar(self.angle_rad, "angle_rad", minimum=0.0)
        if angle > math.pi:
            raise ValueError("angle_rad must be at most pi")
        if self.reason is BiasReason.BIASED:
            if angle == 0.0:
                raise ValueError("a BIASED plan must have a positive angle_rad")
            axis = _normalized_direction(self.axis_reference, "axis_reference")
            delta = _normalized_quaternion(self.delta_quaternion_xyzw)
        else:
            axis = (0.0, 0.0, 0.0)
            delta = (0.0, 0.0, 0.0, 1.0)
        object.__setattr__(self, "selected_direction_reference", selected)
        object.__setattr__(self, "current_body_x_reference", current)
        object.__setattr__(self, "angle_rad", angle)
        object.__setattr__(self, "axis_reference", axis)
        object.__setattr__(self, "delta_quaternion_xyzw", delta)

    @property
    def rotation_required(self) -> bool:
        """Whether the plan asks for a nonzero attitude change."""
        return self.reason is BiasReason.BIASED


def _select_direction(
    body_x: npt.NDArray[np.float64],
    request: AttitudeBiasRequest,
) -> Vec3:
    candidates = request.candidate_directions
    if not candidates:
        return request.target_direction
    selected = candidates[0]
    best_angle, _, _, _ = _angle_and_cross(body_x, selected)
    for candidate in candidates[1:]:
        angle, _, _, _ = _angle_and_cross(body_x, candidate)
        if angle < best_angle:
            selected = candidate
            best_angle = angle
    return selected


def _shortest_arc_axis(
    body_x: npt.NDArray[np.float64],
    cross: npt.NDArray[np.float64],
    sine: float,
    cosine: float,
) -> Vec3:
    """Choose a deterministic unit axis for the shortest arc.

    At 180 degrees the cross product vanishes. The stable fallback uses the
    reference coordinate axis with the smallest absolute component, breaking
    ties in x, y, z order. For body +X that selects +Z as the rotation axis.
    """
    if cosine < 0.0 and sine <= _AMBIGUITY_SINE:
        basis = np.zeros(3, dtype=np.float64)
        basis[int(np.argmin(np.abs(body_x)))] = 1.0
        axis = np.cross(body_x, basis)
        length = float(math.hypot(*axis))
        if not math.isfinite(length) or length == 0.0:
            raise ValueError("cannot choose a finite 180-degree rotation axis")
        axis /= length
    elif sine > 0.0:
        axis = cross / sine
    else:
        # A zero-angle request is handled before this point. Keep a defensive
        # deterministic fallback rather than dividing by zero.
        basis = np.zeros(3, dtype=np.float64)
        basis[int(np.argmin(np.abs(body_x)))] = 1.0
        axis = np.cross(body_x, basis)
        length = float(math.hypot(*axis))
        if not math.isfinite(length) or length == 0.0:
            raise ValueError("cannot choose a finite rotation axis")
        axis /= length
    return (float(axis[0]), float(axis[1]), float(axis[2]))


def _axis_angle_quaternion(axis: Vec3, angle_rad: float) -> QuatXyzw:
    half = 0.5 * angle_rad
    sine = math.sin(half)
    quaternion = np.asarray(
        (axis[0] * sine, axis[1] * sine, axis[2] * sine, math.cos(half)),
        dtype=np.float64,
    )
    length = float(math.hypot(*quaternion))
    if not math.isfinite(length) or length == 0.0:
        raise ValueError("rotation quaternion is not representable")
    quaternion /= length
    if quaternion[3] < 0.0:
        quaternion = -quaternion
    return (
        float(quaternion[0]),
        float(quaternion[1]),
        float(quaternion[2]),
        float(quaternion[3]),
    )


def _no_rotation_plan(
    reason: BiasReason,
    body_x: npt.NDArray[np.float64],
    selected: Vec3,
) -> AttitudeBiasPlan:
    return AttitudeBiasPlan(
        reason=reason,
        selected_direction_reference=selected,
        current_body_x_reference=(float(body_x[0]), float(body_x[1]), float(body_x[2])),
        angle_rad=0.0,
        axis_reference=(0.0, 0.0, 0.0),
        delta_quaternion_xyzw=(0.0, 0.0, 0.0, 1.0),
    )


def plan_attitude_bias(request: AttitudeBiasRequest) -> AttitudeBiasPlan:
    """Return a shortest-arc body +X bias toward the selected direction.

    The plan is geometric only. It never changes translation and never assumes
    that a candidate direction is safe. When ``permit_bias`` is false, or the
    current body +X is within the request tolerance, the result is an explicit
    no-rotation plan. This is an optional design aid, not an agreed requirement,
    and is not validated in flight.
    """
    if not isinstance(request, AttitudeBiasRequest):
        raise ValueError("request must be an AttitudeBiasRequest")
    body_x = _body_x_reference(request.rotation_xyzw)
    selected = _select_direction(body_x, request)
    angle, cross, sine, cosine = _angle_and_cross(body_x, selected)
    if not request.permit_bias:
        return _no_rotation_plan(BiasReason.NOT_PERMITTED, body_x, selected)
    if angle <= request.alignment_tolerance_rad:
        return _no_rotation_plan(BiasReason.ALIGNED, body_x, selected)
    axis = _shortest_arc_axis(body_x, cross, sine, cosine)
    return AttitudeBiasPlan(
        reason=BiasReason.BIASED,
        selected_direction_reference=selected,
        current_body_x_reference=(float(body_x[0]), float(body_x[1]), float(body_x[2])),
        angle_rad=angle,
        axis_reference=axis,
        delta_quaternion_xyzw=_axis_angle_quaternion(axis, angle),
    )


class SlewProfile(Enum):
    """Shape of a bounded rest-to-rest angular slew."""

    NONE = "none"
    TRIANGULAR = "triangular"
    TRAPEZOIDAL = "trapezoidal"


@dataclass(frozen=True, slots=True)
class BoundedAngularRequest:
    """Bounded angular reference derived from a geometric attitude plan.

    Both vectors lie along the plan axis. They are a rest-to-rest slew
    allocation, not a closed-loop tracking law: a caller's controller remains
    responsible for feedback, inertia and torque limits, and for leaving
    translation untouched. This is an optional design aid, not an agreed
    requirement, and is not validated in flight.
    """

    angular_rate_rad_s: Vec3
    angular_acceleration_rad_s2: Vec3
    duration_s: float
    profile: SlewProfile

    def __post_init__(self) -> None:
        if not isinstance(self.profile, SlewProfile):
            raise ValueError("profile must be a SlewProfile")
        rate = vector(self.angular_rate_rad_s, "angular_rate_rad_s")
        acceleration = vector(
            self.angular_acceleration_rad_s2,
            "angular_acceleration_rad_s2",
        )
        duration = scalar(self.duration_s, "duration_s", minimum=0.0)
        if self.profile is SlewProfile.NONE and (
            np.any(rate != 0.0) or np.any(acceleration != 0.0) or duration != 0.0
        ):
            raise ValueError("a NONE slew profile must have zero rate, acceleration and duration")
        object.__setattr__(
            self,
            "angular_rate_rad_s",
            (float(rate[0]), float(rate[1]), float(rate[2])),
        )
        object.__setattr__(
            self,
            "angular_acceleration_rad_s2",
            (float(acceleration[0]), float(acceleration[1]), float(acceleration[2])),
        )
        object.__setattr__(self, "duration_s", duration)


def _capped_axis(
    axis: npt.ArrayLike,
    magnitude: float,
    cap: float,
    name: str,
) -> Vec3:
    """Scale a unit axis without letting rounding cross the caller's cap."""
    unit = np.asarray(_normalized_direction(axis, f"{name} axis"), dtype=np.float64)
    magnitude = scalar(magnitude, f"{name} magnitude", minimum=0.0)
    if magnitude == 0.0:
        return (0.0, 0.0, 0.0)
    interior = math.nextafter(cap, 0.0, steps=4)
    magnitude = min(magnitude, interior)
    result = unit * magnitude
    if float(math.hypot(*result)) > cap:
        result = np.nextafter(result, 0.0)
    return (float(result[0]), float(result[1]), float(result[2]))


def bounded_angular_request(
    plan: AttitudeBiasPlan,
    *,
    max_angular_rate_rad_s: float,
    max_angular_acceleration_rad_s2: float,
) -> BoundedAngularRequest:
    """Convert a plan into a bounded rest-to-rest angular slew reference.

    The peak rate never exceeds ``max_angular_rate_rad_s`` and the acceleration
    magnitude never exceeds ``max_angular_acceleration_rad_s2``. A triangular
    profile peaks below the rate cap when the angle is small enough; otherwise a
    trapezoidal profile uses the rate cap for its cruise phase. No rotation
    returns zero rate and acceleration with ``SlewProfile.NONE``.

    The caller must track this reference with its own attitude controller and
    leave translation unchanged. Returning only the geometric
    :class:`AttitudeBiasPlan` is also valid when the caller already shapes its
    own angular command. This is an optional design aid, not an agreed
    requirement, and is not validated in flight.
    """
    if not isinstance(plan, AttitudeBiasPlan):
        raise ValueError("plan must be an AttitudeBiasPlan")
    rate_cap = scalar(max_angular_rate_rad_s, "max_angular_rate_rad_s", positive=True)
    acceleration_cap = scalar(
        max_angular_acceleration_rad_s2,
        "max_angular_acceleration_rad_s2",
        positive=True,
    )
    if not plan.rotation_required:
        return BoundedAngularRequest(
            angular_rate_rad_s=(0.0, 0.0, 0.0),
            angular_acceleration_rad_s2=(0.0, 0.0, 0.0),
            duration_s=0.0,
            profile=SlewProfile.NONE,
        )

    angle = scalar(plan.angle_rad, "plan.angle_rad", positive=True)
    root_angle = math.sqrt(angle)
    root_acceleration = math.sqrt(acceleration_cap)
    if root_angle <= rate_cap / root_acceleration:
        peak_rate = root_angle * root_acceleration
        duration = 2.0 * root_angle / root_acceleration
        profile = SlewProfile.TRIANGULAR
    else:
        peak_rate = rate_cap
        duration = angle / rate_cap + rate_cap / acceleration_cap
        profile = SlewProfile.TRAPEZOIDAL
    if not math.isfinite(duration) or duration < 0.0:
        raise ValueError("bounded slew duration is not representable")

    return BoundedAngularRequest(
        angular_rate_rad_s=_capped_axis(
            plan.axis_reference,
            peak_rate,
            rate_cap,
            "angular rate",
        ),
        angular_acceleration_rad_s2=_capped_axis(
            plan.axis_reference,
            acceleration_cap,
            acceleration_cap,
            "angular acceleration",
        ),
        duration_s=duration,
        profile=profile,
    )
