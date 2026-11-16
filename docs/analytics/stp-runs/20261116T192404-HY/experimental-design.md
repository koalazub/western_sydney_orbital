# Current-main Tom entrypoints vs Ali MPPI: single-pass experimental design

Prepared: 16 November 2026, 19:24:04 AEDT (Australia/Sydney, UTC+11).

Status: the same experiment is being restarted after an interrupted execution;
final results have not been reviewed. The current execution plan below supersedes
the initial two-controller and proposed four-version plans retained as history.

## Current execution plan: restart-001

Experiment ID: `20261116T192404-HY`. Execution batch: `restart-001`.
Use unchanged controller files from current main commit
`570b175cac663e4e0cf59aa4d5d5c5ef31021d24`, merged into `analysis/harry`.
The restart is **22 seeds × three controllers × one pass = 66 flights**, with
the existing 300-second observed simulated-time limit, 1× playback and watchdogs.
Bui is deferred. No local controller fixes, tuning or restoration of old code
are part of this execution.

| ID | Author and implementation label | Entrypoint |
| --- | --- | --- |
| `tom` (reference) | Tom Kirkpatrick — Polyfit (legacy entrypoint in kalman folder) | `controllers/tom/kalman/Controller.py` |
| `tom_polyfit` | Tom Kirkpatrick — Polyfit (current entrypoint) | `controllers/tom/polyfit/Controller.py` |
| `ali_mppi` | Ali El Ali — MPPI | `controllers/ali/controller.py` |

Both current-main Tom entrypoints contain a position-history velocity estimate
using `np.polyfit(..., 1)`; neither imports or instantiates the retained
`DebrisTracker` Kalman implementation. Therefore the `kalman` directory name does
not determine the analysis method label. The actual Kalman controller from the
earlier batch remains recorded there; it is not silently substituted into this
current-main restart. The latest main also resolves the missing unused-helper
import in the newer Polyfit entrypoint, so no local import edit is needed.

These are whole-controller comparisons, not an isolated estimator experiment.
The incoming MPPI version differs from the earlier batch. Labels record author,
implementation, explicit source commit and complete frozen-folder fingerprint.
Supporting modules and README files are included in the fingerprint. Preparation
verifies committed source content while preserving checkout/merge revision
separately from the pinned source origin.

The interrupted execution `runs/tom-mppi-300s-single-pass-002` stays saved with
its original labels and outcomes. The restart uses
`runs/stp-20261116T192404-HY-restart-001`, created by `prepare`. Configuration
records the same `experiment_id`, new `execution_batch` and `supersedes_execution`
pointing to that interrupted batch. The restart is the planned primary dataset
for final analysis. Earlier flights remain visible as exploratory/operational
history; do not pool duplicate seed/controller attempts into restart statistics
or substitute an earlier success for a restarted failure.

```powershell
$stpPython = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Versions\v0.54.2\py\Scripts\python.exe"
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py prepare --config experiments/stp_benchmark/campaign.json --mode seeded --seed-count 22 --output runs/stp-20261116T192404-HY-restart-001
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/stp-20261116T192404-HY-restart-001 --continue-on-controller-error
```

At 1×, 66 full-budget flights represent about **5 hours 30 minutes** of flight time
before startup/cleanup overhead; this is not a promised wall-time duration. Flights
run sequentially. Started attempts are retained on errors; resume only unstarted
IDs. A restart does not justify suppressing timeout, crash or evidence-failure counts.

## Earlier design and subsequent amendments

## Question

Compare Tom's current baseline and Ali's MPPI controller on the same 22 debris
perturbation seeds, with a 300-second simulated-time budget per flight. Assess
arrival reliability first, then native score, remaining distance, fuel and damage.

The owner selected one pass because within-seed variance is considered nominal
for this comparison. This is a design assumption, not a variance estimate from
this campaign. No repeatability claim or analysis is planned.

## Controllers and matched conditions

