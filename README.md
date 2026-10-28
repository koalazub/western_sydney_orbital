# Western Sydney Orbital

Western Sydney Orbital is our team's flight lab for the Space Teams PRO competition. We use it to try ideas, watch what happens and share experiments that teammates can repeat.

## Why we are building this

The Fall 2026 challenge is **Escape from Phobos**. An autonomous shuttle must leave Phobos and reach a return ship through impact debris. A useful strategy needs to arrive with acceptable speed, fuel use and damage.

The local simulator gives us a place to learn the controls, change conditions and investigate failed flights. The Python notebook lets us turn those observations into prediction, route planning and flight-control code. We then take promising strategies into the licensed competition simulator for validation.

You do not need to open Python for a quick manual flight or a parameter experiment. Use the game's task tabs for those. Open the notebook when you want to change an algorithm, automate a run, inspect graphs or prepare results for the team.

## How the workspace fits together

| Part | What we use it for |
| --- | --- |
| Browser simulation | The physical world, gravity, collisions, ship cameras, manual flight and in-game instruments. |
| [Python flight lab](notebooks/flight_lab.py) | Editable prediction and control code, experiment settings, measured history and downloads. It opens in Marimo, a Python notebook editor. |
| [Shared mission notebook](notebook/NOTEBOOK.md) | Research, decisions and explanations of results. This is a notes index, not the live flight controller. |
| [Space Teams PRO adapter](src/wsorb/stp_api.py) | Running reviewed Python policies against the real, licensed competition simulator. |

Python reads observations from the open game, decides what to do and sends commands back. The game applies those commands through its physics engine and displays Python's suggested route. Drawing a route does not make the lander follow it.

The [published team simulation](https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev) can be older than the local version. Local work is not published automatically.

## Start the game and notebook

Install Nix, devenv and Just first. Use the repository's environment so everyone gets the same Rust, Node and Python tools; a separate global Rust installation is not needed.

From the repository root:

```sh
just setup
just build
devenv up lab simulation
```

The first setup and build can take several minutes. Later builds reuse verified, unchanged outputs.

1. Open the game at **http://127.0.0.1:61829/** in a current browser with WebGPU enabled, the graphics feature the game uses.
2. Wait for the world to appear. Keep one game tab open and visible, preferably beside the notebook.
3. Select **Notebook** in the game's bottom toolbar to open the authenticated editor. The notebook normally runs on port `2718`; use the game's button rather than sharing its private access link.
4. Leave the Devenv processes running while you work. `devenv processes down` stops them.

Use `devenv up -d lab simulation` to run them in the background, and `devenv processes logs lab` if you need the editor's startup message. The process named `notebook` is a separate sharing service; it is not the Marimo flight lab.

For a game-only session, use `PORT=0 just local` after `just setup` and open the address printed in the terminal. `PORT=0 just --no-deps local` serves an existing build without rebuilding it. Choose this path or the managed game-and-lab setup above, rather than starting both accidentally.

### Windows game engine

The game is WebAssembly with WebGPU, not a native Windows executable. For a prebuilt game, use a supported Windows 11 machine, Node 26.12.1 or later in the 26.x series, and current Chrome or Edge with working hardware-accelerated WebGPU. Copy the complete matching `worker/public` assets from `just build`; a source checkout alone does not contain the generated engine.

From PowerShell at the repository root:

```powershell
node .\worker\scripts\platform-probe.mjs
if ($LASTEXITCODE -ne 0) { throw "CPU/WASM probe failed" }
$env:WSORB_LAB = "0"
$env:WSORB_HEADLESS = "0"
$env:PORT = "0"
node .\worker\scripts\local.mjs
```

Open the exact loopback URL printed by the server. The probe verifies actual WASM integrity and physics on the machine running it; it does not certify WebGPU, rendered frames or camera RGB readback. Those checks still need to run on the Windows GPU and driver. No Windows runtime or GPU pass is claimed from macOS tests.

