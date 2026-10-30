"""Constructed anonymous-scan arithmetic, not native observations or flight proof."""

from __future__ import annotations

import numpy as np
import pytest

from test_local_motion import history_scan
from wsorb.flight import ResilienceConfig
from wsorb.flight.estimation import NavigationSample, StateEstimator
from wsorb.flight.local_motion import RadarMotionHistory, temporal_obstacles


def add_scan(history, estimator, config, stamp, positions):
    scan = history_scan(stamp, positions)
    estimator.correct(NavigationSample.from_public(stamp, scan["shuttle"]))
    raw = temporal_obstacles(scan, estimator, config)
    hypothesis, count = history.refine(scan, estimator, config, raw)
    return scan, raw, hypothesis, count


def test_two_scans_and_duplicate_polling_cannot_condition_a_safety_forecast():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    add_scan(history, estimator, config, 10.0, [(100.0, 300.0, 0.0)])
    scan, raw, _, count = add_scan(history, estimator, config, 15.0, [(200.0, 300.0, 0.0)])
    assert count == 1
    assert history.conditioned_obstacles is None and history.corroborated_rows == 0
    history.refine(scan, estimator, config, raw)
    assert len(history.frames) == 2
    assert history.conditioned_obstacles is None and history.corroborated_rows == 0


def test_third_distinct_consistent_scan_adds_velocity_without_removing_any_allowance():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    add_scan(history, estimator, config, 10.0, [(100.0, 300.0, 0.0)])
    add_scan(history, estimator, config, 15.0, [(200.0, 300.0, 0.0)])
    _, raw, _, _ = add_scan(history, estimator, config, 16.0, [(220.0, 300.0, 0.0)])
    conditioned = history.conditioned_obstacles
    assert conditioned is not None and history.corroborated_rows == 1
    assert len(conditioned.radii_m) == len(raw.radii_m) == 4
    np.testing.assert_allclose(conditioned.velocities_mps[3], (20.0, 0.0, 0.0), atol=1e-12)
    assert np.all(conditioned.radii_m >= raw.radii_m)
    assert np.all(conditioned.growth_mps >= raw.growth_mps)
    assert conditioned.growth_mps[3] > raw.growth_mps[3]
    np.testing.assert_array_equal(conditioned.growth_mps2, raw.growth_mps2)
    for name in ("centers_m", "velocities_mps", "radii_m", "growth_mps", "growth_mps2"):
        np.testing.assert_array_equal(getattr(conditioned, name)[:3], getattr(raw, name)[:3])


def test_well_spaced_genuine_corroboration_is_not_hidden_by_an_ambiguous_latest_scan():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    for stamp, x in ((10.0, 0.0), (12.5, 50.0), (15.0, 100.0), (17.5, 150.0)):
        add_scan(history, estimator, config, stamp, [(x, 300.0, 0.0)])
    add_scan(history, estimator, config, 19.9, [(198.0, 300.0, 0.0), (199.0, 301.0, 0.0)])
    assert not np.any(history.frames[-1][7])
    _, raw, _, _ = add_scan(history, estimator, config, 20.0, [(200.0, 300.0, 0.0)])
    conditioned = history.conditioned_obstacles
    assert history.trusted_motion_rows.tolist() == [True]
    assert conditioned is not None and history.corroborated_rows == 1
    np.testing.assert_allclose(conditioned.velocities_mps[3], (20.0, 0.0, 0.0), atol=1e-12)
    assert np.all(conditioned.radii_m >= raw.radii_m)
    assert np.all(conditioned.growth_mps >= raw.growth_mps)
    np.testing.assert_array_equal(conditioned.growth_mps2, raw.growth_mps2)
    assert len(conditioned.radii_m) == len(raw.radii_m) == 4
    assert [frame[0] for frame in history.frames] == [10.0, 12.5, 15.0, 17.5, 19.9, 20.0]


