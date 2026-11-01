# Official competition reference

This page records the Fall 2026 **Escape from Phobos** values used by the local
`competition_reference` preset and `official_cloud` debris mode. It is a source
reference for a local port, not a certification that this practice simulator
matches Space Teams PRO. The values below come from the cited source files, not
from a licensed runtime observation.

Sources were checked on 2 November 2026 against:

- [`SimDynamX/STU4-Sim` main at `c0105648`][sim-main] (31 October 2026).
- [`SimDynamX/STU4-Solution` main at `8545c085`][solution-main] (19 October 2026).

## Official values

| Item | Official value | Source |
| --- | --- | --- |
| Debris bodies | 500 | [`SpawnDebris` parameters in `COMPETITION_SIM.simconf`][simconf] |
| Cloud centre | Shuttle position plus 500 m along local up | [`SpawnDebris.py`][spawn] |
| Cloud half-extents | ±300 m body-frame x, ±300 m body-frame y, ±500 m body-frame z, around a centre offset 500 m along local up | [`SpawnDebris.py`][spawn], [`COMPETITION_SIM.simconf`][simconf] |
| Spawn seed | 100 | [`SpawnDebris.py`][spawn] |
| Tangential drift | 0.05–0.10 of orbital speed; about 0.40–0.80 m/s | [`SpawnDebris.py`][spawn] |
| Debris classes and weights | Rock1 45%, Rock2 35%, Rock3 15%, ExplodedTank 5% | [`COMPETITION_SIM.simconf`][simconf] |
| Maximum debris radius | 20 m | [`Rock2.enconf`][rock2] |
| Shuttle-to-target route | 1305.8 m between the initial `SpaceBuggy` and `AsteroidCycler` locations | [`COMPETITION_SIM.simconf`][simconf] |
| Arrival keep-out | 50 m | [`Scoring` parameters in `COMPETITION_SIM.simconf`][simconf] |
| Damage threshold | 209,200 J | [`Scoring.py`][scoring], [`COMPETITION_SIM.simconf`][simconf] |
| Nominal fuel | 75.0 kg | [`Scoring.py`][scoring], [`COMPETITION_SIM.simconf`][simconf] |
| Nominal time | 120 s | [`Scoring.py`][scoring], [`COMPETITION_SIM.simconf`][simconf] |
| Linear command cap | 0.5 m/s² | [`SpaceBuggy` thrusters in `COMPETITION_SIM.simconf`][simconf] |
| Angular command cap | 0.05 rad/s² | [`SpaceBuggy` thrusters in `COMPETITION_SIM.simconf`][simconf] |
| Radar sample | 200 of the 500 bodies, `Debris0` through `Debris199` | [`Radar` parameters in `COMPETITION_SIM.simconf`][simconf], [`Radar.py`][radar] |
| Radar noise (1σ) | 2 m position, 0.02 m/s range rate, 5% diameter | [`COMPETITION_SIM.simconf`][simconf] |

The orbital speed used by `SpawnDebris.py` is `sqrt(mu / R)` with Phobos
`mu = 707500 m³/s²` and `R = 11100 m`, giving about 7.98 m/s. The 0.05–0.10
speed interval therefore spans about 0.40–0.80 m/s. Velocity direction is a
random tangent; angular velocity is separately bounded at 0.2 rad/s.

## Rock sizes and mass

Each body first chooses a class by weight, then a scale in that class's
`ScaleMin`–`ScaleMax` range. `RadarDiameter_m` and visual radius scale linearly
with that size, while mass scales with size cubed.

| Class | Weight | Base mass | Scale range | Radar diameter | Radius range |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Rock1][rock1] | 45% | 0.63 kg | 10–400 | 0.08 m | 0.4–16 m |
| [Rock2][rock2] | 35% | 0.30 kg | 20–800 | 0.05 m | 0.5–20 m |
| [Rock3][rock3] | 15% | 0.20 kg | 40–1000 | 0.01 m | 0.2–5 m |
| [ExplodedTank][tank] | 5% | 700 kg | 1 | 17 m | 8.5 m |

The radius is half the scaled radar diameter. Mass is
`base_mass × size³`. The largest possible radius is 20 m, from Rock2 at scale
800. These are source values; the local port may quantise or approximate them.