| Setting | Design |
| --- | --- |
| Reference | `tom`: `controllers/tom/Controller.py` |
| Candidate | `ali_mppi`: `controllers/ali/controller.py` |
| Runtime | Native Windows SpaceCRAFT + Compute_Server, normal scheduling, 1× playback |
| Mission | Official STU4 `Sims/COMPETITION_SIM.simconf` |
| Package revision | `0d753a28e1a4265b2839bf8e9fbbc9194efafb87` |
| Population | Saved debris field; seed changes initial debris impulses |
| Radar | Original radar model, fixed seed 100 |
| Schedule | 22 seeds × 2 controllers × 1 flight = **44 flights** |
| Flight budget | **300 simulated seconds**, or earlier native arrival/damage failure |
| Ordering | `coverage_first`; controller order rotates between seed blocks |
| Sources | Frozen during preparation; no tuning during the campaign |

Configuration: [campaign.json](../../../../experiments/stp_benchmark/campaign.json).
Seed bank: [seed_bank.json](../../../../experiments/stp_benchmark/seed_bank.json),
ID `stu4-phobos-2026-11-11-impulses-radar100-v1`.

The ordered seed set is:

```text
100, 1729, 3253, 4999, 7919, 104729, 130363, 155921, 196613, 262147,
327673, 393241, 458879, 524309, 589859, 655373, 720899, 786433,
851971, 917519, 983063, 8193
```

These are 22 matched perturbation scenarios, not 22 independently generated
layouts. Equal seeds do not guarantee identical asynchronous execution.

## Stopping rules and timeout classification

| Outcome | Interpretation and reporting |
| --- | --- |
| `success` | Native arrival; retain original scorer inputs and score |
| `failure` | Native damage failure; retain original scorer inputs and score |
| `simulation_limit` | No observed native terminal receipt before the runner reached the simulated-time limit; retain as a budget timeout with no invented score |
| `wall_timeout` | 720-second wall watchdog; incomplete observation, not a completed 300-second flight |
| `startup_timeout` | Controller/scorer startup exceeded 120 wall seconds |
| `simulator_stall` | No scorer-time progress for 20 wall seconds after connection |
| `controller_stall` | No controller command progress for 45 wall seconds after connection |
| Exceptions, evidence failure, crash, interruption or owner stop | Preserve the reason and partial evidence; report separately from budget timeouts |

The runner continues after simulation-limit and wall-timeout outcomes when cleanup
succeeds. With `--continue-on-controller-error`, controller exceptions also remain
recorded and the next scheduled flight starts after verified cleanup. Startup,
stall, simulator, evidence and cleanup failures halt the campaign for investigation.
Started attempts are never silently retried or replaced. Resume only unstarted IDs;
any deliberate rerun belongs to a separate campaign.

### Boundary limitation

The intended hard budget is 300 simulated seconds. The current implementation is
an observed threshold: it stops when received controller telemetry or scorer
progress reports a time at or above 300. The clocks have different origins, and
receipt delivery, sampling and process cleanup can add overshoot. A native receipt
is not independently checked against a common 300-second clock before acceptance.

Consequently, this campaign must not be described as an exact 300.000-second
simulator stop. Inspect boundary events using mission-start timestamps and flag
late or ambiguous receipts separately; do not silently count them as confirmed
within-budget arrivals. Missing timing evidence remains unresolved. Exact boundary
enforcement requires a separate implementation change.

## Analysis

### Arrival and coverage

Report counts for every scheduled outcome, attempted flights, observed arrivals,
budget timeouts and infrastructure/incomplete outcomes for each controller.
Show arrivals out of 22 scheduled flights as campaign coverage; incomplete flights
have unknown physical outcomes and must not be presented as confirmed failures.
For a completed physical-outcome cohort, show its denominator and exclusions.

Compare outcomes within each matched seed: both arrived, Tom only, MPPI only,
neither arrived, or incomplete/ambiguous. Show boundary flags alongside these
categories. Do not let unscored timeouts disappear from the controller comparison.

### Remaining distance for timeouts

For each budget timeout, use the last recorded straight-line shuttle-to-target
distance:

```text
remaining_distance_m = norm(target_position_phobos_m - position_phobos_m)
```

This is already recorded as telemetry `range_m` and carried into `attempts.csv`
when native scoring inputs are unavailable. Label it **last observed target
distance**, with the actual sample's `sim_s`. Do not label it an exact reading at
300 seconds or extend the trajectory to fill a missing interval.

Show per-seed distances and their median, spread and available-reading count for
budget timeouts. Include final relative speed, fuel and damage as context. Smaller
distance indicates greater proximity, not arrival or a safe rendezvous. Keep
wall-timeout and other early-stop distances separate because their horizons differ.