For building or using the private Python lab and control APIs, run the backend inside WSL2 with the repository and secrets on its Linux filesystem under the WSL home. Do not assume `/mnt/c` or Windows `chmod` provides the POSIX private-file protection these services require. Verify Windows-to-WSL localhost forwarding for both game and notebook ports. Native Windows private-lab ACL support is not implemented; the game-only command above does not enable private control.

## Get your bearings

The initial Overview shows Phobos and Mars at their actual modelled sizes and separation. Press **F** for a close view of the lander. Use **Views** to change the camera, lighting, stars or guide visibility.

The two launch setups let us compare departures on opposite sides of Phobos:

```text
Mars -------- L1 -------- Phobos -------- L2
```

This diagram is not to scale. **Mars-facing L1** launches toward the region between Phobos and Mars. **Far-side L2** launches away from Mars toward the outer region. L2 is not on the Mars-facing side. The return ship is held near the selected region as a repeatable target.

Both setups include Mars's gravity. From the close far-side launch view, Phobos can block Mars from sight; that does not remove its gravitational effect.

## Play directly in the game

The world starts paused. Select **Play** or press **P** to advance physics. Pausing freezes the lander and debris together, even when their stored velocities are nonzero. Increasing thrust strength while no thrust is requested does not fire the engines.

Click the scene before flying. These are the main controls; **H** opens the full controls and key-binding panel.

| Control | What it does |
| --- | --- |
| P | Play or pause the physical world. |
| Space | A fresh press launches from pause; hold it for upward thrust. |
| E / Q | Upward / downward thrust. |
| W / S | Fire the front / back nozzle, pushing backward / forward. |
| A / D | Fire the left / right nozzle, pushing right / left. |
| F / Shift+F | Follow the lander / frame the selected object. |
| 5 / 6 / 7 | Overview / top ship camera / bottom ship camera. |
| Alt/Option + movement keys | Move the viewing camera without commanding the lander. |
| Drag / wheel or pinch | Look around, pan or change the viewing distance. |
| Tab / Enter / Esc | Move keyboard focus / activate a control / cancel editing or close a panel. |

Nozzle directions belong to the lander, not the screen. Releasing a key stops thrust, not existing velocity or spin. Use opposite thrust to slow down. After a reset or input handover, release held movement keys before pressing them again if the game asks.

### Find controls by task

Flight readings, live tuning and launch setup have separate places. A measurement stays a readout. An input changes the real physics, and an action that replaces the world says so explicitly. Longer model explanations belong in Details rather than above everyday controls.

Use **Flight** for measured motion and mission status, **Ship** for live hardware inputs, **Debris** for live stream inputs and body motion, and **Launch** for moving the pad or preparing a new world. **Views** and **Help** hold camera and keyboard controls. Play, Follow and Notebook stay separate from these tabs.

### Edit inputs and inspect the result

Open **Ship** or **Debris** to edit live inputs. Editable controls change the underlying physical settings. Measured speed, force, spin, fuel use and damage are readouts, not numbers we overwrite to make a run look successful.

| Change | What to expect |
| --- | --- |
| Lander mass | Updates the existing body without moving it or resetting the clock. |
| Inertia factor | Changes how hard it is to start or stop the lander spinning. The actual inertia readings are shown separately. |
| Local thrust strength | Changes the force available from an existing command. It does not start a command or resume a paused flight. |
| Stream speed and direction | Changes the common flow of existing stream debris. Individual collision and gravity effects are retained. |
| Stream speed spread | Changes newly spawned rocks, not a fresh random velocity for every existing rock. |
| Count, sizes, coverage, launch setup or debris mode | Prepares a new-world configuration. Apply it explicitly to replace the current trial. |

Select a numeric editor, enter a value and confirm it with **Enter** or **Apply**. Leaving the field also commits it; **Esc** cancels typing. The **+** and **-** buttons make small adjustments. Read any validation message beside the controls, and check the confirmed physical reading rather than assuming the requested value was accepted.

