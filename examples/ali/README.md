# Ali's controller

## Current competition MVP

[`controller.py`](controller.py) is the self-contained SDK entrypoint. Its
marked pure-core section keeps tracker, planner and emergency state in plain
mappings and returns new data; the loop only reads the simulator, calls `step`,
and writes the command and telemetry. The local adapter extracts that marked
section without importing the SDK.

It uses polyfit velocity tracking, a conservative tracked-rock planner that
waits for a 10 s activation horizon, and a raw-radar emergency check inside
150 m and the forward 180°. It records the maximum observed diameter per rock,
including a small margin, rather than smoothing the radius downward. No camera
is used.

While the ship is first accelerating, the controller points body +X at the
commanded burn with bounded side-thruster angular acceleration. Linear thrust
continues during the pivot, then attitude is held once aligned, so the
sustained burn uses the rear thruster and later avoidance dodges translate
without continuous pivoting. Pivoting is cheap against the fuel penalty; the
new compilation reports a ~220 s belt traverse. The final local verification
run arrived in 228.8 s with 102.1 kg fuel and 0 damage. The local pivot cost
about 1.8 kg against the no-pivot baseline; the team's 0.1 kg figure is the
competition-runtime observation, not a local measurement.

The reviewed local profile is `ali_radar_mvp`:

```sh
uv run --locked wsorb controllers run --isolated --profile ali_radar_mvp --run --duration-s 300 --wall-budget-s 400
```

## MPPI candidate

[`controller_mppi.py`](controller_mppi.py) is the SDK shell and
[`mppi_core.py`](mppi_core.py) is the same marked core kept as a standalone copy
for analysis and numeric tests. The shell only reads the simulator, calls the
core, logs telemetry and writes the command.

The policy uses a 7 m/s cruise target with a terminal braking profile, a
burn/coast governor with 0.50/0.30 m/s hysteresis, a 48 x 40 MPPI rollout, a
control-switch penalty, capsule clearance, a braking-curve attitude rate target
with a terminal hold, and an omnidirectional raw-radar emergency guard. The
governor exists to stop the previous continuous micro-burns.

The shell emits one JSON attitude record every 0.5 s through `st.logger_info`,
plus a compact line through `st.OnScreenLogMessage`. The record contains
alignment, body rate, the PD terms, command saturation, the active gate,
burn-direction error, translation-direction error, track count, range, fuel and
damage. `st.OnScreenAlert` is reserved for sustained coast drift. The local
adapter exposes the same record under `guidance.telemetry`, and
`wsorb controllers run --progress` prints it once per second on stderr. No CSV is
written.

The local profile is `ali_mppi`:

```sh
uv run --locked wsorb controllers run --isolated --profile ali_mppi --run --duration-s 60 --wall-budget-s 120
```

A local `--connection-file` run requests the follow view automatically when the
browser renderer is ready. The request is presentation-only and never changes the
flight.

The official-profile material below is the earlier local experiment, not the
current competition base.

## Official-profile experiment

Ali's named contribution is the **official-profile** controller: the local
1.31 km competition-reference beeline with fresh-radar avoidance. It is the
policy that produced the 6/6 local official-cloud comparison.

## Entrypoint

[`official_profile.py`](official_profile.py) is the named entrypoint. The
policy factory lives in
[`src/wsorb/controllers/official_profile.py`](../../src/wsorb/controllers/official_profile.py)
and the flight math lives in
[`src/wsorb/flight/official_profile.py`](../../src/wsorb/flight/official_profile.py).

Run the matched comparison:

```sh
uv run --locked --extra lab python examples/flight_sweep.py --config examples/official_comparison.json --output runs/ali-official-new
```

Ali is the owner of this profile, not the automatic qualifier submitter. The
repository collects the profiles so the team can compare and combine them
deliberately.

The published comparison was generated before the owner name was made explicit,
so its rows and configuration call the same policy `wso_official_profile`. The
current name is `ali_official_profile`; the old key remains an alias for
reproduction.

## STP source port

