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

## Controls and following the lander

Click the scene, or focus the canvas with Tab, before using game keys. **H** opens controls/settings and shows held input, controller mode and thrust limits. **F** follows the lander again if you lose it.

| Input | Action |
| --- | --- |
| Space | A fresh press launches from pause; holding it fires upward thrust. |
| P | Run or pause the simulation. |
| W/S, A/D | Body-frame forward/back and left/right jet forces. Offset nozzles can roll or topple the lander. |
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

Manual thrust takes over from the example controller. Releasing keys stops thrust, **not velocity or spin**. Use opposite thrust to slow down. Controls/settings can re-enable the example controller. After pause, focus loss, a modal or an external-command handoff, release movement keys before pressing them again if the HUD requests rearming.

The controls panel does not pause flight. Blue-white debug streams follow the actual applied jet forces in the opposite direction. They are a visual cue, not a calibrated gas or light simulation.

## Metrics and strategy comparisons

The flight HUD shows actual speed and radial climb in m/s, terrain-centre clearance in metres, and thrust acceleration in m/s². Details includes target range/closing speed, force in N, torque in N·m, spin, fuel/damage estimates, sampled peaks and simulation time. These are lander measurements, not debug-camera speed. Terrain-centre clearance is not landing-foot or pad clearance.

Space defaults to **2.0 m/s² practice boost**, four times the official reference command limit. In Details or Controls, select **Use reference Space thrust** for 0.5 m/s². E, side keys and the normal command API retain reference limits. Switching profiles stops keyboard-owned thrust and requires key release; it does not brake the lander.

For paired comparisons:

1. Select reference thrust and reset. Use the same seed, scenario and step schedule for each strategy.
2. Measure arrival time, speed/closing conditions, fuel and damage together. Do not compare boosted takeoff directly with reference-limit runs.
3. Export the run from Details. JSON includes telemetry, selected profile and the verified engine-build SHA-256.
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