The flight strip keeps speed, clearance, climb, target distance, closing speed and last-applied thrust visible. Clearance is measured from the lander's centre; it is not the gap beneath its feet. Instrument timestamps distinguish a current request from the last force actually applied. While paused, changing a force does not advance motion.

### Move the launchpad on Phobos

Open **Launch** and select **Place lander**. This pauses flight and frames the current launch site. Drag the launchpad or its handle along Phobos, then release to move the pad and seat the lander upright at rest. There is no second Apply step after a drag. The preview follows the actual terrain; it does not change physics until you release. **Esc** cancels, and flight stays paused.

To place without dragging, select **Place lander**, then click a surface point or use the directional buttons or arrow keys to adjust the preview. Select **Apply** to use that site. You do not need to enter XYZ coordinates.

Moving the launchpad keeps the debris field, simulation time, hardware, fuel, damage and mission outcome. It clears the lander's thrust and motion and records a local test intervention. The selected L1 or L2 target does not move with the pad. A blocked or cancelled placement leaves the existing site unchanged.

Reset flight returns to the chosen custom launch site. Creating a new world returns to that preset's starting site.

### Choose a target setup or place the lander separately

In **Launch**, expand the new-world setup to choose **Mars-facing L1** or **Far-side L2**. **Create paused world** opens a replacement confirmation; **Replace run at time zero** applies it. Expand **Size, shell and ejection inputs** for advanced settings. The default remains the far-side L2 setup with 500 debris bodies. Choosing a draft does not replace an ongoing flight or limit where you can drag the current pad.

The advanced lander-only placement controls are a different tool. Enter world X, Y and Z coordinates and select **Apply XYZ position** to move the lander without moving its pad. **Reload current XYZ** fills the fields from the current lander position. Placement pauses flight, aligns the lander away from Phobos, stops its motion and clears thrust. Placement inside another body or outside the supported world is rejected.

The notebook's **Launch → Separate lander placement, advanced → Place lander only** controls move only the lander. A rejected placement leaves the Python controller attached; a valid placement stops its worker.

Placement is recorded as a test intervention, not a flown manoeuvre. It does not erase fuel, damage or a completed mission result. Use **Reset flight** before trying to fly another completed mission. Zero coordinates are the centre of Phobos, not a safe launch location.

Positive X points toward Mars; negative X points away. These are world coordinates, not directions relative to the camera or lander.

## Experiment with debris

Debris bodies have mass, velocity and spin. Gravity changes their paths, and physical collisions can change their motion. They do not move merely because the renderer advances a decorative animation.

- **Directed stream** provides a continuing incoming field. New rocks enter through the upstream side as others leave the bounded stream area.
- **Impact ejecta** starts a debris cloud with an adjustable velocity kick and direction. Gravity and collisions then determine its paths. Some rocks can remain bound, some fall back and some escape. A circular belt is not guaranteed.

The ejecta setup represents debris after an impact. It does not model a rock breaking into fragments. Stream calibration controls and ejecta starting conditions are different experiments; use the controls for the active mode.

Start with the default 500 bodies. Larger trials can request 10,000 or 100,000 real bodies, but the upper input limit is not a promise of smooth play on every machine. Dense fields or narrow spawn regions may not fit the selected sizes. Read setup errors and watch the simulation clock: a heavy world can take longer than an actual second to simulate one second. Use short paused steps for large trials.

## Use Python for guided flights

The same built engine can run without a browser:

```sh
WSORB_LAB=1 WSORB_HEADLESS=1 just local
```

This starts an explicit, separate native-headless flight through the existing private loopback API. It runs the actual physics, sensor delivery, playback and telemetry schedules. Renderer status stays unstarted and camera capture reports no sample; no browser or replacement image is involved. The notebook connects using the private connection file as usual. **Pause physics** works without a game window. Starting this mode does not attach to or reset a flight already open in a browser.

### Run a checkpoint attempt through the APIs

Inspect a fresh isolated world without issuing flight commands:

```sh
devenv shell uv run --locked wsorb checkpoint --isolated
```

Explicitly run matched baseline and body-axis attempts in separate stock worlds:

