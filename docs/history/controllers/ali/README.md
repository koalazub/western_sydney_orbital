# Ali controller history

These entrypoints are retained for reproduction. They are not the current
submission.

- [`radar_mvp.py`](radar_mvp.py) — the earlier radar-only MVP; its local profile
  is `ali_radar_mvp`.
- [`official_profile.py`](official_profile.py) — the local official-profile
  runner.
- [`stp_official_profile.py`](stp_official_profile.py) — the licensed-runtime
  source port.

The current controller is
[`controllers/ali/controller.py`](../../../../controllers/ali/controller.py), with
the `ali_mppi` profile. Its support code remains in `src/wsorb/controllers/` and
`src/wsorb/flight/`.
