"""Constructed guidance arithmetic, never native sensors or arrival evidence."""

from __future__ import annotations

import numpy as np
import pytest

from wsorb.flight import local_motion


def test_checkpoint_region_entry_preserves_existing_closing_motion_without_inventing_it():
    target = np.array((10000.0, 0.0, 0.0))
    position = np.array((10000.0, 82.0, 0.0))
    requested = 0.0012 * (82.0 - 49.0) / 0.07
    moving = local_motion.checkpoint_entry_speed(
        position, target, np.array((0.0, -2.0, 0.0)), requested, 8.0, 0.49, 32.0
    )
    assert moving == pytest.approx(2.0)
    assert (
        local_motion.checkpoint_entry_speed(
            position, target, np.zeros(3), requested, 8.0, 0.49, 32.0
        )
        == requested
    )
    assert (
        local_motion.checkpoint_entry_speed(
            position, target, np.array((0.0, 2.0, 0.0)), requested, 8.0, 0.49, 32.0
        )
        == requested
    )
    # Keep the original near-entry position response for a resting ship.
    near = np.array((10000.0, 49.1, 0.0))
    small = 0.0012 * 0.1 / 0.07
    assert (
        local_motion.checkpoint_entry_speed(near, target, np.zeros(3), small, 8.0, 0.49, 32.0)
        == small
    )


def test_entry_target_is_inside_the_native_region_and_outside_unchanged_body_clearance():
    radius = local_motion.checkpoint_entry_radius(50.0, 32.0, 1.0, 0.25)
    assert radius == pytest.approx(40.5)
    assert 32.0 < radius < 49.0 < 50.0
    assert local_motion.checkpoint_entry_radius(50.0, 32.0, 15.0, 0.25) == pytest.approx(33.5)
    with pytest.raises(ValueError, match="unchanged target clearance"):
        local_motion.checkpoint_entry_radius(50.0, 49.5, 1.0, 0.25)


def test_terminal_speed_preference_fits_the_complete_stop_inside_the_interior_body_margin():
    body, entry, padding = 32.25, 40.625, 0.4306640625
    speed = local_motion.checkpoint_stop_speed(entry, body, 0.05, 0.25, padding, 0.75, 0.49, 0.05)
    horizon = 0.75 + (speed + 0.05) / 0.49
    assert 2.0 < speed < 2.37
    assert body + 0.05 * horizon + 0.25 * horizon**2 + padding == pytest.approx(entry)
    faster_horizon = 0.75 + (speed + 0.01 + 0.05) / 0.49
    assert body + 0.05 * faster_horizon + 0.25 * faster_horizon**2 + padding > entry
    assert local_motion.checkpoint_stop_speed(33.0, body, 0.05, 0.25, 1.0, 0.75, 0.49, 0.05) == 0.0
