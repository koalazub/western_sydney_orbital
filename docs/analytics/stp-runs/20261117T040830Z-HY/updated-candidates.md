# Matched updated-controller additions

Authorized 17 November 2026, following preparation of the PR on analysis/harry.
Both current sources match main at `7d757fe2c988dd4b8816cc5f585bfc197ba4204d`.

| Result ID | Source | Meaning |
| --- | --- | --- |
| ali_mppi_v2 | controllers/ali/controller.py | Updated MPPI; latest source change 30097fbdf1f1; same revision as the recent diagnostic |
| tom_polyfit_updated | controllers/tom/polyfit/Controller.py | Upstream Polyfit; latest source change 8e2dc968d22f, NaN/order-of-operations fix |

The historical `tom_polyfit` and `tom_polyfit_v2` rows remain frozen and are
displayed as `polyfit_bodge` and `polyfit_bodge_v2`. Upstream Polyfit is neither
of these variants. Record exact bundle hashes in prepared controller_versions.json.

`updated-candidates.json` selects only the two new result IDs. Prepare with
`--mode seeded --controllers ali_mppi_v2 tom_polyfit_updated --match-campaign
runs/stp-20261117T040830Z-HY-final-001`. The supported matched-campaign workflow
inherits the original 22 ordered seeds, saved field, mission, scorer, native
runtime conditions, 300 s simulation limit and one repetition. Coverage-first
order rotates the two new candidates across seeds. Original flights are not rerun
or rewritten. Earlier stopped/debug runs are excluded.

First use `updated-candidates-diagnostic.json` in seeded mode with one seed:
two separate 40-second native delivery diagnostics, excluded from scoring.
Require delivery, actuator effects, observed motion and zero owned processes
after cleanup. Freeze controller sources before scored execution; do not tune
during the sweep. The scored budget is 44 flights, up to 3 h 40 min simulation
at 1x plus overhead, with 8 h 48 min aggregate 720-second flight wall caps.

Evidence roots:

- `runs/stp-20261117T040830Z-HY-updated-candidates-diagnostic-001`
- `runs/stp-20261117T040830Z-HY-updated-candidates-001`
- `runs/stp-20261117T040830Z-HY-updated-candidates-combined-001`

Combine original and new scored evidence using the existing `analyze` command
with both campaign roots and the combined output above. `overview.py` selects
that combined report when present, preserving source campaign and frozen IDs.
Compare reliability, zero-damage arrivals, all-attempt damage and timeout progress;
then score, fuel and time on jointly successful seeds. Disclose survivor selection,
missing scores and evidence retries. These are cross-session whole-controller
comparisons; equal seeds do not imply deterministic runtime replay.

Execution status: both source bundles matched the pinned commit exactly;
MPPI bundle `c6964e2e16ecedc97dc3fbfbda4d80039ed529107932ab05686db2eaaee9db38`;
upstream Polyfit bundle `a891f808960882f7070961b7d1495c6bd97cc4322ee87b95f84195ad82406291`.
Both seed-100 diagnostics reached the 40-second limit with verified delivery,
observed motion, zero damage, and zero owned processes after cleanup. Diagnostic
IDs: MPPI `ec0affa8-b5ad-4ca5-98ee-eb097279448a`, Polyfit
`69b1a83f-732a-416e-af1e-a98a42b59545`. Ruff and strict marimo validation passed.
The matched 44-slot scored queue finished on 17 November 2026 at 13:31:12 UTC.

## Completed: replacement of the interrupted Polyfit flight

- [x] After the sweep finished, reran **tom_polyfit_updated, seed 3253,
  repetition 0** as a separate replacement attempt, using the original frozen
  source bundle and the same native mission, radar seed 100 and 300-second limit.
- Interrupted run ID: `bf1b02fa-f693-45d2-97f2-e611b7e8970b` in
  `runs/stp-20261117T040830Z-HY-updated-candidates-001`.
- Its receipt records `interrupted` / `previous_runner_interrupted`.
  `--resume` preserved it and continued with unstarted flights; it did not retry it.
- Preserve the partial folder and receipt. Do not delete logs to make it appear
  unstarted. Confirm old-process cleanup before launching the replacement.
- Record the replacement's ID and evidence location here. During final analysis,
  explicitly link it to this interrupted slot and count that slot once; retain
  interruption incidence and never choose between attempts by score.