def test_a_midpoint_without_motion_advice_cannot_hide_an_eligible_third_source():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    for stamp, x in ((10.0, 100.0), (11.5, 130.0), (13.0, 160.0)):
        add_scan(history, estimator, config, stamp, [(x, 300.0, 0.0)])
    assert not np.any(history.frames[1][7])
    assert np.any(history.frames[-1][7])
    _, raw, _, _ = add_scan(history, estimator, config, 13.1, [(162.0, 300.0, 0.0)])
    conditioned = history.conditioned_obstacles
    assert conditioned is not None and history.corroborated_rows == 1
    np.testing.assert_allclose(conditioned.velocities_mps[3], (20.0, 0.0, 0.0), atol=1e-12)
    assert np.all(conditioned.radii_m >= raw.radii_m)
    assert np.all(conditioned.growth_mps >= raw.growth_mps)
    np.testing.assert_array_equal(conditioned.growth_mps2, raw.growth_mps2)


@pytest.mark.parametrize("failure", ["ambiguity", "range_rate"])
def test_third_scan_must_pass_current_association_and_range_rate_before_conditioning(failure):
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    add_scan(history, estimator, config, 10.0, [(100.0, 300.0, 0.0)])
    add_scan(history, estimator, config, 15.0, [(200.0, 300.0, 0.0)])
    positions = [(220.0, 300.0, 0.0)]
    if failure == "ambiguity":
        positions.append((223.0, 300.0, 0.0))
    scan = history_scan(16.0, positions)
    if failure == "range_rate":
        scan["radar"][0][1][1] += 100.0
    estimator.correct(NavigationSample.from_public(16.0, scan["shuttle"]))
    raw = temporal_obstacles(scan, estimator, config)
    history.refine(scan, estimator, config, raw)
    assert history.conditioned_obstacles is None and history.corroborated_rows == 0
    assert len(raw.radii_m) == len(positions) + 3


def test_radial_motion_can_be_corroborated_without_inventing_a_tangential_component():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    add_scan(history, estimator, config, 10.0, [(-200.0, 0.0, 0.0)])
    add_scan(history, estimator, config, 15.0, [(-100.0, 0.0, 0.0)])
    _, raw, _, count = add_scan(history, estimator, config, 16.0, [(-80.0, 0.0, 0.0)])
    assert count == 0
    assert history.trusted_motion_rows.tolist() == [True]
    assert history.conditioned_obstacles is None
    np.testing.assert_array_equal(raw.velocities_mps[3], (20.0, 0.0, 0.0))


def test_consistent_reverse_motion_is_not_rejected_by_the_inlet_birth_velocity():
    config = ResilienceConfig()
    estimator, history = StateEstimator(config), RadarMotionHistory()
    for stamp, position in (
        (10.0, (300.0, 300.0, 0.0)),
        (15.0, (200.0, 300.0, 0.0)),
        (16.0, (180.0, 300.0, 0.0)),
    ):
        scan = history_scan(stamp, [position])
        direction = np.asarray(position) / np.linalg.norm(position)
        scan["radar"][0][1][1] = float(np.dot(direction, (-20.0, 0.0, 0.0)))
        estimator.correct(NavigationSample.from_public(stamp, scan["shuttle"]))
        raw = temporal_obstacles(scan, estimator, config)
        history.refine(scan, estimator, config, raw)
    assert history.trusted_motion_rows.tolist() == [True]
    assert history.corroborated_rows == 1
    np.testing.assert_allclose(
        history.conditioned_obstacles.velocities_mps[3], (-20.0, 0.0, 0.0), atol=1e-12
    )
    assert np.all(history.conditioned_obstacles.radii_m >= raw.radii_m)
    assert np.all(history.conditioned_obstacles.growth_mps >= raw.growth_mps)
    np.testing.assert_array_equal(history.conditioned_obstacles.growth_mps2, raw.growth_mps2)
