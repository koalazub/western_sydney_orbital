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
named owner of the official-profile contribution in this repository. Ali is
**not** the automatic qualifier submitter. The repository is a collection of
profiles for comparison; choosing and combining the qualifier entry remains an
explicit team decision.

## Requirement matrix

| ID | Minute requirement | Current state | Evidence / next step |
| --- | --- | --- | --- |
| R1 | Consistent reference frames for ship and targets | Implemented locally | `flight/estimation.py`, `prediction.py`, frame-labelled `calibration.py` |
| R2 | Model and propagate process/measurement uncertainty | Reference implementation | `calibration.py` fits bias/scale, residual covariance, process noise and covariance updates; not an STP calibration |
| R3 | Estimate ship and obstacle motion | Implemented locally | `StateEstimator`, `predict_obstacles`, radar propagation |
| R4 | Multi-target lifecycle and association | Reference implementation | `association.py` gated one-to-one association and lifecycle; not wired or optimal multi-hypothesis |
| R5 | Per-target track ageing | Reference implementation | `association.py` per-track `last_seen_s`; caller supplies thresholds |
| R6 | Event-based / neuromorphic sensing | Offline prototype | `event_camera.py` synthetic events and clustering; not renderer/hardware integrated |
| R7 | Camera calibration and astronomical positioning | Partial prototype | `event_camera.py` pinhole projection/calibration; no star catalogue or STP vision |
| R8 | Select estimator, noise, association and update rules | Open decision | Reference modules exist; the team has not selected gains, thresholds or schedules |
| R9 | Prefer single-axis avoidance when safe | Reference implementation | `axis_avoidance.py`, `maneuver_objective.py`; not wired into the proven policy |
| R10 | Balance clearance, route progress and fuel | Reference implementation | `maneuver_objective.py` with caller-supplied weights and Pareto frontier |
| R11 | Account for multi-thruster fuel, no ignition cost | Reference implementation | L1 proxy and `FuelProxy`; explicit exclusion of ignition/allocation |
| R12 | Initial attitude bias only if justified | Reference implementation | `attitude_bias.py` optional shortest-arc planner and bounded angular request |
| R13 | PID theory and saturation/overshoot | Reference implementation | `pid.py` plus `control_validation.py` synthetic metrics; no final gains |
| R14 | Modular control/filter/tracking/planning | Implemented | Pure modules in `src/wsorb/flight/`; per-person entrypoints in `examples/` |
| R15 | Demonstrate controllers and prediction | Partial | Local teammate and official comparisons published; live STP demo remains external |
| R16 | Validate the cited ~41% diagonal penalty | Geometry validated | `maneuver_objective.py` yields `sqrt(2)-1`; not a measured propellant result |
| R17 | Floor-related penalty | Labelled local proxy | `FloorPenaltyProxy` is configurable; the official formula and parameters are still unknown |
| R18 | Compare and combine controller approaches | Profiles ready; decision pending | All profiles are accessible; combination remains the team conversation |
| R19 | Fixed-gain alpha-beta / alpha-beta-gamma | Reference implementation | `tracking_filter.py`; no final gains or qualifier selection |
| R20 | Readable, short qualifier contribution | Source ready; entry not selected | Ali's local entrypoint and STP source port exist; Ali is not auto-submitter |
| R21 | Alpha-beta equations and gamma | Reference implementation | `2 * gamma` convention documented; transcript inconsistency preserved |
| R22 | 300 s forecast | Synthetic validation only | `control_validation.py` measures synthetic error up to 300 s; no real forecast claim |
| R23 | Qualifier deadline and next scrum | Team decision | Minutes: scrum Tue 3 Nov, qualifier Wed 4 Nov (confirm official date) |
| R24 | Demonstrate progress, exams first | Process | Keep contributions small and explainable; no fabricated readiness |

## Completed reference packages

| Package | Public module | Focused tests |
| --- | --- | --- |
| Calibration and covariance | `flight/calibration.py` | 12 |
| Association and track lifecycle | `flight/association.py` | 17 |
| Maneuver objective, floor proxy, 41% geometry | `flight/maneuver_objective.py` | 32 |
| Optional attitude bias | `flight/attitude_bias.py` | 43 |
| Event-camera and pinhole prototype | `flight/event_camera.py` | 22 |
| PID/gamma/300 s synthetic validation | `flight/control_validation.py` | 14 |
| STP official-profile source port | `stp_official.py`, `examples/ali/stp_official_profile.py` | 29 |

These are focused package tests, not the final whole-project result. The
licenced-runtime probe and flight remain external.

## External or team decisions still required

- Run the disabled STP probe and then an enabled validation on the licensed
  Windows runtime. No local test substitutes for that.
- Choose the qualifier entry and submitter. Ali owns the official-profile
  contribution but is not the automatic submitter.
- Agree clearance/progress/fuel weights before wiring the objective.
- Agree PID gains, anti-windup policy, filter gains and measurement schedule.
- Obtain an official floor-penalty formula; the current proxy is local.
- Integrate event-camera output into the renderer or a hardware path before
  claiming event-sensor behaviour.
- Demonstrate the selected controllers live; local comparisons are not an STP
  demo.

## Performance audit

The published official comparison records `median_policy_wall_s` of about
8 ms per decision for Ali's policy, with a worst observed decision below 50 ms
against a 0.5 s controller step. The hot path uses NumPy over the 200 rolling
radar rows and a bounded candidate set; it does not call the route planner.

The new reference modules are pure and small:

- `axis_avoidance.py` ranks a caller-sized candidate list in O(N log N).
- `maneuver_objective.py` and `attitude_bias.py` operate on small bounded sets.
- `pid.py` is O(1) per update and allocates fixed 3-vectors.
- `tracking_filter.py` is O(1) per target update and allocates no covariance.
- `calibration.py` is vectorized over samples and rejects singular systems.
- `association.py` uses O(N·M) vectorized distances plus O(P log P) sorting of
  the gated candidate pairs, with a hard pair cap.
- `event_camera.py` is vectorized over pixels/events with hard event and cluster caps.
- `control_validation.py` is a synthetic deterministic harness; it is not on the
  flight hot path.

No new dependency, thread, subprocess or file I/O was added to the flight path.

## Verification and open decisions

- Focused tests passed for each new package (12 + 17 + 32 + 43 + 22 + 14 + 29).
- The final whole-project verification passed: Rust 533, Python 2,620,
  7 transit-stress skips, 39 subtests and the platform check.
- The official-profile real-WASM arrival regression remains the authority for
  the local 6/6 claim.
- The STP source port has not been executed on the licensed runtime. Run the
  disabled probe first, then the enabled flight, on the teammate's Windows/STP
  environment.
- Ali owns the profile, but is not the automatic qualifier submitter. Select
  the qualifier entry and submitter as a team decision.
- Agree the fuel/clearance/progress weights before wiring `maneuver_objective.py`.
- Agree PID gains and an anti-windup policy before replacing the proven PD path.
- Agree association/filter gains and a measurement schedule before wiring them.
- Obtain the official floor-penalty formula; the proxy is not official.
