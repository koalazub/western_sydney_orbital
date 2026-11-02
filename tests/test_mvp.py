"""SDK-free checks for the merged peer example. Never import its live entrypoint."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mvp_math(monkeypatch):
    # Import the actual adjacent namespace package, not an SDK/engine substitute.
    monkeypatch.syspath_prepend(str(ROOT / "examples" / "bui"))
    before = {name for name in sys.modules if name.split(".")[0] in {"spaceteams", "cv2"}}
    modules = tuple(
        importlib.import_module(f"mvp.{name}")
        for name in ("accel_controller", "state_estimator", "config")
    )
    after = {name for name in sys.modules if name.split(".")[0] in {"spaceteams", "cv2"}}
    assert after == before
    return modules


def test_peer_sources_compile_without_connecting_or_entering_the_control_loop():
    for path in [
        ROOT / "examples/bui/Controller.py",
        *sorted((ROOT / "examples/bui/mvp").glob("*.py")),
    ]:
        compile(path.read_text(), str(path), "exec")


def test_licensed_entrypoint_keeps_the_original_argv_bootstrap():
    tree = ast.parse((ROOT / "examples/bui/Controller.py").read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
        and node.func.attr == "connect_to_sim"
    ]
    assert len(calls) == 1
    assert len(calls[0].args) == 1 and not calls[0].keywords
    assert ast.dump(calls[0].args[0]) == ast.dump(ast.parse("sys.argv", mode="eval").body)


def test_peer_velocity_hold_uses_its_existing_cap_and_stops_at_setpoint(mvp_math):
    accel, _, cfg = mvp_math
    hold = accel.VelocityHold()
    hold.command(np.array([5.0, 0.0, 0.0]))
    command = hold.step(np.zeros(3))
    np.testing.assert_allclose(command, [cfg.ACC_MAX, 0, 0])
    assert hold.active
    np.testing.assert_array_equal(hold.step(np.array([5.0, 0, 0])), np.zeros(3))
    assert not hold.active


def test_peer_burn_sequence_keeps_original_half_open_intervals(mvp_math):
    accel, _, _ = mvp_math
    sequence = accel.BurnSequencer()
    sequence.set(
        [accel.Burn(10, 12, np.array([0.5, 0, 0])), accel.Burn(12, 14, np.array([-0.5, 0, 0]))]
    )
    np.testing.assert_array_equal(sequence.accel_at(9), [0, 0, 0])
    np.testing.assert_array_equal(sequence.accel_at(10), [0.5, 0, 0])
    np.testing.assert_array_equal(sequence.accel_at(12), [-0.5, 0, 0])
    np.testing.assert_array_equal(sequence.accel_at(14), [0, 0, 0])


def test_peer_tracker_active_uncertainty_method_uses_defined_configuration(mvp_math):
    _, state, cfg = mvp_math
    tracker = state.DebrisTracks()
    # Pure numerical inputs, not a fabricated SDK read or simulated sensor result.
    updates = np.array([1, cfg.SETTLE_UPDATES])
    ages = np.array([0.0, 2.0])
    sig_p, sig_v = tracker._sigmas(updates, ages)
    np.testing.assert_allclose(sig_v, [cfg.PRIOR_SIGMA_VEL, cfg.SIG_V])
    np.testing.assert_allclose(sig_p, np.sqrt(cfg.SIG_P**2 + (sig_v * ages) ** 2))
