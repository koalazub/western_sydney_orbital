# Tom baseline vs MPPI: single-pass experimental design

Prepared: 16 November 2026, 19:24:04 AEDT (Australia/Sydney, UTC+11).

Status: design for a new campaign; no flight results are reported here.

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