Where both controllers time out on the same seed, report
`MPPI range_m - Tom range_m`; a negative value favours MPPI on proximity. Report
the number of matched timeout seeds. Timeout-only groups may contain different
seeds, so their aggregate medians alone do not establish superiority.

### Native scores and other measurements

Keep missing scores null; never replace a timeout with zero or fabricate an
official failure score. Show score coverage beside every score summary. Native
score distributions can include scored damage failures. Arrival-time summaries
use arrivals only and are conditional on success.

Use `matched.csv` for MPPI-minus-Tom native score differences where both flights
have a score. With one configured repetition, `scenario_comparisons.csv` contains
that same difference directly; an unscored controller leaves the scenario
comparison incomplete. Report matched-score coverage out of 22 seeds.

Fuel and damage at a timeout are partial-flight observations. Keep their outcome
and time horizon visible rather than treating low consumption on an early stop
as improved efficiency. Damage increases are observed intervals, not identified
collisions.

The runner already exports outcomes, range readings, scores and matched score
differences. The outcome-pair table, timeout-only distance summaries, sample-time
annotations and boundary review described above require analysis of the saved
receipts/events; they are not all automatic outputs. The historical analysis
notebook contains 600-second labels and must not be reused unchanged for this design.

## Execution and retained evidence

From the repository root in native Windows PowerShell, prepare a new directory
and explicitly start it:

```powershell
$stpPython = "$env:USERPROFILE\AppData\Local\SimDynamX\STPro\Versions\v0.54.2\py\Scripts\python.exe"

uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py prepare --config experiments/stp_benchmark/campaign.json --mode seeded --seed-count 22 --output runs/tom-mppi-300s-single-pass-001

uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py run runs/tom-mppi-300s-single-pass-001 --continue-on-controller-error
```

Preparation freezes sources, mission, seed selection, limits and schedule. A new
preparation uses `repetitions: 1`; old prepared campaigns retain their saved schedule.
Keep the Windows session unlocked for native rendering. At 1×, 44 full-budget
flights represent about 3 hours 40 minutes of simulated flight time, before
startup/cleanup overhead; this is not a wall-time guarantee.

Analysis is written automatically after running. To regenerate it without flights:

```powershell
uv run --no-project --python $stpPython python experiments/stp_benchmark/benchmark.py analyze runs/tom-mppi-300s-single-pass-001
```

Retain `campaign.json`, provenance and frozen sources, `seed_selection.json`,
per-flight manifests, `events.jsonl`, native logs and `receipt.json`. Generated
outputs include `attempts.csv`, `summary.json`, `matched.csv`,
`scenario_comparisons.csv`, `failure_timelines.json` and `report.md`.

The result is a comparison of these frozen controller versions on this pinned
native mission and seed set. Do not pool earlier 600-second campaigns, pilots,
WASM practice flights or historical meeting scores into its outcome rates.

## Follow-up: four controller versions across two campaigns

Plan updated: 16 November 2026, 21:18 AEDT. The next campaign is intended to
evaluate Tom's updated polyfit controller and Bui's controller, subject to those
versions being ready. The current campaign remains evidence for Tom's Kalman
version and the frozen MPPI version. It is not evidence for the incoming Tom code.

| Analysis label | Source campaign | Version evidence | State |
| --- | --- | --- | --- |
| Tom Kirkpatrick — Kalman — commit 0cd42198f94b — source ab2d62f26073 | `runs/tom-mppi-300s-single-pass-002` | All frozen controller files match the preparation commit exactly; tracker confirmed to use Kalman | Running campaign at plan update |
| Ali El Ali — MPPI — commit 0cd42198f94b — source d1a6dd9a0b69 | `runs/tom-mppi-300s-single-pass-002` | All frozen controller files match the preparation commit exactly | Running campaign at plan update |
| Tom — Polyfit — pending hash | New campaign, not yet prepared | Confirm polyfit implementation, then freeze intended incoming version and all supporting files | Pending code/version confirmation |
| Bui — pending hash | New campaign, not yet prepared | Confirm native entrypoint and supporting modules run, then freeze intended version | Pending readiness |

The two recorded entrypoint SHA-256 values are:

```text
Tom Kalman: 2852e380cbf1acf8b6c191b23e57d05e77b87749626f73d2a0d38d170b87ea1b
Ali MPPI:   5700423526bcc33880e74300da57980c604bbff8d66ffa00d866e835a4a56154
```

