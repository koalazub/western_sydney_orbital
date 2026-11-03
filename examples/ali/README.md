# Ali's controller

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
