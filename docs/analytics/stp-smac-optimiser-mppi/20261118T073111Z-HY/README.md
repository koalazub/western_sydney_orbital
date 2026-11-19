# SMAC3 MPPI results — 20261118T073111Z-HY

The date/owner directory and `overview.py` filename follow the existing
`docs/analytics/stp-runs/20261117T040830Z-HY/` convention.

The completed 88-flight experiment improved training results but failed the six-seed
held-out arrival/damage gate: finalist 4/6 arrivals, own baseline 6/6. Keep the
untuned patched baseline as the supported result of this viability experiment.

Run from the project root:

```powershell
uv run --locked --extra lab marimo edit docs/analytics/stp-smac-optimiser-mppi/20261118T073111Z-HY/overview.py
```

The notebook loads the portable `results.json` snapshot and launches no simulations.
It includes training loss and running-minimum curves, a cap-penalty sensitivity
control, separate confirmation/test summaries, full-cohort broad-sweep rankings,
interactive six-seed paired comparisons, raw final-test metrics and provenance.
The recent MPPI v2 is the initial historical reference; the own untuned baseline
and the other broad-sweep arms are selectable. It does not select the
best historical controller by inspecting the six test results.

To regenerate the snapshot when the original ignored `runs/` evidence is present:

```powershell
uv run --locked python docs/analytics/stp-smac-optimiser-mppi/20261118T073111Z-HY/capture.py
```

`capture.py` reads the completed optimiser summaries, verifies each native receipt
and manifest, checks runtime/seed compatibility and records evidence SHA-256 hashes.
It reads no full telemetry streams and changes no campaign results. Cap damage,
fuel and distance come from the already recorded last-telemetry summaries; cap
native scores remain null. Only `results.json` is regenerated.

Sources: `runs/20261118T073111Z-phobos-harry-mppi-viability`, and the original and
updated broad-sweep campaigns `stp-20261117T040830Z-HY-final-001` and
`stp-20261117T040830Z-HY-updated-candidates-001`. The four scheduled 22-seed broad
arms are retained separately; the updated Polyfit exception cohort and targeted
follow-up are excluded from that ranking. Cone/timing-corrected Polyfit's one
owner-interrupted flight is shown as incomplete, leaving that arm unranked.
Historical runs are additional evidence,
not reused baseline flights or additional held-out optimisation seeds.
