# Updated MPPI matched comparison

Owner authorized the merge, source renames, native diagnostic and 22-seed
controller-only sweep on 17 November 2026. Main is pinned to
`7d757fe2c988dd4b8816cc5f585bfc197ba4204d`; latest controller-changing commit
is `30097fbdf1f14723b6bc5bb5aef61c38b3910ee5`.

The live updated source is `controllers/ali/controller.py`, registered in this
experiment as `ali_mppi_v2`. Frozen `ali_mppi` remains the old version with
bundle SHA-256 `a192fc3dac235681c689852686f24ecd0e1cafe622ca0895358324be034ef311`.
Preparation records the new exact bundle hash and commit match.

Changes: attitude follows body +Z rather than the +X nose; thrust requires
alignment within 0.15 rad except emergency/collision escape; a 25 m radial
launch-clearance phase requests 1.5 m/s; radar epochs are shifted using a
running-minimum arrival-based clock estimate; emergency avoidance combines
tracks and fast/close raw rows, includes ship radius, and evaluates feasible
acceleration paths. MPPI objective, fuel model and 8 m/s cruise are unchanged.
The clock estimate includes transport delay. Lower fuel use is a hypothesis,
not an established result; the NoMains mission does not establish a directional
main-engine efficiency benefit.

Harry's timing-corrected sources now live in `controllers/tom/polyfit_bodge`
and `controllers/tom/polyfit_bodge_v2`. Historical frozen IDs `tom_polyfit` and
`tom_polyfit_v2` map to those display names in `overview.py`; raw evidence,
prepared manifests and original source hashes are preserved. Main's original
Polyfit source stays at its upstream path.

`ali-mppi-v2-diagnostic.json` uses the final-submission mission with a separate
40-second diagnostic. Require native command delivery, actuator readback/effect,
motion and successful owned-process cleanup before the scored sweep.
`ali-mppi-v2.json` is prepared with `--controllers ali_mppi_v2 --match-campaign`
against `runs/stp-20261117T040830Z-HY-final-001`, retaining all 22 ordered seeds,
one repetition, native runtime/scorer/mission and 300 s simulation limits.
The diagnostic is excluded from analysis. No tuning during the sweep.

Planned evidence roots:

- Diagnostic: `runs/stp-20261117T040830Z-HY-ali-mppi-v2-diagnostic-001`
- Scored: `runs/stp-20261117T040830Z-HY-ali-mppi-v2-001`
- Combined: `runs/stp-20261117T040830Z-HY-mppi-v2-combined-001`

Primary comparison: old versus new MPPI arrivals, zero-damage arrivals, damage
and timeout progress on all scheduled seeds. Secondary: fuel, time and official
score on jointly successful seeds, with coverage and survivor selection explicit.
Missing scores remain missing. Existing Polyfit results are retained as references.
This is a whole-policy comparison across sessions, not an isolated fuel treatment.
Maximum flight budget: 110 minutes simulated at 1x, plus overhead; aggregate
720-second wall caps total 4 h 24 min. Evidence-only retries retain prior attempts.

Diagnostic passed: `c4f19fa6-f1b5-440b-afa4-d6da760d778f` reached the
40-second simulation limit, with no official score, zero damage, 559 command
writes, 274 actuator samples and 139 matched nonzero readbacks. Delivery and
motion were verified; 18 owned processes before cleanup, zero after.
New bundle SHA-256: `c6964e2e16ecedc97dc3fbfbda4d80039ed529107932ab05686db2eaaee9db38`,
exact source match to the pinned main commit. The 22-flight matched campaign was prepared successfully, then stopped by the owner during seed 100. No scored comparison was completed.

Focused numeric/controller checks passed (35 tests), as did strict marimo
validation. No full release verification or local WASM flight is claimed.
File conflict resolution was authorized directly; version-control merge state
has not been finalized, per the owner's instruction to leave version control alone.

## Cancelled and cleaned up

Owner stopped the sweep after observing unsatisfactory behaviour. The runner
confirmed owned-process cleanup (18 before, zero after). No further seeds ran.
The initial seed-100 attempt ended in a radar evidence connection reset; its
first retry was aborted by owner request. Last telemetry: 191.113707 s,
1130.965256 m remaining, 3.551472 m/s relative speed, 105.285065 kg fuel,
28072.060746 J damage. No official score. Effective spawner/debris impulse
seed 100; radar seed 100; repetition zero, native STP 0.54.2 at 1x.

At the owner's request, generated diagnostic and sweep evidence directories
were deleted after cleanup. The original final-001 evidence remains unchanged.
The retained config copies and notebook support describe the attempted follow-up;
no completed updated-controller comparison or retained raw follow-up data is claimed.
