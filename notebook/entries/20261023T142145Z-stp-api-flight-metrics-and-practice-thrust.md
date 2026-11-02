+++
id = "20261023T142145Z-stp-api-flight-metrics-and-practice-thrust"
title = "STP strategy API, flight metrics and practice thrust"
created = "2026-10-23T14:21:45+00:00"
author = "Engineering assistant"
kind = "reference"
tags = ["escape_from_phobos", "api", "telemetry", "practice_limits"]
+++

# STP strategy API, flight metrics and practice thrust

## Historical guidance, supersession recorded 2026-10-28

The original research body below is preserved unchanged. Its Space practice-boost selector and profile-selection instructions are superseded. Beads `wso-sal.41.5`, completed 2026-10-24, removed live boost selection. Space and E now share the 0.5 m/s² reference command cap; normal angular requests remain capped at 0.05 rad/s². Local actuator gain is a separate calibration, not the retired boost selector or a change to those command limits. See the [current README controls](../../README.md#play-directly-in-the-game), [live calibration](../../README.md#edit-inputs-and-inspect-the-result) and [practice limits](../../README.md#what-local-practice-does-not-prove). The boost acceptance criteria in `wso-sal.32` describe the old implementation, not the current UI.

This reference checks the published official sources. It does not establish licensed-runtime execution or exact parity between this Bevy/Rapier practice model and Space Teams Pro.

## Thrust profiles

The official competition configuration sets maximum linear acceleration to **0.5 m/s²** and maximum angular acceleration to **0.05 rad/s²**. The actuator clamps each vector's norm independently before allocating jets. The starter's example request of 1 m/s² does not override that clamp. See [configuration](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Sims/COMPETITION_SIM.simconf#L38006-L38010) and [actuator clamping](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py#L268-L294).

The local Space practice boost is **2.0 m/s²**, four times the reference limit. It applies only to positive lift requested with Space, not to E alone, sideways keys, the debug camera or the normal command API. It is a local training assist, not a valid competition thrust setting. Select reference Space thrust in Details and reset before comparing runs.

Reference thrust is not a claim of full STP-equivalent dynamics. Local terrain, contact damage, gravity, manual nozzle positions, fuel accounting and rendering still need separate validation.

## Verified official calls

Use the official starter's connection/bootstrap and entity handles. In the table, `P = phobos.GetBodyFixedFrame()` and `B = shuttle.GetBodyFixedFrame()`.

| Purpose | Published Python call | Units and meaning | Source |
| --- | --- | --- | --- |
| Command actuation | `SetCommand(accelCommand, angAccelCommand)` | Two NumPy 3-vectors, body-frame m/s² and rad/s². Not newtons or normalized throttle. | [UserAPI L53–55](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/UserAPI.py#L53-L55) |
| Position and velocity | `s = shuttle.getLocVelAcc()`; `s.loc_WRT_ExprIn(P)`; `s.vel_WRT_ExprIn(P)` | Position in metres and velocity in m/s relative to and expressed in the chosen frame. These are the calls in Controller.py; UserAPI.py also shows a `locvel()` wrapper. | [Controller L104–108](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L104-L108) |
| Attitude | `shuttle.getRotation().Quat_WRT(P)` | Frame-qualified quaternion. Its convention must be checked before conversion to local render/physics poses. | [Controller L104–108](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L104-L108) |
| Angular velocity | `shuttle.getAngVelocity().WRT_ExprIn(P)` | rad/s, relative to and expressed in the chosen frame. | [Controller L104–108](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L104-L108) |
| Mass and inertia | `shuttle.getMass()`; `shuttle.getInertiaTensor(B)` | kg and a body-frame 3×3 tensor in kg·m². | [Controller L114–117](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L114-L117) |
| Fuel and damage counters | `shuttle.GetParam(st.VarType.double, ["Stats", "FuelUsed"])`; corresponding `"Damage"` | Official counters, labelled kg and J by scoring. Do not assume the local estimates use the same accumulation formula. | [Controller L114–117](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L114-L117), [scoring labels](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Scoring.py#L99-L102) |
| Target position | `target.getLocVelAcc().loc_WRT_ExprIn(P)` | Target position in the same reference frame. | [BasicController L199–201](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/BasicController.py#L199-L201) |
| Radar | `GetNearestDebrisLocations(N)` | List of 3×3 matrices, described below. | [UserAPI L36–40](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/UserAPI.py#L36-L40) |
| Camera aim | `PanTiltCamera(camera, yaw_deg, pitch_deg)` | Degrees. Yaw ±89°; top pitch −50° to 0°; bottom 0° to 50°. | [UserAPI L43–50](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/UserAPI.py#L43-L50) |
| Camera capture | `st.camera.CaptureImageBlocking(camera, properties)` | A captured image or `None`; the SDK documents timestamps and RGB channel access. | [official example](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L126-L145), [generated SDK](https://simdynamx.github.io/stp-python-api-docs/autoapi/spaceteams/camera/) |
| Simulation time | `st.SimGlobals.SimClock.GetTimeNow()` | `st.timestamp`; the starter subtracts `StartDateTAI` after converting both with `as_datetime()`, then calls `total_seconds()`. Sleeping is not a simulation-step API. | [Controller L75–98](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/Controller.py#L75-L98) |
| Published actuator effects | `shuttle.getPhysEffects()` | SDK documents a dictionary of physical effects. The actuator writes `ThrusterForce` and `ThrusterTorque`, plus `JetSelection/ResultantAccel` and `ResultantAngAccel` parameters. These are not proven net/contact-force sensors. | [SDK](https://simdynamx.github.io/stp-python-api-docs/autoapi/spaceteams/#spaceteams.Entity.getPhysEffects), [actuator output](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py#L309-L329) |

`SetCommand` writes `JetSelection/CommandedAccel` and `CommandedAngAccel`. The configured actuator uses jet allocation with `DirectCommand: false`, and tries to satisfy both translation and angular requests. Our local keyboard nozzles deliberately produce unbalanced turning moments. They are not the official jet allocator. The local normal `set_command` operation is the closer command-level comparison, but still not proof of identical dynamics. See [allocation code](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py#L268-L329) and [configured mode](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Sims/COMPETITION_SIM.simconf#L41753-L41765).

### Frames and quaternions

The SDK documents Hamilton scalar-last `xyzw` quaternions with passive reference-to-body meaning. Local Rapier/Bevy poses use active body-to-world rotations. Do not copy quaternion components between them without verifying reference frames and conversion direction. Prefer frame-qualified SDK conversions such as `st.frames.FramedVector(a_P, P).ExprIn(B)`. See [SDK quaternion convention](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/The-Entity/bdc04c10a50a450e20ea5647cef3264373c1e88f#necessary-for-all-propagators-besides-no_body) and [starter frame conversion](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/BasicController.py#L207-L208).

### Radar timing and visibility

Each public matrix has rows:

1. Relative position expressed in the Phobos frame.
2. Closest range, scalar range rate, diameter. Positive range rate means receding.
3. Measurement timestamp, zero, zero.

There are no actor IDs or full debris velocities in this interface. Row ordering follows noisy centre distance and is not a stable tracking identity. Published noise is 2 m for position/closest range, 0.02 m/s for range rate, and 5% for diameter. See [Radar L76–108](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Radar.py#L76-L108).

The checked-in configuration scans 200 actors, despite wiki wording about up to 500. The requested 120 Hz loop updates one row per iteration and republishes every 20 rows: nominally a 1.67 s sweep and 6 Hz publications, not a verified runtime guarantee. Readings within one publication can have different ages. Their timestamp origin is radar-loop startup, so align clocks before comparing to mission elapsed time. Never replace measurement time with read time. See [configuration](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Sims/COMPETITION_SIM.simconf#L41781-L41803) and [radar loop](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Radar.py#L113-L156).

512×512, FOV 60° and EV10 are explicit starter capture examples. This audit does not establish mandatory optics, FOV axis/full-angle semantics, or equivalence to local Bevy EV100 15. Camera shadows, occlusion and backgrounds still limit useful detections.

### Fuel and score caveat

The official actuator contains Isp 80 and g₀ 9.8, but its fuel accumulation uses acceleration activity without the local mass/timestep factors. Local SI propellant estimates therefore must not be presented as verified STP fuel counters. The official score implementation also differs from the wiki's verbal formula. Use observed official counters/scoring for competition ranking. See [fuel calculation](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py#L69-L126) and [score calculation](https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Scoring.py#L56-L62).

## Local practice boundary

The repository now also contains a separate [SDK-backed adapter](../../src/wsorb/stp_api.py) and [ControllerSystem entry point](../../examples/wso/stp_controller.py). Its `SetCommand`, public radar, frame-qualified state, pan/tilt and capture functions delegate to the actual corroborated `spaceteams` methods. It imports the licensed SDK only when connecting, fails clearly when it is unavailable and contains no fake SDK backend. The entry point requests public radar rows and sends a zero-command placeholder until a reviewed policy replaces it. Syntax, lint and pure vector-validation checks do not establish live STP execution. The [README](../../README.md) explains runtime configuration and the single-command-writer requirement.

These names belong to our WASM facade. They are not aliases for official SDK calls.

| Local entry point | Use |
| --- | --- |
| `observation()` | JSON with own lander/target state, environment, mission and the public radar matrices. Use this for planner inputs. |
| `telemetry_status()` | Read-only JSON of real local flight metrics with units/frame and limitations. HUD metrics and export use this report. |
| `navigation_status()` | Read-only advisory path report. It does not command the lander. |
| `command(JSON.stringify({op:"set_command", accel:[0,0,0.5], angular_accel:[0,0,0]}))` | Reference-limited body acceleration command, analogous in units to the starter helper but implemented locally. |
| `command(JSON.stringify({op:"stop"}))` | Clears commanded thrust/torque. It does not erase linear or angular momentum. |
| `reset`, `restore`, `step`, `play`, `pause`, `view` operations | Local scenario/control facilities. This audit does not establish corresponding official SDK methods. |
| `ship_camera_capture("top")` / `"bottom"` | Local genuine RGB8-per-channel feed with frame metadata, not an official `CapturedImage` object. |

The local `observe` operation also returns a full debug snapshot with hidden debris positions and velocities. That export is useful for offline evaluation, but those hidden fields must never become planner observations. The planner remains radar-limited until real camera/tracker fusion supplies more observations.

Local manual stepping is synchronous physics, not real STP stepping. A queued pause takes effect on an engine tick; ensure runtime is paused before issuing a local step. HUD/telemetry publication follows the engine update, while `observation()` reads current physics. Do not log an old report as a fresh measurement after blocking the browser in a synchronous loop.

## Strategy comparison procedure

1. Select reference Space thrust and reset. Record seed, selected profile, scenario version and frame. Do not mix boosted takeoff results with reference-limit controller results.
2. Use the same seed and simulation-step schedule for paired strategy comparisons. Drive the local normal command API with acceleration vectors, not force values in newtons.
3. Log simulation time, rotating-frame speed, radial climb, terrain-centre clearance, centre range/closing speed, actual thrust acceleration/force/torque, angular speed and peak values. Treat local fuel/damage as estimates.
4. Use only allowed observations. Preserve radar timestamps, reject stale data and keep unobserved rocks explicitly unknown. A match gate or an empty radar row is not a collision-safety guarantee.
5. Compare time, fuel, damage and arrival conditions together. Export includes telemetry, the selected profile and the verified engine-build SHA-256 so runs can be traced to their build. Do not judge only by the debug camera view.
6. Validate the selected policy in licensed Space Teams Pro using the verified interfaces above. Check frame conversion, clocks, jet allocation, sensor visibility and official scoring there. Do not infer exact STP parity from the local game or from plots.

Published source revisions checked: STU4-Solution `8545c085426a71c39cbe1d544a844b3edb757007`; STU4-Sim `c1e3fc8f9ee3b189bba64d8bd87818a8003639cc`. SDK documentation links are live documentation, not pinned runtime evidence. Generic `GetPose`, `Data.Manager`, browser telemetry getters and fixed-step calls are not asserted as STP interfaces here.
