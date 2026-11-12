"""Numeric checks for Ali's MPPI core."""

from __future__ import annotations

import json

import numpy as np

from wsorb.controllers._sources import load_math

core = load_math("ali_mppi")["core"]


def test_cruise_velocity_is_seven_metres_per_second_toward_the_target():
    velocity = core.cruise_velocity(np.zeros(3), np.array([100.0, 0.0, 0.0]))
    assert np.isclose(np.linalg.norm(velocity), 7.0)
    assert np.allclose(velocity, [7.0, 0.0, 0.0])


def test_burn_governor_uses_hysteresis_instead_of_micro_bursts():
    nominal = np.array([7.0, 0.0, 0.0])
    acceleration, burning = core.govern(np.zeros(3), nominal, np.zeros(3), False, 0.5)
    assert burning and np.isclose(np.linalg.norm(acceleration), 0.5)

    acceleration, burning = core.govern(np.zeros(3), nominal, np.array([6.9, 0.0, 0.0]), True, 0.5)
    assert not burning and np.allclose(acceleration, np.zeros(3))


def test_capsule_distance_uses_the_oriented_segment():
    distance = core.capsule_distance(
        np.zeros((1, 3)),
        np.array([[10.0, 0.0, 0.0]]),
        np.array([1.0, 0.0, 0.0]),
        3.0,
        1.0,
    )
    assert np.isclose(distance[0, 0], 6.0)


def test_attitude_is_bounded_and_damps_an_aligned_ship():
    aligned = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
    )
    assert np.allclose(aligned, np.zeros(3))

    turning = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
    )
    assert 0.0 < np.linalg.norm(turning) <= 0.05


def test_plan_returns_a_finite_bounded_first_control():
    state = {
        "rocks": [
            {
                "position": np.array([100.0, 0.0, 0.0]),
                "velocity": np.array([-5.0, 0.0, 0.0]),
                "diameter": 2.0,
                "max_diameter": 2.0,
                "last_seen": 0.0,
                "samples": [],
                "sigma": 0.5,
            }
        ],
        "last_control": np.zeros(3),
    }
    acceleration = core.plan(
        state,
        np.zeros(3),
        np.zeros(3),
        np.array([1000.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        3.0,
        0.5,
        0.1,
        0.0,
        0.0,
        3388.7,
        0.0,
    )
    assert np.isfinite(acceleration).all()
    assert np.linalg.norm(acceleration) <= 0.5 + 1e-9


def test_attitude_diagnostics_show_the_pd_terms_and_saturation():
    trace = {}
    command = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        trace,
    )
    assert trace["angle_deg"] == 90.0
    assert trace["p_term_rad_s2"] > 0.0
    assert trace["d_term_rad_s2"] == 0.0
    assert trace["saturated"] is True
    assert np.allclose(trace["command_rad_s2"], command)

    damping = {}
    damped = core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, -0.07, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        0.05,
        0.1,
        damping,
    )
    assert damping["deadband"] is False
    assert damping["p_term_rad_s2"] == 0.0
    assert damping["d_term_rad_s2"] < 0.0
    assert np.allclose(damped, [0.0, 0.049, 0.0])


def test_attitude_telemetry_is_finite_json_and_flags_coast_drift():
    trace = {}
    core.attitude(
        np.array([1.0, 0.0, 0.0]),
        np.zeros(3),
        np.array([0.0, 1.0, 0.0]),
        0.05,
        0.1,
        trace,
    )
    record = core.telemetry_record(
        1.0,
        0.1,
        "coast",
        "off",
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([0.0, 0.0, 0.0]),
        np.array([0.0, 0.0, 0.0]),
        np.zeros(3),
        trace,
        1.0,
        6.0,
        100.0,
        2,
        1.0,
        0.0,
    )
    assert record["schema"] == "wsorb.mppi_attitude"
    assert record["line"].startswith("MPPI")
    assert record["burn_align_rad"] is None
    assert record["warning"] is None or isinstance(record["warning"], str)
    json.dumps(record, allow_nan=False)
    assert (
        core.attitude_warning(
            {
                "mode": "coast",
                "target_align_rad": np.radians(8.0),
                "rate_rad_s": 0.03,
            }
        )
        == "coast_misaligned_with_residual_rate"
    )
