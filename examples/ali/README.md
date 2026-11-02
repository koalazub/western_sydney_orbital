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
just flight-sweep examples/official_comparison.json runs/ali-official-new
```

The published comparison was generated before the owner name was made explicit,
so its rows and configuration call the same policy `wso_official_profile`. The
current name is `ali_official_profile`; the old key remains an alias for
reproduction.

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

The pure supporting modules from the 26–29 October minutes are in
`src/wsorb/flight/`:

- [`axis_avoidance.py`](../../src/wsorb/flight/axis_avoidance.py) — single-axis
  preference and the L1 fuel proxy. The cited 41% is the two-axis geometry at
  equal resultant acceleration, not a validated propellant measurement.
- [`pid.py`](../../src/wsorb/flight/pid.py) — bounded PID velocity control with
  anti-windup and derivative filtering. No final gains are selected.
- [`tracking_filter.py`](../../src/wsorb/flight/tracking_filter.py) — pure
  alpha-beta and alpha-beta-gamma tracker math for the 29 October filter
  discussion. It does not claim a validated 300 s forecast.

These modules are intentionally not wired into the proven official-profile
arrival path until their gains and weights are validated in a matched sweep.
The requirements matrix is in
[`minutes/REQUIREMENTS_AUDIT.md`](../../minutes/REQUIREMENTS_AUDIT.md).

## STP boundary

The 6/6 controller is local-only. It has not been ported to the licensed
`spaceteams` SDK. The portable STP handoff remains
[`../wso/stp_controller.py`](../wso/stp_controller.py); do not describe this
local policy as an STP result.
