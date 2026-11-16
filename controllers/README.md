# Controllers by person

Each folder is one current entrypoint. Shared prediction, control math and
transport stay in `src/wsorb`; do not copy them into a person's folder. Retired
entrypoints and templates live under `docs/history/controllers/`. The runtime
profile registry is `src/wsorb/controllers/catalog.json`.

| Person | STP entrypoint | Local profile IDs | Implementation | Notes |
| --- | --- | --- | --- | --- |
| Ali | [`ali/controller.py`](ali/controller.py) | `ali_mppi`; comparison profiles `ali_radar_mvp`, `ali_official_profile` | `src/wsorb/controllers/_ali.py`, `_ali_mppi.py`; `src/wsorb/controllers/official_profile.py`, `src/wsorb/flight/official_profile.py`, `src/wsorb/stp_official.py` | Current MPPI submission: one marked core, parameterized policy, terminal coast and on-screen telemetry. The radar MVP, local official-profile runner and STP source port are in [`docs/history/controllers/ali/`](../docs/history/controllers/ali/README.md). |
| WSO | [`wso/stp_controller.py`](wso/stp_controller.py) | `baseline`, `body-axis` | `src/wsorb/stp_policy.py` | The portable checkpoint controller and STP handoff. |
| Bui | [`bui/Controller.py`](bui/Controller.py) | `bui_alpha_beta_mvp` | `bui/mvp/`, `src/wsorb/controllers/_bui.py` | The `mvp/` folder is Bui's controller package and must stay beside `Controller.py`. |
| Tom | [`tom/Controller.py`](tom/Controller.py) | `tom_kalman_velocity_mvp` | `tom/avoidance.py`, `tom/debris_tracker.py`, `tom/radar_display.py`, `tom/camera_overlay.py`, `src/wsorb/controllers/_tom.py` | Current controller plus its support modules. The stock `UserAPI.py` template and display-only `live_map.py` are in [`docs/history/controllers/tom/`](../docs/history/controllers/tom/README.md). |
| Harry | none — shared local controller | `basic_pd_radar`, `coast` | `src/wsorb/flight/control.py`, `src/wsorb/controllers/_simple.py` | The local harness selects it by profile ID; see [`docs/controllers/harry.md`](../docs/controllers/harry.md). |

## Switch between them

Python runs on a teammate's machine, not on the hosted site. Hosting stays on
Cloudflare Free. Run these commands from the repository root inside the Nix development shell (`nix develop` or direnv).
After `just setup` and `just build`, start the local game:

```sh
WSORB_LAB=1 just local
```

In a second terminal, enter `nix develop` in the same checkout and start the
private runner once:

```sh
uv run --locked wsorb controllers serve --connection-file .wsorb/wsorb-local.json --run
```

Open **Test profiles** or press **Shift+C**. Choose a teammate, profile, world,
whole-number seed from 0 to 4294967295 and time budget, then check the connection. **Run flight** requires
a ready game and an online, idle Python service. It starts directly in a fresh
tab and asks before replacing a flight that has already started. The camera
follows the lander, with live metrics and **Stop controller** below the scene.
Changing a selection requests Stop; it does not start another profile.

For the hosted game, create a room and run the same Python service on your machine:

```sh
uv run --locked wsorb room create --connection-file .wsorb/wsorb-room.json
uv run --locked wsorb controllers serve --room-file .wsorb/wsorb-room.json --run
```

Open the printed private game link and allow Python before using **Run flight**.
The [hosted connection guide](../docs/flight-lab.md#hosted-python-connection)
also explains how to connect an already-open comparison selection without
losing its settings. Room creation never starts Python. Keep its link and file private.

`ali_official_profile` is available in the catalog and the matched sweep run by
[`flight_sweep.py`](../experiments/flight_sweep.py). See
[`../docs/official-comparison.md`](../docs/official-comparison.md). The old
`wso_official_profile` sweep key remains an alias so earlier configs still run.

## Keep the boundary clear

- `controllers/<person>/` is the current runnable or shareable entrypoint.
- `docs/history/controllers/` holds retired entrypoints and templates.
- `src/wsorb/` is shared reviewed implementation.
- `experiments/flight_sweep.py` and the comparison JSON files are cross-team
  experiments, not anyone's controller.
- Only one writer may command the shuttle's jets. Do not run two entrypoints
  against the same flight.
