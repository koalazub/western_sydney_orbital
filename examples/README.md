# Controller entrypoints by person

Each folder is one person's entrypoint. Shared prediction, control math and
transport stay in `src/wsorb`; do not copy them into a person's folder. The
runtime profile registry is `src/wsorb/controllers/catalog.json`.

| Person | STP entrypoint | Local profile IDs | Implementation | Notes |
| --- | --- | --- | --- | --- |
| Ali | [`ali/official_profile.py`](ali/official_profile.py) local; [`ali/stp_official_profile.py`](ali/stp_official_profile.py) STP source port | `ali_official_profile` (catalog and sweep; `wso_official_profile` sweep alias) | `src/wsorb/controllers/official_profile.py`, `src/wsorb/flight/official_profile.py`, `src/wsorb/stp_official.py` | The 6/6 official-profile controller. The STP source port still needs the real probe and flight. |
| WSO | [`wso/stp_controller.py`](wso/stp_controller.py) | `baseline`, `body-axis` | `src/wsorb/stp_policy.py` | The portable checkpoint controller and STP handoff. |
| Bui | [`bui/Controller.py`](bui/Controller.py) | `bui_alpha_beta_mvp` | `bui/mvp/`, `src/wsorb/controllers/_bui.py` | The `mvp/` folder must stay beside `Controller.py`. |
| Tom | [`tom/Controller.py`](tom/Controller.py) | `tom_kalman_velocity_mvp` | `tom/`, `src/wsorb/controllers/_tom.py` | `tom/UserAPI.py` is the body +Z template, not the target-seeking controller. |
| Harry | none — shared local controller | `basic_pd_radar`, `coast` | `src/wsorb/flight/control.py`, `src/wsorb/controllers/_simple.py` | The local harness selects it by profile ID; see [`harry/README.md`](harry/README.md). |

## Switch between them

Python runs on a teammate's machine, not on the hosted site. Hosting stays on
Cloudflare Free. Run these commands from the repository root inside `devenv shell`.
After `just setup` and `just build`, start the local game:

```sh
WSORB_LAB=1 just local
```

In a second terminal, enter `devenv shell` in the same checkout and start the
private runner once:

```sh
uv run --locked wsorb controllers serve --connection-file .devenv/wsorb-local.json --run
```

Open **Test profiles** or press **Shift+C**. Choose a teammate, profile, world,
whole-number seed from 0 to 4294967295 and time budget, then check the connection. **Run flight** requires
a ready game and an online, idle Python service. It starts directly in a fresh
tab and asks before replacing a flight that has already started. The camera
follows the lander, with live metrics and **Stop controller** below the scene.
Changing a selection requests Stop; it does not start another profile.

For the hosted game, create a room and run the same Python service on your machine:

```sh
uv run --locked wsorb room create --connection-file .devenv/wsorb-room.json
uv run --locked wsorb controllers serve --room-file .devenv/wsorb-room.json --run
```

Open the printed private game link and allow Python before using **Run flight**.
The [hosted connection guide](../docs/flight-lab.md#hosted-python-connection)
also explains how to connect an already-open comparison selection without
losing its settings. Room creation never starts Python. Keep its link and file private.

`ali_official_profile` is available in the catalog and the matched sweep run by
[`flight_sweep.py`](flight_sweep.py). See
[`../docs/official-comparison.md`](../docs/official-comparison.md). The old
`wso_official_profile` sweep key remains an alias so earlier configs still run.

## Keep the boundary clear

- `examples/<person>/` is a runnable or shareable entrypoint.
- `src/wsorb/` is shared reviewed implementation.
- `examples/flight_sweep.py` and the comparison JSON files are cross-team
  experiments, not anyone's controller.
- Only one writer may command the shuttle's jets. Do not run two entrypoints
  against the same flight.