Replacement executed on 18 November 2026; see the terminal evidence below.

## Captured outcomes: 18 November 2026

| Controller | Success / scheduled | Simulation limit | Exception | Interrupted |
| --- | --- | --- | --- | --- |
| ali_mppi_v2 | 18/22 (81.8%) | 4 | 0 | 0 |
| tom_polyfit_updated | 8/22 (36.4%) | 1 | 12 | 1 |

These are the pre-replacement outcomes, including the interrupted seed-3253 slot.
The queue is finished; the replacement result below supersedes that slot in the notebook. The combined
analysis appends these two distinct controller versions to the original final-001
comparison without modifying either source campaign. Missing scores remain
missing, and jointly successful score comparisons have survivor selection.

## Replacement result: 18 November 2026

The owner authorized the seed-3253 replacement. The single-slot campaign is
`runs/stp-20261117T040830Z-HY-polyfit-replacement-001`; run ID
`57e32e2a-c1b9-4b01-823a-c0543742ebb0`, repetition 0. Configuration and its
separate one-seed bank are `polyfit-replacement.json` and
`polyfit-replacement-seed-bank.json`. The original seed bank was not changed.
Before launch, no native STP flight processes were present. The frozen controller
bundle hash and complete mission package matched the original campaign exactly.
Radar remains seed 100 and the simulation limit remains 300 seconds.

The replacement ended **exception**, with no official score, at
2026-11-17 21:58:16 UTC (18 November Sydney), after 94.09 seconds wall time.
`Controller.py:325` calls `OnScreenLogMessage(message, Severity.Info)` without
the required category argument, raising `TypeError` during avoidance. No controller
fix or additional retry was made. The receipt verifies all 18 owned processes
exited, with zero remaining.

`overview.py` loads the original five-controller combined capture and overlays
this explicitly linked replacement by original run ID, controller, seed and
repetition. It checks frozen bundle identity, mission/scorer hashes, limits and
cleanup; it counts the slot once regardless of score and shows both attempt IDs
and outcomes in a replacement-history table. The original interrupted receipt
and the pre-replacement combined report remain preserved. This deliberate
replacement is disclosed separately from automatic evidence-failure retries.

Current upstream Polyfit outcomes: **8/22 successes (36.4%), 1 simulation limit,
13 exceptions**. The original interruption remains part of the physical attempt
history. All other controller outcomes are unchanged.

## polyfit_v2_bodge exception-seed follow-up

Owner authorized 18 November 2026: copy updated upstream Polyfit as
`controllers/tom/polyfit_v2_bodge/Controller.py` and comment out the malformed
`Dodge info` alert at the avoidance return. No guidance or avoidance calculations
change. This is distinct from the older `polyfit_bodge_v2` corrected-cone variant.

`polyfit-v2-bodge.json` and `polyfit-v2-bodge-seed-bank.json` freeze the 13 seeds
whose latest upstream attempts ended in exceptions, including replacement seed
3253. Original seeds and attempt IDs are recorded in `exception_cohort`.
The native mission, radar seed 100 and 300-second simulation limit are retained.
Results append under the existing final evidence directory at
`runs/stp-20261117T040830Z-HY-final-001/polyfit_v2_bodge-exceptions-001`.
The notebook adds the new controller as a separate selected cohort after the
queue finishes, preserving all earlier outcomes and controller versions.
This selection cannot establish a full 22-seed success rate or a paired score
improvement over the unscored exception attempts.

Completed on 18 November 2026: **9/13 arrivals, 4/13 simulation-limit stops,
zero controller exceptions**. This is a selected exception-seed result, not a
22-seed success rate. Seed 3253 first lost its scorer connection (`hook_error`,
WinError 10054), then succeeded on its first eligible evidence retry; both
physical attempts remain preserved. Every one of the 14 physical attempts has
verified owned-process cleanup with zero remaining, and no native flight
processes remain. The controller’s executable syntax differs from the original
only by removal of the malformed alert call.

The overview appends these 13 rows under `polyfit_v2_bodge`, retaining the
110 earlier slots and their separate upstream exception outcomes. Its dedicated
cohort table links each new run to the prior exception’s seed and attempt ID.
All summary, score and timeout views include the new rows when the queue is
finished. Missing timeout scores stay missing. Raw evidence, `attempts.csv`,
`summary.json` and `report.md` are in the appended campaign directory above.