## Scoring

The current `Scoring.py` uses these equations:

```text
time_score = 120 / T                  (0 when T = 0)
damage_penalty = 2 * D / 209200
fuel_penalty = max(F / 75.0, 0)

raw_success = time_score - damage_penalty - fuel_penalty
raw_failure = -r / r0 - damage_penalty - fuel_penalty

success_points = 100 * (raw_success + 0.5)
failure_points = 100 * (raw_failure + 0.5)
```

`T` is completion time in seconds, `D` is the official `Stats/Damage` counter
in joules, `F` is the official `Stats/FuelUsed` counter in kilograms, `r` is
the current distance to the target and `r0` is the initial distance. The
published code does not clamp the final score. Damage failure is tested as
`D > 209200`; at exactly the threshold, arrival can still succeed. Arrival is
`r <= 50 m` with no arrival-speed or attitude gate in `Scoring.py`.

The example controller in `STU4-Solution` is a 10 m/s beeline with attitude
alignment: [`BasicController.py`][basic]. It is a reference implementation, not
a claim that the local controller or local flight matches it.

## Radar

`SpawnDebris` creates 500 bodies, but the `Radar` system in
`COMPETITION_SIM.simconf` requests 200. `Radar.py` reads `Debris0` through
`Debris199`, applies the position, range-rate and diameter noise above, and
publishes a distance-sorted table. The local `official_cloud` mode uses the
200-of-500 sampling rule. The transit stress mode keeps its earlier nearest-200
sampling. Neither is a certified STP sensor or timing implementation.

## Remaining local differences

This repository does not claim one-to-one parity with the official mission.

- **Terrain and frame.** The local launch site comes from procedural Phobos
  terrain in a Phobos-centred Mars–Phobos rotating frame. The official
  `SpaceBuggy` and `AsteroidCycler` positions use the competition configuration
  and its body-fixed frame. The local site radius, absolute coordinates and
  terrain are not the official ones; the 1305.8 m separation and 50 m keep-out
  are the values being ported.
- **Fuel and damage counters.** Local fuel is an estimated SI mass and local
  damage is a contact-energy estimate. Neither is the official
  `Stats/FuelUsed` or `Stats/Damage` counter. The local dashboard cannot emit
  comparable STP points.
- **Transit traffic.** `transit_stream` is a custom 20 m/s corridor and a stress
  test. It is not the official debris cloud and is not the competition field.
- **Seed.** The official `SpawnDebris.py` hardcodes seed 100. The local new-world
  control can request another seed for practice, so a local draw is not the
  official draw unless it also uses seed 100 and the same distribution.
- **Gravity and sensors.** The local model uses a circular point-mass CR3BP
  approximation and a practice radar/telemetry implementation. It does not run
  the official SPICE ephemeris, sensor bus or scoring process.
- **Local controls.** Pause, step, placement, fault injection and command
  leases are practice tools. They are not additional STP capabilities.
- **Arrival meaning.** Local success means entry to the station region. It is
  not docking, a low-speed rendezvous or an official terminal score.

The existing `far_side_l2` practice world, the 20 m/s transit stress mode and
the 30-trial publication remain unchanged. Those records are local evidence,
not official results.

The earlier [scoring reference](scoring.md) preserves the 29 October review of
the pinned `c1e3fc8f` revision, where the executable fuel penalty used
`NominalFuel_kg = 1.15` and subtracted 1. The current main revision uses 75.0 kg
and does not subtract 1. Keep those revisions distinct when comparing formulas.
Local counters are not official counters under either revision.

[sim-main]: https://github.com/SimDynamX/STU4-Sim/tree/c010564824c48488104e648781d5e987b402e47a
[solution-main]: https://github.com/SimDynamX/STU4-Solution/tree/8545c085426a71c39cbe1d544a844b3edb757007
[simconf]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Sims/COMPETITION_SIM.simconf
[spawn]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/SpawnDebris.py
[scoring]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/Scoring.py
[radar]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/Radar.py
[rock1]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Entities/Rock1.enconf
[rock2]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Entities/Rock2.enconf
[rock3]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Entities/Rock3.enconf
[tank]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Entities/ExplodedTank.enconf
[basic]: https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/BasicController.py
