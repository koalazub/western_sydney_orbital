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

The marked core is 1430 lines. The original 200-line target remains a guideline,
not a hard limit; the numeric test keeps a 1435-line regression guard. The
current 8 m/s default is the line-fit tracker, 25 s velocity avoider, a 10/20 m
goal corridor, the 0.3/0.1 m/s governor deadband, no launch clearance, and the
+Z thrust axis. The emergency guard remains the last-resort layer. The high-speed
braking-speed cap was removed: at 17 m/s it forced the ship down to 14.5 m/s
before the 200 m terminal coast.

Against the native port of Tom's 8 m/s polyfit on seeds `100-107`, Ali averaged
`67.87` points, `166.63 s` and `40.62 kg` versus Tom's `66.72`, `166.32 s` and
`41.49 kg`, with zero damage on all eight. Seeds 102, 103 and 104 won points,
time and fuel outright; 101 won points and fuel while arriving 0.8 s later;
100, 105 and 106 lost points by 0.4-0.9. Against the Tom Kalman port at 17 m/s,
Ali averaged `75.83` points, `91.45 s` and `79.03 kg` versus `62.81`, `91.13 s`
and `89.16 kg`; seven of eight seeds won points and fuel, six were within 0.2 s,
and seed 106 lost 2.3 s to its close-threat dodge. The remaining per-seed gaps
are at the simulator's 0.3-1.0 point repeatability limit; seed 106's avoidance is
the least stable. The Kalman, raw-radar guard, inflation, fixed-sample,
proportional-governor and STP-source modes remain opt-in for seed studies.

Retired Ali entrypoints are kept for reproduction, not submission, in
[`docs/history/controllers/ali/`](../../docs/history/controllers/ali/README.md):
the radar MVP, the local official-profile runner and the STP source port.

The controller index for the whole team is [`../README.md`](../README.md).
