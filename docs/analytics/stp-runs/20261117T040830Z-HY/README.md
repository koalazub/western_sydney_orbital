# Polyfit radar timing changes for the next sweep

Recorded 2026-11-17 UTC. See [experimental-design.md](experimental-design.md)
for the approved comparison and [campaign.json](campaign.json) for its settings.
Fresh snapshots have been prepared under
`runs/stp-20261117T040830Z-HY-seeded-001` (66 attempts) and
`runs/stp-20261117T040830Z-HY-pilot-001` (one separate V2 diagnostic).
The diagnostic delivery gate passed in `pilot-003`, launched by the owner from
normal PowerShell. The current execution target is now the separately prepared
`runs/stp-20261117T040830Z-HY-final-001`, selecting the exact official
`FINAL_SUBMISSION.simconf`. The earlier `seeded-001` snapshot remains preserved.

## Final-submission config and timeout reporting amendment

The experimental design now explicitly requires last-observed timeout distance,
sample time relative to cutoff, and derived fractional progress with a declared
initial-range source. These are descriptive metrics, not fabricated official
scores; the harness already captures raw distance and timestamps. Derived
progress is specified for later analysis, not newly added to harness output.

Current official main is the same pinned `0d753a28...` revision. A full parsed
comparison found only Pilot and OrbitPawn `RotateToEntity` references swapped;
the debris field and all other configuration values are identical. Local Git
blob hashes match the official listing. See the design amendment for exact
values, source links and final mission SHA-256.

The new final batch has 66 attempts with identical controller bundle hashes,
22 seed values/order and limits to `seeded-001`. A separate seed-bank snapshot
records the final mission hash without altering the shared bank. Both batches
remain unstarted at this amendment. No simulator code, physics, controllers,
frozen earlier sources or existing results were modified.

## Diagnostic passed: pilot-003

Attempt `59b50066-50c7-4eec-9917-1da3e27fb09c` completed the separate 40-second
diagnostic with outcome `simulation_limit` (expected budget stop, not arrival).
There were 1762 command writes, 280 native actuator samples, 280 matched nonzero
readbacks and 280 nonzero impulse samples. Delivery was verified and motion was
observed: 63.07 m displacement and 3.84 m/s velocity change across sampled
actuator states. Motion includes gravity/contact effects; this is not exact
thrust attribution. All 18 owned processes exited during cleanup. Controller
bundle hashes match the prepared main campaign. No official score was received;
this diagnostic remains excluded from the scored sweep.

Launch from the same normal PowerShell terminal, at repository root:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/stp-20261117T040830Z-HY-final-001 --continue-on-controller-error --status-file runs/stp-20261117T040830Z-HY-final-001/queue_status.json
```

The agent's restricted execution environment failed to reach simulator startup;
normal PowerShell succeeded. Do not repeat login or alter credentials to address
the restricted-environment failure.

## Execution stopped: launcher authentication

The owner reported failed OAuth during the diagnostic startup. A clean stop was
requested through the runner. Diagnostic attempt
`585b31e1-2bc1-4c12-a628-369f987cf04a` is retained as aborted, with no official
score; it does not establish controller delivery or flight performance. The
66-attempt main campaign remains prepared and unstarted. No automatic retries,
credential changes or authentication workarounds were attempted. After normal
STP sign-in is restored, use a separately identified diagnostic rather than
replacing this aborted attempt.

## Source changes

### Harness evidence retries

Owner-authorized amendment: three additional attempts maximum after eligible
`evidence_failure` with successful cleanup and no recorded native score. Each
retry retains the same logical run/seed/controller but writes a separate
`<UUID>-retry-N` folder. Original evidence and frozen campaign snapshots stay
unchanged. Resume can recover the failed Ali seed-100 slot; status and analysis
retain retry provenance and count the latest result once. Retry policy is in the
design amendment and harness README. No simulator restart was launched while
implementing this change; resume from normal PowerShell when ready.
Validation: 14 focused retry, resume, queue, status and analysis tests passed
under native Python 3.12; Ruff formatting and lint passed. A read-only inspection
confirmed Ali seed 100 remains eligible for its first retry. No simulator flight
was launched as part of these checks. The separately aborted V2 seed-1729 slot
is not an evidence failure and is deliberately not retried by this policy.

Both `controllers/tom/polyfit_bodge/Controller.py` and
`controllers/tom/polyfit_bodge_v2/Controller.py` now keep recent own-position history,
interpolate the shuttle position at each radar timestamp, and rebase radar
offsets before cone filtering and track updates. Each row retains its own epoch;
rows are processed oldest first. Repeated rows are not assimilated again.
Invalid rows, timestamp-zero bootstrap rows, future or over-5-second-old rows,
and measurements outside recorded history are skipped. History is bounded to
10 seconds and at most 4096 samples. Missing usable observations do not prove
clear space; the existing avoidance policy has not been redesigned.

This prevents current shuttle motion from being incorrectly added to historical
rock positions. Both arms receive identical handling so V2's corrected cone is
still the intended difference. The original arm deliberately retains its cone
bug. Both also retain the earlier disabling of the malformed fallback SDK log.
Guidance, cruise, governor, avoidance candidates and simulator radar are unchanged.
The excluded Kalman implementation and its camera behaviour were not modified.

## Ali review and remaining clock limitation

Ali's current source adds `record_shuttle_state`, `shuttle_position_at` and
`reconstruct_measurements` before track estimation. Ali was inspected but not
modified. His correction still compares radar timestamps directly with mission
elapsed time and falls back to current position when history is unavailable.
With the owner-specified shared-origin assumption, his history interpolation
addresses the reconstruction error where history is available. His fallback
behaviour remains a relevant comparison difference.

Tom's `WSORB_RADAR_ORIGIN_OFFSET_S` explicitly maps radar time to mission time:
mission_measurement_s = radar_timestamp_s + origin_offset_s. The owner specifies
a shared origin for this run; use zero. This is an explicit assumption, not a
measured offset. The pinned official radar source
`0d753a28e1a4265b2839bf8e9fbbc9194efafb87` sets its origin at radar startup,
not from mission StartDateTAI. No origin is guessed from packet arrival and no
harness-only clock is injected into competition policy. Clock calibration is not
a prerequisite for this run under the shared-origin assumption.

## Experimental provenance

Prepare fresh frozen sources before execution; leave previous schedules,
snapshots and measurements unchanged. Do not pool old and new source versions
under a single controller label. Record configured clock offsets and bundle
hashes in the new campaign. The shared historical campaign configuration is
unchanged; this experiment uses its own configuration beside this README.

Prepared bundle SHA-256 prefixes: original Polyfit `78cc599f3d1d`,
Polyfit V2 `4a9876034c51`, Ali MPPI `a192fc3dac23`. Full hashes are in each
prepared campaign's controller_versions.json. Ali matches checkout commit
`5b0e36049074a77f4f8571012a4478b0efd0c9cf`; the Polyfit bundles include local
edits and are correctly labelled as modified rather than assigned a false commit.

Verification uses SDK-free numeric tests of history interpolation, changing
shuttle speed, mixed timestamps, duplicates, invalid ages, explicit offsets,
reset handling and parity between the two timing implementations. All three
numeric regression tests passed under native STP Python 3.12. Ruff formatting
and lint passed for the changed Python files. A source comparison confirmed
the two controller files differ only in the cone-filter calculation. Native flight
and local WASM validation have not been performed for this edit.

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