[`stp_official_profile.py`](stp_official_profile.py) is the licensed-runtime
source port. The implementation is
[`src/wsorb/stp_official.py`](../../src/wsorb/stp_official.py). Configure it with
`WSORB_STP_OFFICIAL_JSON` or `WSORB_STP_OFFICIAL_FILE`; missing configuration
does not connect or write. Start with the read-only probe:

```json
{"schema":"wsorb.stp_official","schema_version":1,"enabled":false,"probe":true}
```

Enabled runs require an independently established `radar_clock_offset_s`. The
linear and angular caps stay 0.5 m/s² and 0.05 rad/s². This port has not been
executed against a real STP server; run the probe first and keep one command
writer. The portable checkpoint controller in [`../wso/`](../wso/) remains the
other STP handoff.

## Result

Six attempts on three seeds with two consecutive attempts each:

- 6/6 arrivals
- 153.2 s median arrival
- 1.20 m/s median final speed
- 109.0 kg median local fuel estimate
- two damage observations, both below the 209,200 J threshold

This is local practice evidence, not an STP score or a docking claim.

## Relationship to the other controllers

- [`../wso/`](../wso/) is the portable STP checkpoint controller and its
  `baseline`/`body-axis` local profiles.
- [`../bui/`](../bui/) and [`../tom/`](../tom/) are the teammate STP entrypoints.
- [`../harry/`](../harry/) is the shared basic PD/radar profile.

## Meeting-minute work

The pure supporting modules from the October control and estimation minutes are in
`src/wsorb/flight/`:

- [`axis_avoidance.py`](../../src/wsorb/flight/axis_avoidance.py) — single-axis
  preference and the L1 fuel proxy. The cited 41% is the two-axis geometry at
  equal resultant acceleration, not a validated propellant measurement.
- [`maneuver_objective.py`](../../src/wsorb/flight/maneuver_objective.py) —
  caller-weighted clearance/progress/fuel objective, Pareto frontier and a
  labelled floor-penalty proxy.
- [`pid.py`](../../src/wsorb/flight/pid.py) — bounded PID velocity control with
  anti-windup and derivative filtering. No final gains are selected.
- [`tracking_filter.py`](../../src/wsorb/flight/tracking_filter.py) — pure
  alpha-beta and alpha-beta-gamma tracker math for the 29 October filter
  discussion. It does not claim a validated 300 s forecast.
- [`calibration.py`](../../src/wsorb/flight/calibration.py) — frame-labelled
  bias/scale, residual covariance, process-noise and covariance utilities.
- [`association.py`](../../src/wsorb/flight/association.py) — bounded gated
  association and per-target track lifecycle, not optimal multi-hypothesis
  tracking.
- [`attitude_bias.py`](../../src/wsorb/flight/attitude_bias.py) — optional
  shortest-arc body +X bias and bounded rest-to-rest slew plan.
- [`event_camera.py`](../../src/wsorb/flight/event_camera.py) — offline event
  generation, clustering and pinhole calibration prototype.
- [`control_validation.py`](../../src/wsorb/flight/control_validation.py) —
  synthetic PID step, gamma and 300 s forecast-error harness.

These modules are intentionally not wired into the proven official-profile
arrival path until their gains and weights are validated in a matched sweep.
The requirements matrix is in
[`minutes/REQUIREMENTS_AUDIT.md`](../../minutes/REQUIREMENTS_AUDIT.md).

## STP boundary

The [Python theory review](../../docs/python-flight-theory.md) maps the sweep
script to the actual flight loop, reviews the reference modules, and explains
why the STP source port is not equivalent to the local avoidance/braking path.
Use the [recorded path comparison](https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/compare.html)
to inspect a matched seed and repetition without changing a flight.

The 6/6 comparison is local evidence. A source port now exists in
[`stp_official_profile.py`](stp_official_profile.py), but it has not been
executed against the licensed runtime. The portable checkpoint handoff remains
[`../wso/stp_controller.py`](../wso/stp_controller.py). Do not describe either
source port as an STP score until the teammate has run the probe and flight.
