# Ali controller

[`controller.py`](controller.py) is the current single-file STP submission. The
marked core from `# BEGIN ALI MPPI CORE` to `# END ALI MPPI CORE` is the only
copy; the local adapter and numeric tests load the same region.

`GuidanceController` owns one run and its explicit state enums: `Phase`,
`GuardMode` and `CollisionMode`. Collision transitions, guard decisions, command
arbitration, governor selection and the attitude gate use `match`/`case`, so each
section can be inspected without following a chain of booleans. The math helpers
stay separate.

The runtime profile is `ali_mppi`. The policy is settings-driven: cruise
speed, terminal coast, emergency guard, MPPI plan steering, main-engine gate,
risk/switch weights, attitude brake scale and collision response. The core also
keeps the reviewed estimator and guidance alternatives as settings: `tracker`
(`line`/`kalman`), `guard_source` (`raw`/`tracks`), `risk_inflation`,
`mppi_fixed_samples`, `avoidance` (`mppi`/`avoider`), `governor`
(`full`/`proportional`) and `attitude_control`. The planner traces a zero-control
coast sample and steers when that coast path is risky, rather than waiting for
its selected sample to be risky. This lets the same seeds rerun the review
ablations. `wsorb fuzz` reports the score distribution; `wsorb tune` searches the
settings with a worst-seed objective and held-out validation.

Collision handling uses two independent triggers: any increase in accumulated
damage, or an unexplained velocity change beyond the last command plus a small
allowance. A hit starts a short lateral escape impulse away from the nearest
observed threat. The target then shifts to a parallel route offset sized so the
impulse matches that route, so the ship does not need continuous sideways
thrust. Repeated hits flip the offset side and request a longer impulse within
the offset cap; three hits or damage above `collision_safe_damage_j` also reduce
cruise speed. Offsets are capped so the ship still arrives inside the goal
radius.

```sh
uv run --locked wsorb fuzz --seeds 100-107 --workers 4
uv run --locked wsorb tune --grid '{"cruise_speed_mps":[7.0,8.0,9.0]}' \
  --seeds 101,102 --validation-seeds 200,201 --workers 2
```

The marked core is 1078 lines. The original 200-line target remains a guideline,
not a hard limit; the numeric test keeps a 1100-line regression guard. The
current default is the line-fit tracker, fitted-track guard, MPPI steering on,
no risk inflation, no fixed samples, MPPI beeline, full governor, attitude
control on and an 8 m/s cruise. The guard prefers a lateral dodge whenever it
clears the keep-out and only reverses when no lateral escape is safe; this
removes the backwards launch swing when a rock approaches the pad. On seed 108
the latest clean run produced 45.98 points, 57.05 kg of fuel, zero damage and a
166.6 s arrival; repeated runs of the same logic varied between about 44 and 52
points because of simulator timing;
the ship coasts from about 40 s to the target. The same logic at 14 m/s with
planning off scored 72.38 with 68.72 kg and a 105.2 s arrival on the same seed.
The Kalman, raw-radar guard, inflation, fixed-sample, avoider and
proportional-governor modes remain opt-in for seed studies.

Retired Ali entrypoints are kept for reproduction, not submission, in
[`docs/history/controllers/ali/`](../../docs/history/controllers/ali/README.md):
the radar MVP, the local official-profile runner and the STP source port.

The controller index for the whole team is [`../README.md`](../README.md).
