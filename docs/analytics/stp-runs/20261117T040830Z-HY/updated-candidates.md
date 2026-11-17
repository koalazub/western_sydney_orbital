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
The matched 44-flight scored batch is running; no completed sweep is claimed.
