"""ControllerSystem entry point for a licensed Space Teams Pro simulation.

Point the competition's ControllerSystem at this file using its existing
runtime configuration. Keep ../src/wsorb/stp_api.py, stp_policy.py and flight/
available. They are loaded directly without the historical practice-model
package. NumPy, the project's supported Python and the real bundled spaceteams
SDK must work in that runtime. This is not a browser or standalone Nix command.

Configuration comes only from WSORB_STP_POLICY_FILE or WSORB_STP_POLICY_JSON,
never argv. A valid disabled JSON document is:
{"schema": "wsorb.stp_policy", "schema_version": 1, "enabled": false}
To enable, explicitly set enabled=true and add radar_clock_offset_s using the
runtime's known clock origins. Mission measurement time equals the unchanged
source radar time plus this declared offset. Zero is valid only if independently
known to be correct. No offset is inferred from a reading. Other reviewed
settings and their limits are in STPPolicyConfig. Configuration is loaded once,
before connecting. Missing configuration coasts; invalid configuration aborts.

Use exactly one active command writer for the shuttle. Do not run this alongside
the original Controller.py/UserAPI.py or another system commanding its jets.
The enabled baseline computes target PD, radial radar repulsion and spin damping.
It has no terrain/clearance geometry, vision or collision guarantee. State and
radar reads are sequential, not an atomic observation. DoSpawning=true is
explicitly rejected. Zero requests do not cancel momentum or gravity.

The loop runs for duration_wall_s, at most 300 seconds, paced by the real
LoopFreqHz. This is a wall-clock budget, not a simulation-step API or an SDK
call timeout. Errors and exit attempt real zero commands before leave_sim.
Python finally cannot guarantee cleanup after kernel/process death, blocked SDK
calls or failed writes. There is no native lease/watchdog parity here.
"""

from __future__ import annotations

import sys
import time
from contextlib import suppress
from pathlib import Path

import numpy as np


def main() -> None:
    adapter_directory = Path(__file__).resolve().parents[1] / "src" / "wsorb"
    sys.path.insert(0, str(adapter_directory))
    from stp_api import connect_from_argv
    from stp_policy import decide_command, load_config_from_environment

    config = load_config_from_environment()
    # No arguments are parsed or removed. The SDK receives original sys.argv.
    with connect_from_argv() as client:
        try:
            deadline = time.monotonic() + config.duration_wall_s
            while (remaining := deadline - time.monotonic()) > 0.0:
                # Wall-clock pacing only. sleep is NOT a simulation-step API.
                time.sleep(min(1.0 / client.loop_frequency_hz, remaining))
                if time.monotonic() >= deadline:
                    break
                # Request up to 500 public rows; the radar may expose fewer.
                # Read radar first to reduce future timestamps relative to state.
                # The reads still cannot form an atomic snapshot.
                debris = client.GetNearestDebrisLocations(500)
                state = client.read_state()
                now = client.elapsed_time_s()
                accel, angular_accel = decide_command(state, debris, config, now_elapsed_time_s=now)
                if time.monotonic() >= deadline:
                    break
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
