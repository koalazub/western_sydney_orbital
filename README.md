# Western Sydney Orbital

Western Sydney Orbital is our team's workspace for the Space Teams PRO competition. We use it to develop mission strategies, compare ideas and share what we learn.

## The mission and the two backends

The Fall 2026 challenge is **Escape from Phobos**. An autonomous crew shuttle must reach a return ship through impact debris. We compare flight time, fuel use and damage, then validate selected strategies in Space Teams PRO. Our [shared notebook](notebook/NOTEBOOK.md) contains research and decisions.

- **Browser practice:** the Bevy/Rapier game in this repository. It runs locally without a Space Teams PRO licence. Its native HUD owns controls, telemetry and inspection.
- **Official simulator:** a licensed Space Teams PRO runtime using the real `spaceteams` SDK. Our [SDK adapter](src/wsorb/stp_api.py) calls its actual methods; it is not the browser backend.

The [published team simulation](https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev) may lag the local branch. New local changes are not published until the owner deploys them.

## Start the local game

Install Nix, devenv and Just, then use the repository environment. Rust, Node and build tools come from Nix; do not install a separate global Rust toolchain.

```sh
just setup
PORT=0 just local
```

The terminal prints the actual `http://127.0.0.1:PORT/` address. Open that address in a WebGPU-capable browser. We manually check Safari Technology Preview on macOS. Initial WASM optimization can take several minutes.

If the assets have already been built and you only need to restart the server:

```sh
PORT=0 just --no-deps local
```

This serves the existing build. It does not rebuild changes or deploy anything. Keep the server process running while you use the game; Ctrl+C stops it.

## Inspect and experiment in Marimo

The [Marimo flight lab](notebooks/flight_lab.py) is where the team develops debris prediction, path planning, controller logic and experiment scheduling. Those algorithms are Python, with editable notebook functions and shared [Python modules](src/wsorb/flight/). Bevy/Rapier runs the physical world and sensors; it does not choose routes or run an autonomous controller. It only displays the route Python sends it.

From the repository root, build once and start the two Devenv processes:

```sh
just build
devenv up lab simulation
```

Devenv manages the game at `http://127.0.0.1:61829/` and Marimo at port `2718`. Use **Open notebook** in the local game to open the authenticated editor. On the hosted game, this button shows the local setup instructions. Keep one game tab open and visible, ideally in a separate window. In Marimo:

1. Connect and refresh to inspect real public observations, radar and flight metrics.
2. Edit the prediction, planning and controller functions and their parameters in Python. **Plan and show route** runs the Python planner without steering. The initial controller requests zero acceleration. Editing functions or refreshing measurements does not apply thrust.
3. Select a controller and arm **Apply and run** to run the visible simulation while Python supplies commands. Change parameters and apply again to start a new run with those changes. A read-only monitor shows the latest real observations and run status. **Run one policy step** remains available for bounded, paused experiments.
4. Use Stop or Reset when needed. **Upright lander** pauses, aligns it with the launch pad and clears spin/thrust without moving or braking it. This local test intervention is recorded and stops a notebook run; it is not an STP maneuver. A failed connection cannot guarantee delivery of a notebook Stop request.

The lab also has staged world/ship controls, measured charts and exports:

- Use the linked sliders and number boxes to change ship mass/inertia, asteroid count, size range, stream speed, direction and shell coverage. **Apply world/ship settings and reset** starts a new physical trial. It never changes the official acceleration command limits.
- The live asteroid field surrounds Phobos rather than just the station route. Its initial population fills an already-flowing shell; bodies leaving the bounded volume return through the upstream inlet. All motion and mutual/terrain/ship contacts use real rigid bodies. There is no fragmentation. **5 / Overview** frames Phobos and Mars at their actual positions.
- Camera aim controls use the published yaw/pitch limits. The top camera pitches from −50° to 0°, the bottom from 0° to 50°, and both yaw from −89° to 89°. The practice capture remains pinned to the published square 60° example, 512×512 RGB8 and fixed exposure. Explicit image captures return real, unenhanced GPU pixels.
- The graphs use measured, timestamped history. Missing readings stay missing; reset epochs are not joined into invented trajectories. Export experiment JSON, history CSV or the saved Python notebook after a run. Exports include applied settings and engine identity, not login credentials.

