# Ali controller

[`controller.py`](controller.py) is the current single-file STP submission. The
marked core from `# BEGIN ALI MPPI CORE` to the end of the file is the only copy;
the local adapter and numeric tests load the same region.

The runtime profile is `ali_mppi`. The policy is settings-driven: cruise
reference, terminal coast, emergency guard, MPPI plan steering, main-engine gate,
risk/switch weights and attitude brake scale. `wsorb fuzz` reports the score
distribution; `wsorb tune` searches the settings with a worst-seed objective and
held-out validation.

```sh
uv run --locked wsorb fuzz --seeds 100-107 --workers 4
uv run --locked wsorb tune --grid '{"cruise_fraction":[0.48,0.5,0.52]}' \
  --seeds 101,102 --validation-seeds 200,201 --workers 2
```

The marked core is 231 lines under a temporary 250-line guard. The original
200-line target is the later optimization checkpoint.

Retired Ali entrypoints are kept for reproduction, not submission, in
[`docs/history/controllers/ali/`](../../docs/history/controllers/ali/README.md):
the radar MVP, the local official-profile runner and the STP source port.

The controller index for the whole team is [`../README.md`](../README.md).
