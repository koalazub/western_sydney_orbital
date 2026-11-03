# Harry controller

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
