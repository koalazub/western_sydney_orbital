# Polyfit cone correction and MPPI comparison

Experiment ID: 20261117T040830Z-HY. Created 17 November 2026, 15:08:30 AEDT
(Australia/Sydney, UTC+11); identifier uses UTC. Owner authorized preparation
and execution. This is a new experiment; earlier sweep results remain unchanged.

## Question and hypothesis

Does correcting the forward cone improve native Polyfit arrival reliability and
reduce damage, with identical radar timing handling in both Polyfit arms?
Hypothesis: valid forward threats reach the tracker and permit useful avoidance.
Ali MPPI is a whole-controller reference, not an isolated algorithm treatment.
No favourable outcome is assumed; increased avoidance may cost time and fuel.

## Arms

| ID | Implementation | Role |
| --- | --- | --- |
| tom_polyfit | Original cone; history timing fix; fallback log disabled | Baseline |
| tom_polyfit_v2 | Corrected cone; same timing and logging fixes | Treatment |
| ali_mppi | Latest checkout, including Ali's own history correction | Performance reference |

Kalman and Bui are excluded. Both Polyfit arms retain 8 m/s cruise, the existing
tracker, candidate velocities, governor and goal planner. Their only code
difference is the cone correction. The README records interventions and Ali's
remaining history fallback difference. Radar and mission origins are assumed
shared as specified by the owner; both Polyfit offsets are explicitly zero.

## Method and budget

Use licensed Windows STP 0.54.2 at 1x with the official saved-field package
0d753a28e1a4265b2839bf8e9fbbc9194efafb87. Mission and scorer hashes are in
campaign.json. No local WASM adapters or modified competition physics are used.
The 22 saved seed-bank scenarios vary debris impulses; radar noise stays at 100.
Run one flight per arm per seed: 66 scored attempts. Coverage-first scheduling
rotates controller order between seed blocks. Equal seeds do not guarantee
identical asynchronous observations; these are not independent debris layouts.

First run a separate 40-second V2 diagnostic, excluded from scored analysis.
Inspect delivery evidence before launching the main campaign. Diagnostics can
end with an official arrival/failure or the diagnostic simulation limit.
Require native commands, actuator readback/effects and observed shuttle motion.
Freeze fresh source snapshots; do not tune or edit them during the sweep.

Main limits: 300 s simulated time, 720 s wall time, 120 s startup,
20 s simulator stall and 45 s controller stall. Sampling period is 0.1 s.
66 flights at the simulation limit equal 5 h 30 min of simulated flight time;
startup, cleanup and runtime pacing add overhead. Wall cap is 13 h 12 min.
These are budget figures, not predicted completion times.

## Outcomes and analysis

Primary: official arrival within budget, accumulated damage and zero-damage
arrivals. Secondary: native official score where available, arrival time, fuel
and relative arrival speed. Report exceptions, stalls, evidence failures and
budget timeouts explicitly. Missing scores remain missing.

### Timeout distance and progress (analysis amendment, 17 November 2026)

For simulation-budget timeouts, report the last observed straight-line distance
between shuttle and target centres (`range_m`, metres), its sample time
(`last_telemetry_sim_s`), and its time difference from the 300 s cutoff. This is
the last available observation, not necessarily a measurement exactly at 300 s.
Keep final fuel, damage and relative speed alongside distance.

Derived fractional progress is `1 - final_range_m / initial_range_m`. Use a
recorded official initial range if available; otherwise use the first valid
telemetry range, explicitly label it as an observed-baseline approximation and
retain that initial sample's time. Do not substitute a nominal initial range.
Progress can be negative when the shuttle ends farther away; do not clip it.
Missing final/initial readings or a nonpositive denominator leave progress
missing. State sample coverage when comparing seeds. Crashes and stalls remain
separate from simulation-budget timeouts, even if partial distance is available.

Remaining distance and progress are descriptive metrics, not official scores.
No derived timeout score is currently computed by the harness; any later score
formula needs its own documented definition and separate label. Native missing
scores stay missing. This amendment changes analysis documentation only and
does not change flight capture or any already frozen controller sources.

Pair V2 minus original Polyfit by seed/repetition. Report counts, distributions,
medians and worst cases. Time/fuel comparisons on jointly successful seeds must
be accompanied by all-attempt outcomes to disclose survivor selection. Ali
comparisons are exploratory whole-policy comparisons. One repetition measures
scenario coverage, not within-seed repeatability or definitive statistical proof.