```sh
devenv shell uv run --locked wsorb checkpoint --isolated --run --policy compare --playback-rate 5 --cadence-hz 50
```

This script uses authenticated HTTP calls and the same portable numeric controller as the colleague-facing STP script. It does not click the interface, reset an open flight, call a fixed-step test driver or emulate `spaceteams`. The two portable profiles differ only in body-axis preference; they are separate from the notebook's native-contract resilient A* controller. Default inspection is read-only, and default playback is 1×. Accelerated runs can expire their leases when computation or transport is too slow; expiry is a failed attempt, not permission to extend or retime the decision.

The final JSON reports actual native arrival, range, speed, damage, fuel, score and source/configuration diagnostics. A run succeeds only on native mission success with the unchanged stock arrival/damage limits. L1 command effort is labelled as a proxy, not fuel. No report files are written automatically. To inspect an existing local flight instead, supply an explicit `--connection-file`; it is never implicitly reset, tuned or used for comparison.

### Watch Python fly the hosted game

Python runs privately on your machine. The Cloudflare room relays authenticated calls to the real WASM engine in your browser; it does not run a Python kernel or publish the controller's source.

```sh
devenv shell uv run --locked wsorb room create --connection-file .devenv/wsorb-room.json
```

Open the private browser link printed by this command in a new tab, then choose **Allow Python connection**. This starts a separate game; leave existing flights open. Keep the new game visible in a separate window. Inspect first, then explicitly start the same portable checkpoint policy:

```sh
devenv shell uv run --locked wsorb checkpoint --room-file .devenv/wsorb-room.json
devenv shell uv run --locked wsorb checkpoint --room-file .devenv/wsorb-room.json --run --progress
```

Keep the link and connection file private. A room cannot replace an already connected owner. Room calls read fresh public observations, not saved room state or hidden debris truth. Flight control uses the native ownership fence and original-source expiring commands. A disconnect never retries thrust, resets the game or extends a decision's validity. Manual ship input or pausing ends the run. The hosted bridge deliberately excludes reset, tuning, raw latched thrust and private local-lab endpoints. Network latency may exhaust a lease and end an attempt; this connection is not STP runtime validation.

Add `--wait-for-owner-s 3600 --progress` when Python should wait for the external game to connect. Waiting is read-only, stays within the invocation's total wall budget, and does not grant `--run` consent. Browser readiness requires advancing real renderer counters; native-headless readiness uses its actual scheduler status. An existing controller is not replaced. Only read-only owner/network unavailability is retried, with bounded wait-error logs; authentication and protocol failures end the wait. Flight commands are never retried. Read-only hosted observations remain available when the initialized page is hidden, but commands still require a visible game.

An instrumented, already-loaded hosted game also accepts a private room link through a fragment change without restarting its WASM or physics. It still asks for permission, and refuses to replace an active or pending room connection. The local game shows which tab owns its Python API connection; separate tabs and hosted rooms are separate flights, not mirrored views. A new game URL can opt into a close lander view with `?follow=lander`; this requests only a debug-camera change after real renderer readiness.

A local game started with a separate connection file does not redirect its Notebook button to the default flight's editor. Its help page identifies the explicit API connection instead, so opening the notebook cannot silently control a different game.

### Opt-in flight diagnostics

Diagnostics are off by default. Collect a short, read-only log from an existing flight without starting a controller or changing its playback:

```sh
devenv shell uv run --locked wsorb diagnostics --connection-file .devenv/wsorb-local.json --duration-s 5 --output .devenv/flight-debug.jsonl
```

Use `--room-file` for a hosted flight. The output path must be unused; logs are private mode-0600 JSONL. The collector skips a busy local API rather than competing with the controller. It records actual source clocks, ownership, Rapier motion and renderer counters. Page identity, debug camera and Bevy visual transforms are included only when the loaded bridge/engine supplies them; older builds report them unavailable.

