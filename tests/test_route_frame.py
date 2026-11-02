"""Reports and hosted plots must use the same signed spatial coordinates."""

import math

import pytest

from wsorb.route_frame import route_basis
from wsorb.sweep_analysis import _route_frame


@pytest.mark.parametrize("target", [(100, 0, 0), (0, 100, 0), (0, 0, -100), (5, 30, 7)])
def test_python_report_and_public_projection_share_the_same_basis(target):
    origin = [0.0, 0.0, 0.0]
    length, basis = route_basis(origin, target)
    report = _route_frame({"shuttle": {"position": origin}, "target": {"position": target}})
    assert report["length_m"] == length
    for axis in ("along", "lateral", "normal"):
        assert report[axis] == basis[f"{axis}_axis"]
        assert math.hypot(*report[axis]) == pytest.approx(1)
    along, lateral, normal = (report[axis] for axis in ("along", "lateral", "normal"))
    assert sum(a * b for a, b in zip(along, lateral, strict=True)) == pytest.approx(0)
    assert normal == pytest.approx(
        [
            along[1] * lateral[2] - along[2] * lateral[1],
            along[2] * lateral[0] - along[0] * lateral[2],
            along[0] * lateral[1] - along[1] * lateral[0],
        ]
    )


@pytest.mark.parametrize("target", [(0, 0, 0), (float("inf"), 0, 0), (True, 0, 0)])
def test_missing_or_unrepresentable_route_is_not_invented(target):
    with pytest.raises(ValueError):
        route_basis([0, 0, 0], target)