## Failure and provenance policy

### Evidence retry amendment (17 November 2026)

The owner authorizes up to three additional attempts for a logical run whose
outcome is `evidence_failure`, including an eligible previously failed run on
explicit resume. Same seed, controller snapshot, mission, repetition and limits;
four total physical attempts maximum. Require verified cleanup and no native
score/scoring inputs before each retry; respect Stop. No retries for other
outcomes. Exhaustion or an ineligible infrastructure failure halts the queue.
Separate sibling folders preserve every physical attempt and its original raw
measurements. Analysis counts the latest attempt once per scheduled run and
discloses prior outcomes/retry count, never selecting the best score. Report
retry incidence alongside mission success and scores. This explicitly
supersedes the no-retry rule below for eligible evidence failures only.

Continue after controller exceptions only with verified owned-process cleanup.
Simulator, startup, sensor/evidence and cleanup failures halt the queue. Never
replace started attempts or silently retry failures. Any repeat is a separately
identified campaign. No prior failed source version is pooled into this one.
Prepared manifests freeze source hashes, machine/runtime dependencies, scenario,
seeds, limits and controller environment; exact bundle hashes are authoritative
for local edits, not an invented committed version.

## Locations

- Main evidence: runs/stp-20261117T040830Z-HY-final-001 (amended mission)
- Preserved original preparation: runs/stp-20261117T040830Z-HY-seeded-001
- Diagnostic: runs/stp-20261117T040830Z-HY-pilot-001
- Queue status: runs/stp-20261117T040830Z-HY-final-001/queue_status.json
- Current configuration: final-submission.json and final-seed-bank.json
- Historical configurations: campaign.json and pilot.json beside this document

Execution status and limitations will be recorded in README.md. Prepared sources
and raw measurements remain in runs/; this directory retains design and notes.

## Mission amendment: exact final-submission configuration

Before starting the main sweep, prepare a separate batch with
`final-submission.json`, selecting `Sims/FINAL_SUBMISSION.simconf` from the same
pinned official revision. Its SHA-256 is
`04abcfb54321b22b68322ecf98d785cc548a7bd7b7ba447aac511d9a4d38e06e`.
Official main was checked on 17 November 2026 and still resolves to
`0d753a28e1a4265b2839bf8e9fbbc9194efafb87`.

The full parsed-JSON comparison found exactly these changes:

| Entity | Property | Competition | Final submission |
| --- | --- | --- | --- |
| Pilot (entity 456) | RotateToEntity | Phobos | SpaceBuggy |
| OrbitPawn (entity 457) | RotateToEntity | SpaceBuggy | Phobos |

There is no debris-field swap. All other config values, including saved debris,
actuators, sensor settings, initial state and scoring inputs, are identical.
These are pawn/view orientation changes; exact runtime equivalence is not
claimed from a static comparison alone.

Source: [official Sims directory at the checked revision](https://github.com/SimDynamX/STU4-Sim/tree/0d753a28e1a4265b2839bf8e9fbbc9194efafb87/Sims).
Both local files' Git blob hashes match the official directory listing.
The final-submission seed-bank snapshot keeps the same 22 values and ordering,
with a distinct bank ID and mission hash because the harness binds banks to an
exact mission. It does not modify the shared competition seed bank.

Main final-submission evidence will be stored at
`runs/stp-20261117T040830Z-HY-final-001`. Preserve `seeded-001` and the completed
competition-config diagnostic as historical preparation/compatibility evidence.
No launches are part of this amendment. Use the final batch for subsequent
execution and do not pool it with competition-config flights as one version.

## Controller naming and MPPI-v2 follow-up (17 November 2026)

The owner's modified controllers are now `polyfit_bodge` and
`polyfit_bodge_v2`, at the matching directories under `controllers/tom/`.
Original final-001 frozen IDs remain `tom_polyfit` and `tom_polyfit_v2`;
these historical names in the earlier entries above refer to the same preserved
bundles. The notebook maps those IDs to the new display names without changing
raw evidence. Configuration copies here now use the renamed live source paths;
the authoritative configurations actually executed remain in each run's snapshot.

The separately authorized `ali_mppi_v2` comparison, native diagnostic evidence,
source commit, design and execution status are in [ali-mppi-v2.md](ali-mppi-v2.md).
This supplements the earlier frozen three-arm experiment.
