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
(`line`/`kalman`), `guard_source` (`raw`/`tracks`/`both`, default `both`), `risk_inflation`,
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

Aged radar rows are reconstructed from a short shuttle-position history before
tracking. Radar timestamps count from the radar loop's own startup, so the
controller estimates that origin from the freshest row seen and shifts every row
onto its own clock first; if the measurement epoch is outside the history, the
current position is used. The collision detour side is scored by predicting both
parallel routes against fitted tracks over `collision_side_horizon_s` with
`collision_side_margin_m`. The previous side is kept only when its predicted
clearance is within `collision_side_switch_m` of the safer side; hit parity is no
longer used.

```sh
uv run --locked wsorb fuzz --seeds 100-107 --workers 4
uv run --locked wsorb tune --grid '{"cruise_speed_mps":[7.0,8.0,9.0]}' \
  --seeds 101,102 --validation-seeds 200,201 --workers 2
```

Attitude control and the launch gate track the lander's **+Z thrust axis**;
`+X` remains the nose and capsule axis used for lateral collision geometry.
Aligning `+X` to the launch direction tips a tail-lander onto its side. The
radial clearance phase is retained but starts disabled (`launch_clearance_m`
defaults to `0`), so the transit begins immediately; set it above zero to climb
clear of a crowded pad before cruising.

The emergency guard evaluates fitted tracks and raw radar rows together, so a
close rock is not invisible when association fails. Its keep-out includes the
ship radius, and escape clearance is predicted with the available acceleration
rather than an instantaneous velocity jump.

## Guidance composition

`GuidanceController.step` builds one `Tick` per decision and runs eight ordered
stages: `_sense -> _collide -> _guide -> _pilot -> _guard -> _arbitrate_cmd ->
_steer -> _report`. Each stage owns one responsibility and writes only the
`Tick` fields it names:

- `_sense`: radar clock, observer history, line/Kalman tracks, launch height.
- `_collide`: collision check/route, collision mode, steering target.
- `_guide`: cruise prep, goal velocity, avoider/MPPI nominal and lateral plan.
- `_pilot`: full/proportional governor and plan-lateral composition.
- `_guard`: emergency escape and terminal condition.
- `_arbitrate_cmd`: command-source priority (collision, guard, terminal, launch, plan, avoider, cruise).
- `_steer`: alignment, main-engine gate, attitude, body-frame command.
- `_report`: telemetry record and compact line.

The `_stages` tuple is the composition seam; replacing one stage does not change
the others. The persistent `state` dict remains the adapter/run boundary.

The marked core is 1522 lines across eight stages. The original 200-line target
remains a guideline, not a hard limit; the numeric test keeps a 1530-line
regression guard. The current 8 m/s default is the line-fit tracker, 15 s
velocity avoider, a 35 m stop / 45 m restart goal corridor, a 3 m
emergency-guard surface margin, the 0.3/0.1 m/s governor deadband, no launch
clearance, and the +Z thrust axis. The emergency guard remains the last-resort
layer. The high-speed braking-speed cap was removed: at 17 m/s it forced the
ship down to 14.5 m/s before the 200 m terminal coast.

Against the native Tom ports, Ali won points on every seed at 8, 14 and 17 m/s, with
zero damage in all 24 runs at 2x native playback. At 8 m/s the polyfit comparison
averaged `71.09` vs `66.72` points (`168.79` vs `166.56 s`, `37.51` vs `41.49 kg`), with
the smallest margin `+0.14` on seed 100. At 14 m/s it averaged `82.53` vs `79.23`
(`104.75` vs `104.34 s`, `61.52` vs `64.34 kg`), with the smallest margin `+0.11` on
seed 101. At 17 m/s the Kalman comparison averaged `79.65` vs `62.81` (`91.54` vs
`91.13 s`, `76.09` vs `89.16 kg`), with the smallest margin `+3.39` on seed 106. The 1x
spot checks on the thinnest seed at each speed also win: 8/100 `+0.48`, 14/101 `+0.10`
and 17/106 `+3.02`. The 3 m guard surface margin removes the seed-106 false escape: the
6 m margin fired a 5.8 kg burn when the predicted surface gap was 5.8 m. The 35 m stop /
45 m restart corridor trades a small time increase for 0.5-1.3 kg less fuel. Kalman,
raw-radar guard, inflation, fixed-sample, proportional-governor and STP-source modes
remain opt-in for seed studies.

Retired Ali entrypoints are kept for reproduction, not submission, in
[`docs/history/controllers/ali/`](../../docs/history/controllers/ali/README.md):
the radar MVP, the local official-profile runner and the STP source port.

The controller index for the whole team is [`../README.md`](../README.md).
