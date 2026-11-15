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

[`controller_mppi.py`](controller_mppi.py) is the single-file STP submission.
The marked core is loaded by the local adapter and numeric tests; the shell only
reads the simulator, calls the core, logs telemetry and writes the command. The
competitor region is currently 231 lines under a temporary 250-line guard while
the policy settings are parameterized; the original 200-line target is the later
optimization checkpoint.

The policy derives a cruise reference from the job's actual time budget and the
initial target range. The settings are data: `cruise_fraction` (default 0.5) is a
starting point for `wsorb tune`, not a fixed answer. The core uses a mass-flow
fuel estimate, a 48 x 40 MPPI rollout with a remaining-time horizon, a burn/coast
governor with 0.50/0.30 m/s hysteresis, a control-switch penalty, capsule
clearance, a braking-curve attitude rate target with a terminal hold, and an
omnidirectional raw-radar emergency guard. The score coefficients are one
configuration; the run supplies its duration and the native arrival radius.
Plan steering is off by default; when enabled, it follows the MPPI direction
only if the predicted capsule risk exceeds `plan_risk_threshold`.

Body +X is pointed at the stable nominal velocity, so the initial ~90° slew puts
the main engine on the transfer direction. `main_engine_gate_rad` can hold
translation until the main engine is within that angle of the path. It is off by
default because the local score rewards thrusting during the slew; under the
prior guard, a 15° gate on seed 101 scored 36.45 vs 54.14 without it, so it
remains opt-in for visual or STP-specific runs.

Scoring checks only the 50 m region, not arrival speed. The terminal controller
coasts inside `coast_range_m` at >=`coast_speed_mps` when the raw-radar emergency
is clear instead of spending fuel to stop. The current safe default is
`cruise_fraction=0.5` with a 180 m / 7 s / 6 m / 3.5 m/s emergency guard: the
official 300 s fuzz over seeds 100-107 is 44.44-72.90 points (mean 64.63), all
eight native successes and no damage. Seed 100 is 162.5 s, 49.7 m, 8.25 m/s,
38.47 kg, 0 J, 72.58 points. Held-out seeds 200-203 are 45.51-72.81 points (mean
59.07); seed 200 is 45.74 points with 3,027 J of damage, inside the official
209,200 J limit. This is a local practice estimate, not an STP receipt.

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

Fuzz the reviewed profile across seeds on separate owned native engines:

```sh
uv run --locked wsorb fuzz --seeds 100-107 --workers 4
```

Each seed gets a fresh official 300 s world and its own headless server. The
command prints one row per seed and the min/mean/max points; it writes the
per-seed JSON report under `runs/`. There is no fixed pass threshold by default:
exit status is zero only when every seed reports native success and a usable
score. `--threshold` is available only when a caller wants a reporting floor.

To search policy settings against the native score, use the max-min objective:

```sh
uv run --locked wsorb tune \
  --grid '{"cruise_fraction":[0.48,0.5,0.52]}' \
  --seeds 100-103 --validation-seeds 200-203 --workers 4
```

`wsorb tune` ranks candidates by the worst seed score, requires native success
and the official damage limit, and validates the best candidate on held-out
seeds. It writes the ranked settings and per-seed measurements under `runs/`.

The first max-min tune pass on the two binding seeds chose `cruise_fraction=0.48`
over 0.5 and 0.52: worst-seed 59.33 vs 53.54 and 51.74, all with zero damage. The
full 100-107 run at 0.48 was better (59.83-74.52, mean 69.89, zero damage), but
the held-out seed 200 took 60,575 J of debris damage and scored -49.23, so 0.48
was rejected. The emergency-guard search then found 180 m / 7 s / 6 m /
3.5 m/s: seeds 100-107 scored 44.44-72.90 (mean 64.63) with no damage, and
held-out seeds 200-203 scored 45.51-72.81 (mean 59.07); seed 200 scored 45.74
with 3,027 J, inside the official damage limit. This is the current default. The
47 figure is not the objective; the search uses the worst seed score with native
success and the official damage limit. A 9.3 m/s trial also damaged seed 102, so
it was not kept.

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