The notebook is a normal `.py` file, so teammates can read, change and review it in the private repository. Its controller loop starts only on an explicit armed run action. Python decides when to sample, predict, plan and command. Use one command writer; do not run another controller against the same game. Displaying an A* route never makes Bevy follow it.

Use `devenv up -d lab simulation` for background processes, `devenv processes logs lab` for the editor URL and `devenv processes down` to stop them. `WSORB_GAME_PORT` and `WSORB_LAB_PORT` override the defaults. `just format` formats Python and Rust; `just check` checks Python/Marimo, Rust and the web build; `just unit-check` runs both test suites. No inline Python probes are needed.

| Code | What to change there |
| --- | --- |
| [`notebooks/flight_lab.py`](notebooks/flight_lab.py) | Python policy, experiment controls and analysis cells. |
| [`src/wsorb/local_api.py`](src/wsorb/local_api.py) | Python access to the local game. |
| [`src/wsorb/flight/`](src/wsorb/flight/) | Pure Python prediction, collision geometry, A* and controller functions, with no simulator or notebook dependency. |
| [`simulation/src/navigation/`](simulation/src/navigation/) | Validation and drawing of Python's route preview, with no planner or tracker. |
| [`simulation/src/physics.rs`](simulation/src/physics.rs) | The actual local Rapier physics and command implementation. |
| [`src/wsorb/stp_api.py`](src/wsorb/stp_api.py) | The separate adapter for licensed STP execution. |

Python cell changes do not require a WASM rebuild. Rust and versioned scenario changes require `just build` and a game-page reload.

`SetCommand(accelCommand, angAccelCommand)` and `GetNearestDebrisLocations(N)` use the official helper names, body-frame SI vectors and 3×3 radar matrices. The local client and the genuine STP adapter keep transport details outside the algorithms. Pause, manual stepping and route drawing are local experiment tools, not claimed STP SDK methods. The licensed adapter preserves the SDK's passive quaternion and radar timestamps; align frames and clock origins explicitly when moving an experiment to STP.

The local connection is opt-in, loopback-only and bearer-authenticated. Its private connection file lives at `.devenv/wsorb-local.json`, or `WSORB_LOCAL_CONNECTION` when set. Do not publish that file or its token. Without a ready browser, the lab reports an error rather than returning sample results. Public observations exclude hidden debris truth; report timestamps can differ because telemetry and sensors have their own cadence. The ordinary local server remains read-only when `WSORB_LAB` is unset.

For teammates, the simplest setup is to clone the private repository and run their own game/lab pair. Use private screen sharing for a shared demo. Do not expose the Marimo editor publicly: it can execute Python on its host. Remote access needs an owner-approved authenticated tailnet setup, not Tailscale Funnel. Nothing is published or deployed automatically.

## Controls and following the lander

Click the scene, or focus the canvas with Tab, before using game keys. **H** opens controls/settings and shows held input, controller mode and thrust limits. **F** follows the lander again if you lose it.

| Input | Action |
| --- | --- |
| Space | A fresh press launches from pause; holding it fires upward thrust. |
| P | Run or pause the simulation. |
| W / S | Fire the front / back nozzle. The resulting force points backward / forward. |
| A / D | Fire the left / right nozzle. The resulting force points right / left. |
| E/Q | Reference-power upward/downward thrust. |
| Alt/Option + movement keys | Move only the independent debug camera. Shift boosts debug movement only. |
| F | Follow the lander with stable Orbit tracking; reset pan and viewer zoom. |
| Shift+F | Frame the currently selected object. |
| H | Show/hide controls, live inputs and controller settings. |
| 1 / 2 / 3 / 4 / 5 | Orbit / debug first person / third person / free flight / overview. |
| 6 / 7 | Fixed top / bottom ship-camera feed. |
| Left drag / right or middle drag | Orbit or look / pan the debug view. |
| Wheel / trackpad pinch | Adjust the debug view. Ship-camera optics remain fixed. |
| R / . / T | Reset prompt / one paused physics step / playback rate. |
| I / , / / | Inspector / previous focus / next focus. |
| L / X / M | Mouse capture or release / browser fullscreen / window maximize. |
| Tab / Enter / Esc | Select a control / activate it / close a panel or release the view. |

