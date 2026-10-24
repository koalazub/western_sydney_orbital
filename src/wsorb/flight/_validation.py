"""Numeric checks shared by the pure flight calculations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Integral, Real
from typing import Any

import numpy as np
import numpy.typing as npt

type Vec3 = tuple[float, float, float]


def mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def field(value: Mapping[str, Any], key: str) -> Any:
    if key not in value:
        raise ValueError(f"Missing public observation field: {key}")
    return value[key]


def scalar(value: Any, name: str, *, minimum: float = 0.0, positive: bool = False) -> float:
    message = f"{name} must be finite and {'positive' if positive else f'>= {minimum}'}"
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(message)
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(message) from error
    if not math.isfinite(number) or number < minimum or (positive and number <= 0.0):
        raise ValueError(message)
    return number


def count(value: Any, name: str, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    if (
        isinstance(value, (bool, np.bool_))
        or not isinstance(value, Integral)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return int(value)


def real_array(value: Any, name: str) -> npt.NDArray[np.float64]:
    message = f"{name} must contain finite real numbers"
    try:
        array = np.asarray(value)
        if array.dtype.kind not in "iuf":
            raise ValueError(message)
        with np.errstate(over="ignore", invalid="ignore"):
            array = np.array(array, dtype=np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(message) from error
    if not np.isfinite(array).all():
        raise ValueError(message)
    return array


def vector(value: Any, name: str, *, length: int = 3) -> npt.NDArray[np.float64]:
    array = real_array(value, name)
    if array.shape != (length,):
        raise ValueError(f"{name} must have shape ({length},)")
    return array


def point(value: Any, name: str) -> Vec3:
    x, y, z = vector(value, name)
    return float(x), float(y), float(z)


def difference(left: Vec3, right: Vec3) -> Vec3:
    result = tuple(a - b for a, b in zip(left, right, strict=True))
    if not all(math.isfinite(component) for component in result):
        raise ValueError("Coordinate subtraction is not representable")
    return result


def distance(left: Vec3, right: Vec3) -> float:
    result = math.hypot(*difference(left, right))
    if not math.isfinite(result):
        raise ValueError("Distance is not representable")
    return result