These identify entrypoint files only. Supporting modules are part of the version:
retain the complete `snapshot_hashes` map from each campaign's `provenance.json`.
In particular, the frozen Tom `debris_tracker.py` SHA-256 is
`747d0b3626fa7a43c7ab27c463b64a97f6c15bef1f6181a0bbee9d3701a2e97f`.
A helper-only change is a new controller version even if the entrypoint hash is
unchanged. Do not identify a controller version solely by its author's name,
filename, branch or latest checkout.

The two existing versions have now been verified against source commit
`0cd42198f94b73ebfe9ab6e840745f02b8266494`. This is the commit from which the
preparation snapshot was taken, not a claim that every controller was originally
authored in that commit. The labels distinguish commit IDs from SHA-256 content
hashes. Both controllers can legitimately share one source commit.

The complete frozen-folder fingerprints are:

```text
Tom Kalman: ab2d62f260733dd69987ae52e47287cbb57c31148c139183547d6f0a7545b40d
Ali MPPI:   d1a6dd9a0b6967af88afdf133327d754311ebfb39ba9831fdcc9d6fb3b733b91
```

Supplemental labels are saved in the campaign's `dataset_labels.json`, bound to
these fingerprints. `controller_versions.json` records full author/method,
source and checkout commits, verification status and fingerprint. Campaign
configuration, frozen sources and raw receipts are unchanged. Re-analysis adds
the labels to CSVs and reports. Future preparations declare `author` and `method`
in each controller entry and verify the source commit from their own frozen files.
If verification fails or history is unavailable, label the commit unverified and
retain the exact content fingerprint rather than assuming the current HEAD matches.

### Conditions for combining results

Combine for comparison as four separate controller-version rows; do not merge
Tom Kalman and Tom polyfit into one Tom success rate or score distribution.
Keep campaign path, run ID, source hashes, seed and outcome attached to every row.
The new configuration should use distinct controller IDs such as `tom_polyfit`
and `bui`, with Tom polyfit as its reference. The display labels must also include
the frozen version identifier. Do not modify the running campaign's configuration,
snapshot, controller IDs or receipts to add display labels.

Before treating flights as matched, check the pinned package, mission/scorer and
binary/dependency hashes, saved field, seed bank selection and scope, radar seed,
1× playback, scheduling, instrumentation/sample period, stopping rules and all
limits. Use the same 22 seeds, one pass and 300-second budget in the next campaign.
Keep the same machine and runtime conditions as far as practicable. If conditions
differ materially, present separate cohorts rather than a pooled ranking.

The existing `analyze` command reads one campaign at a time. Its `matched.csv`
and `scenario_comparisons.csv` compare controllers within that campaign only;
concatenating those files does not produce every cross-campaign comparison.
A combined analysis must read original campaign identities, receipts, provenance
and events, and join controller-version outcomes by the actual seed, not by row
position or block number. Do not mix in earlier campaigns or use an incoming
checkout to relabel frozen sources.

Show all four versions on all 22 seeds with missing/incomplete outcomes visible.
For each pairwise result, show the eligible matched-seed count. Score differences
require two native scores; timeout-distance differences require two budget
timeouts with available readings and comparable observation horizons. The failed
MPPI startup on seed 327673 remains an infrastructure outcome and cannot provide
a matched physical or score result. No failed attempt is silently substituted.

Existing MPPI and Tom Kalman measurements can be reused if the conditions match;
neither must be rerun just to add Tom polyfit and Bui. However, controller version
and campaign time are then partly confounded: this is a matched-scenario comparison
across sessions, not four controllers interleaved in one session. State that limit.

### Handover at 11 pm

Treat 11 pm AEDT on 16 November as a proposed code-readiness handover, not a
completion deadline or an automatic launch. Verify and freeze the intended Tom
polyfit and Bui versions after the incoming changes arrive. Prepare a new campaign
with a new output directory, then start it after the current queue releases its
flight lock. Do not prepare before the intended versions are confirmed.

At the same 22-seed single-pass design, Tom polyfit plus Bui is another 44 flights.
Full-budget flight time alone is about 3 hours 40 minutes at 1×, plus startup and
cleanup. If Bui is not ready, defer its campaign or run it later with the same
conditions and its own version label; do not substitute an older version under
the proposed label.
