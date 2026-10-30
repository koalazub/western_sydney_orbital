"""Stateless example commands. Calling these functions never applies thrust."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import numpy.typing as npt

from ._validation import field, mapping, point, scalar, vector
from .config import PlannerConfig
from .prediction import checked_radar

MAX_ACCEL_MPS2 = 0.5
MAX_ANGULAR_ACCEL_RAD_S2 = 0.05


def reference_to_body(
    value: npt.ArrayLike, rotation_xyzw: npt.ArrayLike, convention: str
) -> npt.NDArray[np.float64]:
    """Apply an explicitly directed Hamilton quaternion coordinate transform.

    For normalized q = [v, w], define
    R(q) = (w² - v·v)I + 2vvᵀ + 2w[v]×, with [v]×a = v × a.

    ``active_xyzw`` means the local body's active body-to-reference rotation;
    reference components become body components through R(q)ᵀ.
    The pinned official JetSelectionActuator.Quat_to_DCM helper defines the
    passive coordinate matrix with -2w[v]×, also R(q)ᵀ. Preserve the SDK's raw
    Hamilton components; do not conjugate them merely because it says passive.

    This defines the numeric boundary. It neither relabels a local quaternion
    as an SDK reading nor verifies a licensed runtime's convention or frame.
    """
    reference = vector(value, "reference vector")
    quaternion = vector(rotation_xyzw, "rotation", length=4)
    if convention not in ("active_xyzw", "passive_xyzw"):
        raise ValueError("rotation_convention must be active_xyzw or passive_xyzw")
    scale = float(max(abs(quaternion)))
    if scale == 0.0:
        raise ValueError("rotation must be a nonzero quaternion")
    quaternion /= scale
    quaternion /= math.hypot(*quaternion)
    x, y, z, w = quaternion
    rotation = np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y)],
            [2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x)],
            [2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y)],
        ]
    )
    with np.errstate(over="ignore", invalid="ignore"):
        result = rotation.T @ reference
    if not np.isfinite(result).all():
        raise ValueError("Body-frame acceleration is not representable")
    return result


def _bounded(value: npt.ArrayLike, maximum: float) -> npt.NDArray[np.float64]:
    result = vector(value, "command")
    scale = float(max(abs(result)))
    if scale == 0.0 or maximum == 0.0:
        return np.zeros(3)
    unit = result / scale
    length = math.hypot(*unit)
    # Python's correctly rounded hypot and V8's scaled hypot can differ at
    # the final bit. Keep the producer inside the unchanged transport cap,
    # including normalization/multiplication rounding. Never relax validation.
    interior = math.nextafter(maximum, 0.0, steps=4)
    if scale > interior / length or math.hypot(*result) > interior:
        result = unit / length * interior
        if math.hypot(*result) > interior:
            result = np.nextafter(result, 0.0)
    return result


def basic_command(
    observation: Mapping[str, Any],
    *,
    goal_world_m: npt.ArrayLike | None = None,
    position_gain_s2: float = 0.0012,
    velocity_gain_s: float = 0.07,
    avoidance_range_m: float = 70.0,
    avoidance_accel_mps2: float = 0.7,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Return capped body-frame m/s² and zero rad/s², with no attitude control.

    This ports the old basic position/velocity controller and the first 24
    public radar rows' short-range repulsion. It does not follow a route, model
    dynamics, predict contacts or promise collision avoidance. Empty radar
    supplies no repulsion. Present readings must be valid and at most 5.4 s old.
    All positions and radar vectors must use the same reference frame. The
    caller must explicitly align official radar and observation clock origins.
    """
    observation = mapping(observation, "observation")
    ship = mapping(field(observation, "shuttle"), "shuttle")
    position = vector(field(ship, "position"), "shuttle.position")
    velocity = vector(field(ship, "velocity"), "shuttle.velocity")
    if goal_world_m is None:
        target = mapping(field(observation, "target"), "target")
        goal_world_m = field(target, "position")
    goal = point(goal_world_m, "goal_world_m")
    position_gain_s2 = scalar(position_gain_s2, "position_gain_s2")
    velocity_gain_s = scalar(velocity_gain_s, "velocity_gain_s")
    avoidance_range_m = scalar(avoidance_range_m, "avoidance_range_m", positive=True)
    avoidance_accel_mps2 = scalar(avoidance_accel_mps2, "avoidance_accel_mps2")
    maximum = min(MAX_ACCEL_MPS2, scalar(field(observation, "max_accel_mps2"), "max_accel_mps2"))
    scalar(field(observation, "max_angular_accel_rad_s2"), "max_angular_accel_rad_s2")
    rows = checked_radar(observation, PlannerConfig().max_measurement_age_s)
    with np.errstate(over="ignore", invalid="ignore"):
        acceleration = (np.array(goal) - position) * position_gain_s2 - velocity * velocity_gain_s
        for reading in rows[:24]:
            if reading[1, 0] < avoidance_range_m:
                direction = reading[0] / math.hypot(*reading[0])
                acceleration -= (
                    direction
                    * avoidance_accel_mps2
                    * max(0.0, 1.0 - reading[1, 0] / avoidance_range_m)
                )
    acceleration = _bounded(acceleration, maximum)
    body = reference_to_body(
        acceleration,
        field(ship, "rotation"),
        field(observation, "rotation_convention"),
    )
    return _bounded(body, maximum), np.zeros(3, dtype=np.float64)


def coast_command(
    observation: Mapping[str, Any] | None = None,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Request no linear/angular acceleration. This is not braking or guidance."""
    return np.zeros(3, dtype=np.float64), np.zeros(3, dtype=np.float64)