Releasing keys stops thrust, **not velocity or spin**. Use opposite thrust to slow down. Autonomous control belongs to Python, not the game. After pause, focus loss, a modal or an external-command handoff, release movement keys before pressing them again if the HUD requests rearming.

These are lander-body sides, not screen directions. The debug body-axis guide and H-panel legend identify front, left and up. In **H → Key bindings**, select an action, press a new physical key and release it before flying. Esc cancels capture; restore defaults returns to WASD/QE/Space. Reserved browser/game shortcuts and duplicate bindings are rejected. Bindings persist for that browser origin. Alt/Option camera navigation keeps its normal forward/back/left/right meaning.

The controls panel does not pause flight. Blue-white debug streams follow the actual applied jet forces in the opposite direction. They are a visual cue, not a calibrated gas or light simulation.

## Metrics and strategy comparisons

The flight HUD shows actual speed and radial climb in m/s, terrain-centre clearance in metres, and thrust acceleration in m/s². Details includes target range/closing speed, force in N, torque in N·m, spin, fuel/damage estimates, sampled peaks and simulation time. These are lander measurements, not debug-camera speed. Terrain-centre clearance is not landing-foot or pad clearance.

Space, E, side keys and the normal command API use the **0.5 m/s² reference limit**. There is no live practice-boost switch. Older exported runs may record a boost profile; do not compare those directly with current reference-limit experiments.

For paired comparisons:

1. Reset and use the same seed, scenario and step schedule for each Python strategy.
2. Measure arrival time, speed/closing conditions, fuel and damage together.
3. Export the run from Details. JSON includes telemetry and the verified engine-build SHA-256.
4. Use only public observations for the planner. Radar covers a configured subset; unobserved rocks are not clear space. The amber path is advisory and never drives the lander.
5. Validate promising policies in licensed STP using its actual sensors, actuation and scoring.

See the [source-backed API and measurement reference](notebook/entries/20261023T142145Z-stp-api-flight-metrics-and-practice-thrust.md) for units, frames, clock origins and known differences.

## Use the real Space Teams PRO API

[`src/wsorb/stp_api.py`](src/wsorb/stp_api.py) is an optional adapter over the real SDK. [`examples/stp_controller.py`](examples/stp_controller.py) is an entry point for an official ControllerSystem. It contains a **zero-command placeholder**, not an avoidance strategy. Replace `decide_command` with a reviewed policy before expecting mission progress.

Run the example through the licensed simulator's Python-system workflow, not as a browser script or a standalone Nix simulation. Keep its relative source layout, or install the adapter where that runtime can import it. Configure the existing ControllerSystem with:

- Entity references `Phobos`, `Shuttle`, `Target`, `TopCamera` and `BottomCamera`.
- A positive `LoopFreqHz`, simulation `DoSpawning=false`, and `Time/StartDateTAI`.
- The real bundled `spaceteams` SDK and runtime-provided process arguments.

Use **one active command writer** for the shuttle. Replace the original controller source rather than running both writers. Do not invent connection ports or argv; follow the official system-launch configuration. On exit, the example makes a best-effort zero-command request before leaving; this stops requested thrust/torque, not momentum, and cannot guarantee delivery if the connection has failed.

