"""ControllerSystem entry point for a licensed Space Teams Pro simulation.

Point the competition's ControllerSystem at this file using its existing
runtime configuration. Keep this file beside ../src/wsorb/stp_api.py, which it
loads directly to avoid importing the historical practice-model package.
NumPy and the real bundled spaceteams SDK must work in that runtime's Python.
This is not a browser script or a standalone Nix simulation command.

Use exactly one active command writer for the shuttle. Do not run this alongside
the original Controller.py/UserAPI.py or another system commanding its jets.
The decision function is a zero-command placeholder, not avoidance or an
autopilot. Zero acceleration requests do not cancel momentum or gravity and do
not guarantee a safe flight. DoSpawning=true is explicitly rejected.
"""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from stp_api import STPState


def decide_command(
    state: STPState, debris: list[npt.NDArray[np.float64]]
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Replace with a reviewed policy using state and the public radar only.

    The placeholder ignores both inputs. Return body-frame linear acceleration
    in m/s² and angular acceleration in rad/s², each a finite NumPy 3-vector.
    Preserve the radar's original measurement times when adding a tracker.
    """
    return np.zeros(3), np.zeros(3)


def main() -> None:
    adapter_directory = Path(__file__).resolve().parents[1] / "src" / "wsorb"
    sys.path.insert(0, str(adapter_directory))
    from stp_api import connect_from_argv

    # The official bootstrap consumes the ControllerSystem's full sys.argv.
    with connect_from_argv() as client:
        try:
            while True:
                # Wall-clock pacing only. sleep is NOT a simulation-step API.
                time.sleep(1.0 / client.loop_frequency_hz)
                state = client.read_state()
                # Request all public rows; the configured radar may expose fewer.
                debris = client.GetNearestDebrisLocations(500)
                accel, angular_accel = decide_command(state, debris)
                client.SetCommand(accel, angular_accel)
        finally:
            # Best effort: this example is the sole active writer. Stop its
            # requested thrust/torque before leaving; do not erase momentum.
            with suppress(Exception):
                client.SetCommand(np.zeros(3), np.zeros(3))
    # The context manager calls the real st.leave_sim even if the loop raises.


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