Add `--diagnostics` to an explicit checkpoint invocation for correlated transport logs on stderr, or set `WSORB_DIAGNOSTICS=1` before starting Python or the local backend. Unset the flag to return to the default. Python calls `wsorb.diagnostics.enable()` and `disable()` affect the current Python context; the environment flag also covers worker threads.

An instrumented browser exposes `globalThis.__wsorbDiagnostics.enable(true)` and `enable(false)`. A new game can also opt in with `?diagnostics=1`. Do not navigate or reload an active flight just to enable logs. Browser events are bounded, and disabling clears native collection; per-request native collection otherwise expires after two seconds. Visual-transform samples are main-world Bevy measurements, not proof of GPU pixels.

All logging passes through dedicated helpers. Tokens, capability URLs, raw error text, camera pixels, radar rows and hidden debris state are excluded. No collector command resets, reloads, claims control or retries actuation.

### Run from the notebook

In the notebook:

1. Open **Flight** and select **Connect / Refresh**. This watches the existing flight and publishes Python guidance. It does not reset, pause, resume or command thrust.
2. Open **Python policy** to inspect prediction and A* search. The default resilient controller keeps state estimates, radar selection and route progress in the Python flight modules. A route remains advisory until a controller sends commands.
3. Choose the controller and its settings. Changes to connected notebook functions and supported controls reach the existing worker without restarting the flight.
4. Select **Run controller** to hand over flight control. This can resume an eligible paused flight after its first valid command.
5. Watch the native mission result and checkpoint range in **Flight**, alongside the game and **History**. A candidate path or a running controller is not checkpoint arrival. The stock local mission must report `success` inside its 50 m arrival region.
6. Use **Stop Python control** to end the notebook worker and its standby. Resilient mode requests a guarded native release without pausing physics or clearing a newer manual command. If release cannot be confirmed, native expiry still bounds its old command. Use **P** in the game or **Pause physics** in the notebook to pause. The older baseline controllers still stop and pause during cleanup. Select **Connect / Refresh** to resume read-only guidance.

Keep one controller in charge of a game. Manual flight, native pause or a placement/reset can end Python's control; the worker must not overwrite a manual takeover. Playing the simulation again is not the same as requesting another Python-controlled run.

| Notebook tab | Use it for |
| --- | --- |
| Flight | Connect, run or stop the controller and inspect its current status. |
| Resilience | Inspect sensors, estimates, controller ownership and the standby. Interrupt primary decisions or fail and restore sensor delivery. |
| Ship | Change live ship inputs and inspect their actual readbacks. |
| Debris | Change live stream inputs and prepare clearly labelled new-world debris drafts. |
| Launch | Move the launchpad in the game, reset the flight or explicitly regenerate the world. Advanced lander-only placement is separate. |
| Python policy | Edit algorithms, gains, timing and paused policy steps. |
| History | Inspect received measurements and changes between runs. |
| Cameras | Aim the mounted cameras and request actual image captures. |
| Export | Prepare experiment, history and saved-source downloads. |
| Details | Inspect configuration, model notes and source references. |

Run duration is counted from the original Run action. Shortening it below the elapsed simulation time ends that controller; it does not restart the clock. The requested decision rate controls how often Python tries to make a decision, not the physics step size.

The notebook now starts with a 1,800-simulation-second flight budget and an 8 Hz decision target. This allows a full checkpoint attempt rather than ending after a short movement check. The limit is not an arrival-time prediction; native mission completion can stop flight sooner. The Flight readouts retain the actual range, relative speed, damage and navigation source time.

**Reset flight** restarts the chosen trial while retaining live calibration. **Regenerate world** replaces it with the selected world settings and clears the live overrides, including the local thrust multiplier. Both stop the notebook worker; reconnect before another run.

Notebook edits and supported live parameter changes do not require rebuilding the game. Changes to the Rust simulation do require `just build` and a game-page reload. Save results before rebuilding or reloading a flight you want to keep.

### Review local configuration proposals