| Adapter method | Actual SDK calls underneath |
| --- | --- |
| `client.SetCommand(accel, angular_accel)` | `shuttle.SetParam(st.VarType.doubleV3, ["JetSelection", "CommandedAccel"], accel)` and `"CommandedAngAccel"`. Both inputs are finite NumPy 3-vectors in body-frame m/s² and rad/s². |
| `client.GetNearestDebrisLocations(N)` | `GetParamArray(st.VarType.doubleM3x3, ["Radar", "SortedDebrisData"])`; original public matrices/timestamps retained. |
| `client.PanTiltCamera(camera, yaw, pitch)` | Official clipped degree limits, `Euler321_to_DCM`, then `camera.setRotation_DCM(..., shuttleFrame)`. |
| `client.read_state()` | Frame-qualified location, velocity, passive SDK quaternion, angular velocity, mass, inertia and official stats getters. This method/report is our adapter convenience. |
| `client.capture_image(camera, properties)` | Actual `st.camera.CaptureImageBlocking`; returns the real captured object or `None`. |
| `client.elapsed_time_s()` | Official SimClock minus `StartDateTAI`. Wall-clock `sleep` paces the loop; it is not a simulator-step call. |

The official actuator caps vector norms at **0.5 m/s²** and **0.05 rad/s²**. The adapter forwards validated vectors without changing those settings. Missing SDK/runtime access raises a clear error; there is no fake SDK fallback. Calls are corroborated against pinned [official starter](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/UserAPI.py) and [actuator](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py) sources. Execution still needs verification in your licensed runtime.

## Local API access and troubleshooting

The browser exposes local WASM functions, not official SDK objects. After the app has loaded, a local client or the browser console can reuse its initialized module:

```js
const manifest = await (await fetch('/sim/manifest.json')).json();
const api = await import(`/sim/wsorb_bevy.js?v=${manifest.sha256}`);
JSON.parse(api.telemetry_status());
JSON.parse(api.observation()); // Own/target state and public radar.
```

Use `api.command(JSON.stringify(request))` for the documented local operations, including `set_command`, `stop`, `pause`, `reset` and `step`. A queued pause needs an engine update before manual stepping. Local `step`, `restore`, `view`, `telemetry_status` and `navigation_status` are **not** SDK aliases. The full debug snapshot returned by the local `observe` operation contains hidden debris truth; do not feed those fields to a flight planner.

If the HUD is missing, confirm that you opened the printed local address rather than an older published release, reload, then use H for controls and F to recover the lander view. For a bug report, include a screenshot and the read-only `JSON.parse(api.hud_status())` and `JSON.parse(api.renderer_status())` reports. They distinguish native layout/target information from completed world rendering. No browser automation is required.

## Model and team workflow

The practice game starts on Phobos and targets a station-keeping return ship near a force-derived Mars–Phobos L2 point. It uses a circular rotating-frame model, synthetic terrain/nozzles and approximate contact, fuel, damage and radar models. Its results are **not official competition scores or validated STP physics parity**. Quaternion conventions, radar clocks, jet allocation and fuel/scoring differences require care when moving a policy between backends.

Top and bottom ship cameras retain genuine 512×512 RGB8-per-channel images with fixed exposure. Shadow, occlusion and Mars backgrounds limit what they reveal. HUD metrics, path markers and debug exhaust do not modify those pixels.

Each teammate can run an independent local instance. Review changes in [`simulation/scenarios/`](simulation/scenarios/) and discuss them through the shared notebook/NotebookLM before an owner-controlled release. Keep the repository private and Actions disabled; local development does not deploy to Cloudflare. Historical Lunar SAR and rover experiments remain available for reference.

## Team members

| Member | Responsibility | Reference |
| --- | --- | --- |
| To be added | Mission planning | [Simulation structure](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Sim-Structure) |
| To be added | Python and automation | [Python systems](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python) |
| To be added | Testing and analysis | [Simulator debugging](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Python-System-Debugging) |
| To be added | Team coordination | [Collaboration](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Collaboration-with-Git) |

## Competition resources

- [Official announcements and discussions](https://github.com/SimDynamX/SpaceTeamsPRO/discussions)
- [Escape from Phobos rules](https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218)
- [Competition simulation](https://github.com/SimDynamX/STU4-Sim)
- [Controller starter and mission API](https://github.com/SimDynamX/STU4-Solution/wiki)
- [Space Teams PRO documentation](https://github.com/SimDynamX/SpaceTeamsPRO/wiki)
- [Running code in the simulator](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python)
- [Python API](https://simdynamx.github.io/stp-python-api-docs/)
