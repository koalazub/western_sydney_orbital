# Requirements audit from the control minutes

Sources:

- [20 October: state estimation and target tracking](2026-10-20_state_estimation_target_tracking.md)
- [26 October: spacecraft control and collision avoidance](2026-10-26_spacecraft_control_meeting_minutes.md)
- [28 October: shuttle control and avoidance planning](2026-10-28_shuttle_control_avoidance_planning.md)
- [29 October: asteroid filtering and qualifier preparation](2026-10-29_asteroid_filtering_qualifier_preparation.md)

This audit records what is implemented, what is deliberately only a pure
reference, and what still needs a decision. It does not turn an unvalidated
meeting suggestion into a competition claim.

## Priority path to the station

1. Keep the proven local official-profile controller intact. It is the only
   local policy with 6/6 official-cloud arrivals in the matched comparison.
2. Select and validate the qualifier entrypoint in the real STP runtime. The
   local result is not STP evidence.
3. Keep the new axis, PID and alpha-beta-gamma work as pure reference modules
   until objective weights or gains are agreed and a matched sweep shows they do
   not degrade arrival.
4. Do not present the 41% diagonal figure, a 300 s forecast or an event-sensor
   capability as validated.

## Named ownership

| Owner | Contribution | Entrypoint / implementation |
| --- | --- | --- |
| Ali | Official-profile local controller and the 6/6 comparison | [`examples/ali/official_profile.py`](../examples/ali/official_profile.py), [`src/wsorb/controllers/official_profile.py`](../src/wsorb/controllers/official_profile.py), [`src/wsorb/flight/official_profile.py`](../src/wsorb/flight/official_profile.py) |
| WSO | Portable STP checkpoint controller; local `baseline` / `body-axis` | [`examples/wso/stp_controller.py`](../examples/wso/stp_controller.py), [`src/wsorb/stp_policy.py`](../src/wsorb/stp_policy.py) |
| Bui | Alpha-beta tracker and sidestep controller | [`examples/bui/`](../examples/bui/), adapter [`src/wsorb/controllers/_bui.py`](../src/wsorb/controllers/_bui.py) |
| Tom | Kalman tracker and velocity governor; UserAPI template | [`examples/tom/`](../examples/tom/), adapter [`src/wsorb/controllers/_tom.py`](../src/wsorb/controllers/_tom.py) |
| Harry | Basic PD/radar and coast baselines | [`examples/harry/README.md`](../examples/harry/README.md), [`src/wsorb/flight/control.py`](../src/wsorb/flight/control.py) |

The minutes do not name Ali or assign the qualifier submission. Ali is the
named owner of the official-profile contribution in this repository; the
qualifier submission owner still needs an explicit team decision.

## Requirement matrix

| ID | Minute requirement | Current state | Evidence / next step |
| --- | --- | --- | --- |
| R1 | Consistent reference frames for ship and targets | Implemented locally | `flight/estimation.py`, `prediction.py`; frame-qualified observations |
| R2 | Model and propagate process/measurement uncertainty | Partial | Model allowances in `guidance.py`; not calibrated probabilities |
| R3 | Estimate ship and obstacle motion | Implemented locally | `StateEstimator`, `predict_obstacles`, radar propagation |
| R4 | Multi-target lifecycle and association | Partial | Bui alpha-beta and Tom Kalman adapters; no common hypothesis manager |
| R5 | Per-target track ageing | Partial | Bui `DebrisTracks` expiry; thresholds remain local assumptions |
| R6 | Event-based / neuromorphic sensing | Not implemented | No event sensor in the local model; documentation only |
| R7 | Camera calibration and astronomical positioning | Not implemented | Local cameras are not a calibrated star tracker |
| R8 | Select estimator, noise, association and update rules | Open | The minutes record no decision; do not invent one |
| R9 | Prefer single-axis avoidance when safe | Reference implementation | `flight/axis_avoidance.py`; not wired into the proven policy |
| R10 | Balance clearance, route progress and fuel | Partial | Official profile balances clearance/progress; weights for fuel are not agreed |
| R11 | Account for multi-thruster fuel, no ignition cost | Partial | L1 component-sum proxy in `axis_avoidance.py`; local fuel is mass-based |
| R12 | Initial attitude bias only if justified | Open | Official profile aligns +X to the target; no avoidance bias |
| R13 | PID theory and saturation/overshoot | Reference implementation | `flight/pid.py` with anti-windup and filtered derivative; no final gains |
| R14 | Modular control/filter/tracking/planning | Implemented | Pure modules in `src/wsorb/flight/`; per-person entrypoints in `examples/` |
| R15 | Demonstrate controllers and prediction | Partial | Local teammate and official comparisons published; no live STP demo |
| R16 | Validate the cited ~41% diagonal penalty | Reference explanation | 41.4% is the L1 geometry at 45° for equal resultant acceleration, not measured fuel |
| R17 | Floor-related penalty | Open | No formula or parameters were provided |
| R18 | Compare and combine controller approaches | Partial | Matched local comparison; combination remains a team decision |
| R19 | Fixed-gain alpha-beta / alpha-beta-gamma | Reference implementation | `flight/tracking_filter.py`; not selected for the qualifier |
| R20 | Readable, short qualifier contribution | Open | Ali's official-profile entrypoint is readable; submission owner/entry not selected |
| R21 | Alpha-beta equations and gamma | Reference implementation | Gamma uses the documented `2 * gamma` convention; the transcript was inconsistent |
| R22 | 300 s forecast | Not validated | No forecast-error study; do not claim a horizon |
| R23 | Qualifier deadline and next scrum | Open | Minutes: scrum Tue 3 Nov, qualifier Wed 4 Nov (confirm official date) |
| R24 | Demonstrate progress, exams first | Process | Keep contributions small and explainable; no fabricated readiness |

## Performance audit

The published official comparison records `median_policy_wall_s` of about
8 ms per decision for Ali's policy, with a worst observed decision below 50 ms
against a 0.5 s controller step. The hot path uses NumPy over the 200 rolling
radar rows and a bounded candidate set; it does not call the route planner.

The new reference modules are pure and small:

- `axis_avoidance.py` ranks a caller-sized candidate list in O(N log N).
- `pid.py` is O(1) per update and allocates fixed 3-vectors.
- `tracking_filter.py` is O(1) per target update and allocates no covariance.

No new dependency, thread, subprocess or file I/O was added to the flight path.

## Verification and open decisions

- Local tests cover the new pure modules and the Ali sweep alias.
- The official-profile real-WASM arrival regression remains the authority for
  the local 6/6 claim.
- The STP probe must be run in the licensed runtime before any enabled flight.
- Agree the fuel/clearance/progress weights before wiring `axis_avoidance.py`.
- Agree PID gains and an anti-windup policy before replacing the proven PD path.
- Agree filter gains and a measurement schedule before using gamma in flight.
- Confirm the qualifier submission owner, deadline and entrypoint.