In **Launch**, open the proposal importer inside the new-world controls. Download its versioned JSON template for local editing or NotebookLM suggestions. Upload the result, select **Review proposal**, inspect the normalized settings and content digest, then select **Use reviewed proposal as draft**. A changed file cannot reuse the previous review digest.

Import and approval only stage data. They do not tune live hardware, write files or Git, replace the world, or give NotebookLM an execution API. Only the separate **Regenerate world** action applies the approved draft and explicitly replaces the run. An active imported draft is labelled above Launch; **Discard imported draft** restores the manual draft controls. Keep reviewed proposal files in the private repository, never connection descriptors or tokens.

### Sensor failures and redundancy

Select **Resilient route with radar failover** in Python policy. The Python worker selects genuine primary or backup radar readings, maintains an ownship position/velocity estimate with explicit source times and model uncertainty, and follows A* routes with persistent progress and bounded speed. Estimates are not fresh measurements. Radar row order is not an object identity, and unknown sideways debris motion remains an uncertainty allowance.

Cruise speed increases only when a longer modeled stopping corridor clears the inflated observed obstacles. That lookahead does not extend a command lease or make old sensor readings fresh. Close approaches retain the shorter route checks; losing usable data still requests coasting, not automatic braking.

The backup radar samples the actual physical world with a separate noise stream. Both radars still share the simulated radar bus and physical environment. Navigation telemetry is one unreplicated direct-state source. The two RGB cameras share GPU dependencies and do not supply navigation redundancy without a vision estimator.

Open **Resilience** to fail one unit, the shared radar bus, navigation delivery or all avionics. Choose unavailable or frozen delivery and a duration, then select **Apply sensor failure**. These are bounded simulation-time delivery failures, not cloud-region outages or replacement sensor results. Frozen samples keep their original timestamps. An all-avionics blackout also withholds public navigation and physics telemetry from the controller. **Restore all sensor delivery** clears the faults without resetting the world.

An explicit resilient Run starts a separate Python standby process. Native epoch/term checks allow one command writer. The standby can claim expired leadership using the original Run consent, but cannot undo manual pause, placement, reset or release. It uses checked-in algorithms and hot data settings, not unsaved notebook callbacks. **Interrupt primary decisions** withholds the primary's decisions and renewals for the selected simulation duration so actual expiry and takeover can be observed. Both processes share this host; this is not regional redundancy.

Loss of usable data requests zero thrust. Recovery needs advancing authentic samples and a new checked route; polling a held sample cannot count as recovery. The native watchdog independently expires autonomous commands if Python blocks, exits or loses its connection. The default lease is 0.75 simulation seconds, with a native maximum of one second, anchored to the original decision input rather than network arrival. Controller authority also expires on the actual runtime clock, including paused frames. Reads and preview heartbeats never renew it. Use a controller cadence faster than its lease interval. Slow computation or higher playback rates can cause expiry rather than prolonged stale thrust.

Zero thrust means coasting, not braking, hovering or a guaranteed collision-free orbit. The current radar covers a bounded sampled subset of the debris field, not every body in a large world. These controls exercise failure handling in the local model; they do not establish competition-runtime parity or complete hardware redundancy.

## Understand the camera views

The viewing camera is for us. The mounted ship cameras are sensors with their own limits. In this practice setup they capture real 512×512 RGB images with a fixed 60-degree field of view and fixed exposure.

Viewer lighting, stars, route guides, instrument panels and exhaust effects do not enter those captured pixels. A night-side scene can therefore be readable in the viewer and dark in a ship-camera capture. Use the **Cameras** tab to aim and capture the sensors; zooming the viewer does not improve their optics.

Phobos and Mars use [credited spacecraft surface maps](worker/web/assets/ATTRIBUTION.md). These help us recognise the setting; they are not a claim that our terrain or camera rendering exactly matches the official simulator.

## Compare runs and share what you learn

Use the same launch setup, seed, world settings, ship calibration and run schedule when comparing two policies. Change one thing at a time where possible, and record deliberate differences.

Look at arrival time, arrival speed, fuel and damage together. A fast arrival with excessive speed or damage is not necessarily a better strategy. History retains measurement timestamps, leaves missing readings missing and separates resets or interventions instead of drawing a false continuous flight.

