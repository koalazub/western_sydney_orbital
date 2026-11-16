# Harry controller

## Official STP analysis notebook

[`stp_campaign_analysis.py`](stp_campaign_analysis.py) is a Marimo notebook for the
largest native Windows campaign, `runs/stp-full-20261115-001/seeded-radar100`.
It excludes incomplete attempts and other datasets. Completed 600 s timeouts
remain outcomes with missing scores. The default comparison uses first-pass
seeds with completed flights from every controller; repeatability uses complete
two-run pairs only.

From the repository root, using installed Python through UV:

```powershell
uv run --no-project --python 3.12 --with "marimo>=0.25.2" --with "altair>=6.3.0" python -m marimo edit --no-sandbox examples/harry/stp_campaign_analysis.py
```

The notebook covers arrival rates, official score reconciliation and components,
fuel, damage, matched seed comparisons, repeatability, command pacing, native
actuator readbacks, radar age and individual flight histories. The first load
streams the raw event logs; later loads use per-flight caches under the selected
campaign's `harry_analysis/` directory. Missing raw data produces a path prompt;
no simulation is launched. CSV outputs and an analysis summary are saved alongside
the cache, without modifying raw evidence or runner reports.

## Local practice controller

There is no separate STP entrypoint here. Harry's local controller is the shared
basic PD/radar policy in
[`src/wsorb/flight/control.py`](../../src/wsorb/flight/control.py):

- `basic_pd_radar` — position/velocity PD with short-range public-radar
  repulsion, capped at 0.5 m/s²
- `coast` — zero command control trial

The local adapter is
[`src/wsorb/controllers/_simple.py`](../../src/wsorb/controllers/_simple.py).
Both profiles are registered in `src/wsorb/controllers/catalog.json` and can be
selected in **Controller profiles** or run through the cross-team sweep:

```sh
uv run --locked --extra lab python examples/flight_sweep.py --config examples/teammate_comparison.json --output runs/teammate-comparison-new
```

This policy does not model a complete stopping reserve or certify clearance. It
is a practice baseline, not an STP submission. Use only one command writer per
flight.
