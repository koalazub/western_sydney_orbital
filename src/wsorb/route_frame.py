"""Fixed plotting coordinates shared by reports and public comparisons.

This is geometry, not a controller route or a clearance certificate. The basis
never follows the target after its initial anchor.
"""

import math
from collections.abc import Sequence


def route_basis(origin: Sequence[float], target: Sequence[float]) -> tuple[float, dict]:
    """Return route length and a right-handed, deterministic orthonormal basis."""
    points = []
    for value in (origin, target):
        if len(value) != 3 or any(type(cell) not in (float, int) for cell in value):
            raise ValueError("Route anchors must contain three finite real numbers")
        try:
            point = [float(cell) for cell in value]
        except OverflowError as error:
            raise ValueError("Route anchor is not representable") from error
        if not all(math.isfinite(cell) for cell in point):
            raise ValueError("Route anchors must be finite")
        points.append(point)
    start, goal = points
    delta = [b - a for a, b in zip(start, goal, strict=True)]
    length = math.hypot(*delta)
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Route anchors must have a positive finite separation")
    along = [cell / length for cell in delta]
    index = min(range(3), key=lambda axis: (abs(along[axis]), axis))
    lateral = [float(axis == index) - along[index] * along[axis] for axis in range(3)]
    size = math.hypot(*lateral)
    lateral = [cell / size for cell in lateral]
    normal = [
        along[1] * lateral[2] - along[2] * lateral[1],
        along[2] * lateral[0] - along[0] * lateral[2],
        along[0] * lateral[1] - along[1] * lateral[0],
    ]
    return length, {
        "origin_reference_m": start,
        "target_initial_reference_m": goal,
        "along_axis": along,
        "lateral_axis": lateral,
        "normal_axis": normal,
    }