For notebook experiments, save Python edits, open **Export** and select **Prepare downloads**. The generated download buttons appear below the tabs:

- **Experiment JSON** records settings, measurements, interventions and the engine version.
- **Measured history CSV** is for comparing readings in a spreadsheet or analysis script.
- **Saved Python notebook** lets a teammate inspect and repeat the saved code.

Prepare downloads again if you need newer readings. History contains at most 2,000 received rows, not every internal physics step.

The game's **Details → Export** is a separate full-state JSON download. It includes world details the ship's sensors may not have observed. Do not use those hidden details to give a competition policy an unfair view of the debris.

Add a short explanation to our [shared mission notebook](notebook/NOTEBOOK.md): what you tried, why, the setup, what happened and what to try next. Keep the exported run and saved code with the explanation. Historical Lunar SAR and rover experiments remain useful references; they are not the current Phobos challenge.

## Work safely as a team

Each teammate can run their own local game and notebook from the private repository. Sharing the repository does not mean that everyone's browser controls the same physical world. Use private screen sharing when demonstrating a run.

Keep editor access links, tokens and `.devenv/wsorb-local.json` private. The Python editor can execute code on its host. Remote access needs an owner-approved authenticated Tailscale Serve setup, not a public editor or Tailscale Funnel.

Use Jujutsu through the repository's Just recipes for changes and review. `just check` and `just unit-check` are the existing validation commands. Keep the repository private and Actions disabled. Publishing a Cloudflare release remains an owner-controlled action.

## What local practice does not prove

Our practice world uses simplified gravity, terrain, contact, fuel and damage models. Its scores are not official competition scores, and a local success does not establish equivalent behaviour in Space Teams PRO.

The normal command request limits are 0.5 m/s² and 0.05 rad/s². Local thrust-strength controls, placement, pause/step controls and the look-ahead ray are experiment tools, not additional official SDK capabilities. The ray is a debugging aid, not radar or a route-safety guarantee. The planner uses public ship observations; missing radar readings do not mean that space is empty.

Validate promising policies with the real licensed runtime and its sensors, actuators and scoring. The [SDK adapter](src/wsorb/stp_api.py) and [controller example](examples/stp_controller.py) use the authentic STP calls reviewed against the pinned official UserAPI and Controller sources. The local HTTP API is a separate validation transport, not a replacement `spaceteams` SDK. Local pause, step, fault injection and controller leases are not exported as STP capabilities.

### Send the controller to a colleague

Send `examples/stp_controller.py` with these five files from `src/wsorb` beside it: `stp_api.py`, `stp_policy.py`, `control_contract.py`, `maneuvers.py` and `tracking.py`. This subset does not import the local engine, `flight/` or the historical package. It targets Python 3.10+ syntax and requires compatible NumPy and the server's genuine SDK. Source checks do not prove SDK binary or interpreter compatibility.

Keep the server's existing ControllerSystem launch arguments unchanged. Start with no policy configuration, or this disabled configuration selected by `WSORB_STP_POLICY_JSON`:

```json
{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false}
```

That runs a read-only SDK probe. It reports actual versions, resolved handles, frames and source clocks without writing even zero commands. Unknown SDK/server versions remain unknown. The colleague should return that output before enabling flight control.

For an enabled checkpoint attempt, select `policy: "checkpoint_velocity"` in the reviewed configuration. Set an independently established `radar_clock_offset_s`, verify `checkpoint_radius_m` against the server's Scoring `KeepOutRadius_m`, and ensure this is the only jet-command writer. Select `body_axis_preference` explicitly for the candidate policy. Do not infer the radar offset from one reading or pass our policy options as SDK bootstrap arguments. The legacy PD/radar policy remains available for comparison.

The portable controller uses named reference frames, original measurement times and bounded P velocity control. It does not add unreviewed integral/derivative gains or initial attitude changes from the meeting discussion. Anonymous radar tracks carry covariance and tentative/confirmed/coasting states, but their IDs are hypotheses and their uncertainty models are not calibrated probabilities. Tracking cannot remove raw obstacle envelopes or certify clearance. Body-command L1 is an allocation proxy, not measured fuel or proof of the minutes' diagonal-fuel claim; the local centre-of-mass actuator charges resultant L2 force.

SDK state/radar reads and the two command writes are sequential. The script rechecks the actual clock after computation, but STP has no native decision lease here. Cleanup attempts real zeros only after explicit enabled control; it cannot guarantee a stop after process death, blocked calls or failed writes. A configured-radius observation is not an official scoring result or a physics freeze. Authentic licensed-server execution is still required. Missing SDK access fails clearly rather than returning substitute results.

For technical details, use the [API and measurement reference](notebook/entries/20261023T142145Z-stp-api-flight-metrics-and-practice-thrust.md), the adapter source and the official resources below. Check how each simulator defines directions and timestamps before moving a policy between them.

## If something looks wrong

| Symptom | Check |
| --- | --- |
| Nothing moves | Is the simulation clock paused? Use Play/P. Stored velocity is not movement while time is held. |
| A thrust-strength edit does not move the lander | Check playback and requested thrust. A completed mission needs Reset before another flight. |
| The notebook will not connect | Start `lab simulation`, wait for the game, open Notebook from that game and select Connect / Refresh. A game-only server is not the live lab connection. |
| A run finished and Run is refused | Use Reset flight. Editing parameters does not revive a completed result. |
| Mars is missing from the close view | Use Overview. Phobos blocks Mars from the far-side pad; viewer lighting cannot remove that obstruction. |
| WebGPU canvas unavailable | Use **Copy startup diagnostics** on the error screen. It records the actual failure without calling the failed renderer. Restart or update Safari Technology Preview and reopen the page if needed. The app does not silently fall back to another renderer. |
| Loading stalls or the view fails | Copy the startup diagnostics and share the exact message. `just local-check live` reads the connected game's status without a flight command; `just local-check load` checks loading the game code, not browser graphics. |
| Stop could not be delivered | Use P in the game and confirm its clock is paused. A failed connection cannot guarantee a stop request reached the browser. |

## Find the relevant code

| File or folder | What belongs there |
| --- | --- |
| [`notebooks/flight_lab.py`](notebooks/flight_lab.py) | Team-facing experiment controls, editable Python functions and analysis. |
| [`src/wsorb/flight/`](src/wsorb/flight/) | Shared prediction, geometry, path-search and controller functions. |
| [`src/wsorb/local_api.py`](src/wsorb/local_api.py) | The notebook's connection to the actual local game. |
| [`simulation/scenarios/`](simulation/scenarios/) | Versioned default world and vehicle settings. |
| [`simulation/src/`](simulation/src/) | Game physics, sensors, instruments and rendering. Python still owns autonomous decisions. |
| [`src/wsorb/stp_api.py`](src/wsorb/stp_api.py) | Connection to the real licensed Space Teams PRO runtime. |

## Team members

| Member | Responsibility | Reference |
| --- | --- | --- |
| To be added | Mission planning | [Simulation structure](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Sim-Structure) |
| To be added | Python and automation | [Python systems](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python) |
| To be added | Testing and analysis | [Simulator debugging](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Python-System-Debugging) |
| To be added | Team coordination | [Shared mission notebook](notebook/NOTEBOOK.md) |

## Competition resources

- [Official announcements and discussions](https://github.com/SimDynamX/SpaceTeamsPRO/discussions)
- [Escape from Phobos rules](https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218)
- [Competition simulation](https://github.com/SimDynamX/STU4-Sim)
- [Controller starter and mission API](https://github.com/SimDynamX/STU4-Solution/wiki)
- [Space Teams PRO documentation](https://github.com/SimDynamX/SpaceTeamsPRO/wiki)
- [Running code in the simulator](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python)
- [Python API](https://simdynamx.github.io/stp-python-api-docs/)
