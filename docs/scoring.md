# Scoring and ranking reference

Reviewed on 3 November 2026 against `STU4-Sim` main at
`c010564824c48488104e648781d5e987b402e47a`, dated 31 October. Its scorer announces
`Backend Sim Version: 2026/10/30`. The dashboard uses this revision's formula,
not the older 1.15 kg formulas preserved below.

## Current points formula

The current [scorer][current-scorer] and [configuration][current-config] give:

```text
time_bonus = 120 / T, except time_bonus = 0 when T = 0
damage_penalty = 2 * D / 209200
fuel_penalty = max(F / 75, 0)

success_points = 100 * (time_bonus - damage_penalty - fuel_penalty + 0.5)
failure_points = 100 * (-r / r0 - damage_penalty - fuel_penalty + 0.5)
```

`T` is scorer elapsed seconds, `D` accumulated damage in joules, `F` fuel used in
kilograms, `r` distance to the target's centre and `r0` that distance at scorer
initialization. Official results use the observed STP counters. Local estimates
use the recorded local equivalents and are labelled as such. Higher points are
better. The result is not a percentage, is not capped at 100 and may be negative.

Damage failure at `D > 209200` takes precedence over arrival at `r <= 50 m`.
Equality at the damage threshold still permits arrival. There is no speed,
attitude or dwell gate, and no hard timeout in the published scorer. Arrival
speed, collision count, burns and turns do not add separate score terms.

### How 175 points is possible

This is a hypothetical example, not a recorded flight or an official 175-point
result. A success at `T = 80 s`, `D = 0 J`, `F = 18.75 kg` gives:

```text
150 time - 0 damage - 25 fuel + 50 offset = 175 points
```

The current formula fixtures are separate from the archived ones. Every row
uses `r0 = 10000 m`; penalties below are positive values to subtract.

| Reference ID | `T`, s | `D`, J | `F`, kg | `r`, m | Outcome | Time or distance | Damage | Fuel | Offset | Points |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| `hypothetical_175_success` | 80 | 0 | 18.75 | 50 | Success | 150 | 0 | 25 | 50 | 175 |
| `current_nominal_success` | 120 | 0 | 75 | 50 | Success | 100 | 0 | 100 | 50 | 50 |
| `current_zero_time_success` | 0 | 0 | 0 | 50 | Success | 0 | 0 | 0 | 50 | 50 |
| `current_damage_at_threshold` | 120 | 209,200 | 75 | 50 | Success | 100 | 200 | 100 | 50 | -150 |
| `current_damage_failure` | 120 | 313,800 | 37.5 | 5,000 | Damage failure | -50 | 300 | 50 | 50 | -350 |
| `current_damage_preempts_arrival` | 80 | 313,800 | 18.75 | 50 | Damage failure | -0.5 | 300 | 25 | 50 | -275.5 |
| `current_running_unscored` | 120 | 0 | 75 | 5,000 | Running | Unavailable | Unavailable | Unavailable | Unavailable | Unavailable |

### Read points in the comparison

The score-first browser controls were deployed on 3 November 2026.
**Points** is the first numeric comparison column. It applies the current formula
to each saved terminal run's recorded time, damage, fuel and distance inputs.
**Metric details** shows the time or distance term, damage penalty, fuel penalty,
offset and total. These are current-formula local estimates, not STP receipts,
and do not overwrite archived scores, counters, checksums or formula IDs.

**Summary** shows the mean of complete two-run pair scores. Calculate each run's
points, average the two repetitions for each seed, then average the complete
pairs. Never score median time, damage and fuel as if they described one flight.
Damage failures with complete terminal inputs can contribute; errors, unfinished
runs and missing inputs cannot. Coverage, incomplete pairs, missing values and
error/attempt counts remain visible. Unavailable is not zero.
Point differences from baseline average complete matched pair-score differences;
both profiles need both repetitions. The other metrics retain their median
seed/repetition-matched differences.

The native `competition_reference` preset already uses this same algebra on its
local counters. Other practice presets, including L2, can remain natively
unscored while the saved-run browser comparison calculates a labelled estimate
where complete terminal inputs exist. Do not infer a terminal result from a
partial plot or software-error trace.

## Current fuel, damage and clock limits

The [current actuator helper][current-actuator-fuel] corrects two problems in the
29 October pin. It uses elapsed simulation time and converts jet acceleration
back to impulse with vehicle mass. It no longer accumulates the old unscaled
per-iteration activity value.

For activity matrix `A`, define `M = max(max(onTimes), 0)`. If `M < 1e-12`, the
helper returns zero impulse and fuel. Otherwise, for each jet:

```text
timeToFire_i = remap(max(onTimes[i], 0), 0, M, 0, min(dt_tick, M))
activity_i = min(timeToFire_i, dt_tick) * A[i]
linear_impulse_i = mass * activity_i[0:3]
angular_impulse_i = inertia @ activity_i[3:6]
delta_F = sum_i(norm(linear_impulse_i) / (80 * 9.8))
```

`A` still contains force/mass and inverse-inertia times torque. The helper sums
individual jet impulse magnitudes for fuel, not the magnitude of their resultant.
Jets used for rotation can consume fuel even when translation cancels.

The [calling loop][current-actuator-loop] reads mass, inertia and both commands,
applies their norm caps, solves the six-axis allocation, then passes the actual
`SimClock` timestamp difference to the helper. It skips nonpositive elapsed time.
The configuration requests 60 Hz with `DirectCommand = false`, so the loop applies
the summed linear and angular impulses in the body frame. It does not assume
`dt_tick = 1/60`. The minimum-on-time test selects the `OnThrustersMain` index
list; that list does not filter the helper's impulse or fuel calculation.

Our [physics integrator](../simulation/src/physics.rs) also charges force times
elapsed time divided by `80 * 9.8`. For autonomous centre-of-mass commands,
[`apply_actuator_forces`](../simulation/src/physics/live_tuning.rs) uses
`mass * gain * norm(commanded_accel)` as its fuel force. It applies angular torque
directly without an additional angular fuel charge. Manual nozzles and practice
boost instead sum their own applied jets' force magnitudes. They do not reproduce
the official six-axis allocation, nozzle geometry or firing-time remap. There is
no constant mass or frequency conversion that supplies the missing allocation
history. Both models now have an impulse-based kilogram interpretation; that
alone does not make their fuel totals equal.

Damage remains a separate gap. The [official answer][damage] describes collision
energy from physics hit outputs, including infrastructure and Phobos contacts.
The configuration initializes `Stats/Damage` on the `E_SpaceBuggy` Blueprint, but
the reviewed repositories do not provide its complete hit-to-counter code. The
local closing-energy contact estimate does not establish parity.
Locally, a newly active lander/contact pair adds
`0.5 * reduced_mass * closing_speed²`. Closing speed uses the maximum positive
contact-normal component of the pre-step relative velocity. A continuing contact
does not add that charge every step. This is the local accumulator, not a recovered
equation for the official Blueprint.

The [current scoring loop][current-scorer-loop] starts its clock and `r0` at scorer
initialization, assesses the preceding iteration's elapsed accumulator, and
requests 10 Hz through a Python sleep. It continues after a terminal alert instead
of freezing physics. The local engine uses its physics clock and freezes at the
terminal substep. Licensed clock, contact and impulse-effect behaviour remain
unvalidated. For a submission, retain the actual official score, counters, video
and logs. Source agreement is not runtime certification.

The source failure equation divides directly by `r0`. The browser requires a
recorded positive time-zero range for a failure estimate. Saved paths use their
actual time-zero sample. Live profile results may use the time-zero row in the
existing bounded native `result.analytics`, whose range is column index 7. The
live job's measurement object does not supply initial range. Without that actual
reading the failure estimate is unavailable; neither preset geometry nor a newer
engine supplies a substitute. This needs no protocol change. The current native local
scorer instead uses a zero distance term for a degenerate initial range. That is
a local guard, not an upstream rule; none of the examples uses that case.

## Which source answers which question?

- [Rules & Info][rules] declares itself authoritative when competition information
  conflicts. Its submission section defines a two-run average and organizer
  validation. It does not give the per-run equations needed to resolve the
  wiki/code differences below.
- [Mission Success, Failure, and Scoring][wiki] describes scoring and says arrivals
  belong above non-arrivals. The linked page is pinned to wiki revision
  `4ddae12f7d577b09c27dbfc9cab886ef98e693c5`, dated 19 October 2026.
- [Current Scoring.py][current-scorer] is the published executable per-run algebra.
  The [current competition configuration][current-config] supplies its constants. These are source
  evidence, not an observation of a licensed runtime.
- Official answers clarify [collision energy and arrival][damage],
  [fuel/timing variability and validation][variance], and
  [the current debris seed][seeds]. Discussion comments can change. Their dates
  and comment anchors identify what was reviewed.

### Preserved 29 October source record

The archived simulation pin is `c1e3fc8f9ee3b189bba64d8bd87818a8003639cc`. The then-observed
`main` was `3d7c342cd28817345bcd237279c44c95f9a990d1`, dated 26 October 2026.
GitHub's trees show identical blobs for all three scoring-related files at both
commits.

| File in `SimDynamX/STU4-Sim` | Git blob SHA-1 | Bytes |
| --- | --- | ---: |
| `Systems/Scoring.py` | `388558e7fac38a1b7bfb73fd37c0a303f58fa0fb` | 6,862 |
| `Systems/JetSelectionActuator.py` | `2d7f0f1ddad2e6c275e32af387196381dac9e6f5` | 11,815 |
| `Sims/COMPETITION_SIM.simconf` | `4a228831ea71c21e9aad304914abbf95d11bf5ed` | 822,061 |

The [machine-readable reference](../src/wsorb/scoring_reference.json) records
both revisions, constants, disagreements and worked examples. Its formula IDs are
repository labels, not version names assigned by the competition organizers.
Schema version 1 is documentation data, not executable scenario configuration. The
`algebra` strings are explanatory, not expressions to evaluate. `current_main`
holds the current pinned URLs, corrected counter semantics and dashboard constants;
`current_scoring` holds current examples and the local-estimate policy.
`archived_scope` names the fields preserved from the older review. `worked_examples`
contains inputs, outcomes and published-code point receipts. `documented_model`
separately records the wiki formula and its 50-point and 75-point examples.
Null ranking fields mean the
reviewed sources do not specify a policy; null running-score fields mean no
terminal score is available. Content updates change `reference_version`;
incompatible shape changes also change `schema_version`.

## Archived 29 October scorer and wiki

Everything in this section describes `c1e3fc8f`, not current main. The
[pinned scoring wiki][wiki] defines the successful-run equation as:

```text
Time Score = 120 / T
Damage Penalty = 2 * D / 209200
Fuel Penalty = F / 1.15
Raw Score = Time Score - Damage Penalty - Fuel Penalty
Final points = 100 * (Raw Score + 0.5)
```

Its nominal success scores 50. Its worked example `T = 96`, `D = 10460`,
`F = 1.035` gives `1.25 - 0.10 - 0.90 = 0.25` raw score, then 75 points. The
wiki says distance replaces time on failure but does not define that equation or
numerical category enforcement. The executable formula below is a separate
source. The raw score is dimensionless; the offset and factor of 100 belong to
final points.

Let `T` be the scorer's elapsed seconds, `D` the official `Stats/Damage` counter,
`F` the official `Stats/FuelUsed` counter, `r` the current distance to the return
ship's center, and `r0` that distance when the scoring system initializes.
The scorer labels damage in joules and fuel in kilograms. The fuel accumulation
code needs the qualification in the counter section below.

| Reference | Configured value |
| --- | ---: |
| Nominal completion time | 120 s |
| Damage threshold | 209,200 J |
| Nominal fuel counter | 1.15, labelled kg |
| Arrival radius | 50 m |
| Raw-score offset | 0.5 |
| Points per raw-score unit | 100 |

The code applies these equations without clamping the result.

```text
time_bonus = 120 / T, except time_bonus = 0 when T = 0
damage_penalty = 2 * D / 209200
fuel_penalty = max(F / 1.15 - 1, 0)

success_score = 100 * (time_bonus - damage_penalty - fuel_penalty + 0.5)
failure_score = 100 * (-r / r0 - damage_penalty - fuel_penalty + 0.5)
```

Higher numerical scores are better, including negative scores. A score is not a
percentage and 100 is not a maximum. A damage-free success in 120 seconds using
the nominal fuel counter scores **150** under this code, not the wiki's 50.
Fuel at or below 1.15 produces no fuel penalty and no extra fuel-saving bonus.
The published alerts format the score to one decimal and round displayed
counters to two decimals. Recalculating from those rounded counters can lose
information; the display format does not establish leaderboard tie precision.

"Speed" in this score means completion time. Arrival velocity, angular velocity,
collision count, burn count and turn count are not additional score inputs.
Collisions affect the score through accumulated damage, not a fixed point cost
per contact.

### Terminal checks and timing

[The loop][scorer-loop] checks these conditions in order.

1. `D > 209200` causes a damage failure. The test is strictly greater than, not
   greater than or equal to. Damage failure takes precedence even inside the
   arrival radius.
2. Otherwise, `r <= 50` causes success. There is no arrival-speed, attitude or
   dwell-time gate in the published scorer. At exactly the damage threshold,
   arrival can still succeed.
3. Otherwise, no terminal score is emitted yet. Running, paused, cancelled and
   locally budget-exhausted trials must not acquire an invented zero score.

The [25 October official answer][damage] describes arrival as within 50 metres
of the mothership's center of mass. It recommends aiming below 300 seconds, but
does not establish a hard 300-second timeout. The published loop has no such
timeout either. Unlimited fuel does not create a fuel-exhaustion terminal state.

`Scoring.py` starts its clock and records `r0` when that system initializes. It
assesses the current counters and position using the elapsed-time accumulator
from the preceding loop iteration, then refreshes that accumulator. The
configuration requests a 10 Hz loop through a Python sleep. This is not a
guaranteed 0.1-second simulation-clock cadence. The first assessment can use
zero elapsed time, whose time bonus is explicitly zero.

Success and failure flags prevent a second terminal result. The published
scoring process itself continues looping after either result. It does not stop
the physics engine. This is narrower than the wiki's statement that arrival
"stops the clock and ends your run."

## Archived counter meanings and practice receipts

### The old actuator omitted time and mass

The archived [JetSelectionActuator.py][actuator-fuel] forms a six-component activity vector
for each jet. Its first three components are the jet force divided by vehicle
mass. It then accumulates

```text
delta_F = sum_i(norm(abs(onTimes[i]) * A[i, 0:3]) / (80 * 9.8))
F = previous_F + delta_F
```

This happens once per actuator iteration. The multiplication by `dt_tick` is
commented out. There is no multiplication back by vehicle mass to convert
acceleration to force. The configured loop requests 60 Hz and jet allocation,
with `DirectCommand = false`. The sum is over individual jet activity, not the
norm of the resultant acceleration. Jets used for rotation can therefore add
fuel even when their translational effects cancel.

This analysis remains valid for that pin only. Current main replaces this helper
with the time- and mass-aware impulse calculation described above. Do not describe
the current fuel counter as broken or incompatible with SI kilograms by definition.

On [27 October][variance], collaborator AlexSein98 acknowledged PC-dependent
fuel results from asynchronous Python systems and zero-order-held forces and
torques. The reply said backend improvements might follow; the current source has
changed. Recheck upstream pins
before a submission instead of treating this reference as permanent.

### Damage is accumulated collision energy

The [official clarification][damage] says STP checks shuttle/debris mesh
intersections and obtains collision energy from the physics engine's hit
outputs. Collisions with surface infrastructure and Phobos also count. It does
not provide a complete accumulator equation or a minimum debris size that can
be ignored.

The configuration selects the `E_SpaceBuggy` Blueprint and initializes
`Stats/Damage` to zero. The reviewed repositories do not publish that
Blueprint's hit-to-counter implementation. Our local contact-energy estimate
is not proof of damage-counter parity. Neither a contact count nor a guessed
kinetic-energy formula can replace the observed official counter.

### Historical practice scores

Older versions of [`simulation/src/mission.rs`](../simulation/src/mission.rs)
applied the published code algebra to local fuel and damage estimates. They used
the exact local physics clock and froze the local mission at the terminal physics
substep. They also
guarded the failure-distance denominator with the minimum step when the initial
distance is degenerate. The upstream `DistancePenalty` divides directly by
`r0`. None of the examples here uses a degenerate initial distance.

Those totals, including the earlier roughly -7,000 and -26,000 values, are not
STP point comparisons. Changing a penalty, dividing fuel by mass or assuming a
fixed 60 Hz cannot recover the old official counter history. Existing archives
are not rewritten. Some later practice builds suppressed all native scores;
the current competition-reference preset emits current-formula local estimates,
while other native practice modes remain unscored.
The receipt ID `practice_normalized_time_damage_fuel_v1` identifies a local
score, not an official result. The Python
[`native_score_breakdown`](../src/wsorb/scoring.py) function validates and copies
a native receipt. It is not a second scorer and does not reconstruct absent
terms.

## Official submission ranking

The [authoritative submission rules][rules] specify the following.

- The final submission contains **two consecutive runs**, using the same
  solution code, recorded in the same video.
- The two run scores are averaged. Average the scores, not the time, damage and
  fuel counters before scoring. For example, two accepted scores of 150 and 50
  have a mean of 100.
- Program crashes and failures to start may be discarded between consecutive
  runs if recording continues while the problem is resolved. This does not
  establish permission to drop a scored damage failure or cherry-pick isolated
  successes within the required pair.
- Teams may make further attempts and update submissions before the deadline.
  Qualification submission is required for prize eligibility. A submission is
  a score claim; organizers validate top claims for leaderboard consideration.

The [27 October collaborator reply][variance] adds that validation simulations
will run on the competitor's end, with two consecutive uncut runs. It mentions
a new debris seed as well as uncorrelated timing variability. The
[22 October maintainer answer][seeds] says the current competition configuration
uses the given debris seed, with run-to-run propagation and startup variation.
The pinned [SpawnDebris.py][spawn] hardcodes seed 100. These statements do not publish
a final seed list or a matched-seed averaging protocol.

### Ranking details that remain unresolved

The wiki explicitly says all non-arrivals are in a lower score "category" than
all arrivals. That is published guidance and should not be omitted. However,
the code's numerical ranges overlap. Its uncapped fuel penalty can make a
successful score lower than a damage-failure score, as the examples below show.
`Scoring.py` contains no leaderboard or category-ranking implementation.

The reviewed rules, pinned wiki, source and current competition discussions do
not specify:

- how to combine the wiki's arrival category with the required two-run mean
  when the pair has mixed outcomes;
- a tie-break sequence or whether displayed one-decimal scores decide ties;
- an aggregation across a school's teams or across teammates' controllers;
- an official score for an unfinished run, software error or local time-budget
  stop, or a general DNF ordering beyond the wiki's arrival/non-arrival claim.

Do not invent those rules. Ask the organizers to settle them. Numerical
descending order is useful locally, but it is not enough to claim exact
official leaderboard ordering.

## Preserved documentation conflicts

This table records the 29 October wiki/code disagreement. Current main now uses
75 kg and `max(F / 75, 0)`, so its nominal success is 50, not the old code's 150.
The historical 1.15 kg examples are not current dashboard examples. Damage
boundary, arrival-category and clock qualifications still apply.

| Topic | Published wiki or answer | Published code |
| --- | --- | --- |
| Fuel penalty | `F / F_nom`; nominal success scores 50 | `max(F / F_nom - 1, 0)`; nominal success scores 150 |
| Wiki's worked example | `T = 96`, `D = 10460`, `F = 1.035` gives 75 | The same counter values give 165 |
| Failure boundary | Reaching maximum damage ends the run | Only `D > D_max` fails |
| Arrival category | Every arrival is above every non-arrival | No disjoint score ranges or leaderboard comparison |
| Completion clock | Measured from simulation start; arrival ends the run | Scorer-initialization origin, preceding elapsed accumulator, loop continues |

Rules & Info has priority as competition policy, but does not resolve these
equations and implementation details. Preserve the conflict instead of silently
substituting either formula into production. Actual submitted official receipts
and organizer decisions remain the competition evidence.

## Archived worked algebra examples

These are hand-worked reference fixtures, not recorded flights, engine mocks or
proof of runtime equivalence. `r0 = 10000 m` in every row. Fuel values are the
official counter's numerical values, not assertions about physical kilograms.

| Reference ID | `T`, s | `D`, J | `F` | `r`, m | Code outcome | Code score |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| `nominal_success` | 120 | 0 | 1.15 | 50 | Success | 150 |
| `wiki_example_under_code` | 96 | 10,460 | 1.035 | 50 | Success | 165 |
| `negative_success` | 120 | 0 | 9.2 | 50 | Success | -550 |
| `damage_at_threshold` | 120 | 209,200 | 1.15 | 50 | Success | -50 |
| `damage_failure` | 120 | 313,800 | 1.15 | 5,000 | Damage failure | -300 |
| `damage_preempts_arrival` | 60 | 313,800 | 1.15 | 50 | Damage failure | -250.5 |
| `zero_time_success` | 0 | 0 | 1.15 | 50 | Success | 50 |
| `running_unscored` | 120 | 0 | 1.15 | 5,000 | Running | Unavailable |

For `wiki_example_under_code`, the terms are `125 - 10 - 0 + 50 = 165` points.
For `damage_failure`, they are `-50 - 300 - 0 + 50 = -300` points.
`negative_success` scores below `damage_failure`, which is why the wiki's
category rule cannot be inferred from the numeric formula alone.

[`tests/test_scoring_reference.py`](../tests/test_scoring_reference.py) checks
the closed version-1 reference shape, pinned-source metadata, scenario constants
and explicit fixture values. It checks the fixtures' compatibility with the
existing receipt validator without starting an engine, importing the SDK or
fetching the network. The documented examples are reference checks, not observed
STP flights. Current examples separately check the 75 kg formula and do not turn
these archived fixtures into new flight evidence.

## Local teammate comparisons and efficiency metrics

The README and app's matched-seed comparison is a local experiment, not an
official team or school leaderboard. Compare measured duration, fuel, damage,
success counts and activity without ranking incompatible legacy point totals.
Declare the seed list, scenario/settings,
engine build, controller version and budgets, and use the same cohort for every
profile. Current-formula browser estimates are a separate derived metric, not a
rewrite or endorsement of archived point receipts. The closed historical
`local_comparison.score_compatibility` record still says
`reason = incompatible_local_si_counters` and `stp_points_available = false`.
Existing publication validators depend on that exact record. Its reason describes
the old comparison, not the corrected current actuator. Current estimate metadata
instead says which local terminal counters and source formula were used.

State which terminal outcomes enter each score summary and report the scored
sample count. Retain software failures and unfinished trials in attempted-run
and outcome counts. A missing terminal score is unavailable, never zero, and a
missing trial is not a successful trial. Any success-first sort, tie-break or
exclusion policy used by the local table must be named as local.

Burn counts, burn durations, angular-actuation counts and durations, physical
turn counts and durations, rotation travelled, arrival velocity and wall-clock
cost explain controller behavior. They do not add terms or weights to the
published score. Distinguish angular actuation from physical turning because a
vehicle can rotate while coasting. Report the analytics schema, thresholds,
simulation-time integration resolution and source coverage. Missing historical
metrics stay unavailable; sparse API polls do not establish exact episode counts
or durations.

[current-scorer]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/Scoring.py#L41-L67
[current-scorer-loop]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/Scoring.py#L87-L141
[current-config]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Sims/COMPETITION_SIM.simconf#L41752-L41828
[current-actuator-fuel]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/JetSelectionActuator.py#L129-L147
[current-actuator-loop]: https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/JetSelectionActuator.py#L269-L382
[rules]: https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218
[wiki]: https://github.com/SimDynamX/STU4-Solution/wiki/2.-Mission-Success,-Failure,-and-Scoring/4ddae12f7d577b09c27dbfc9cab886ef98e693c5
[scorer]: https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Scoring.py#L37-L67
[scorer-loop]: https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/Scoring.py#L89-L152
[config]: https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Sims/COMPETITION_SIM.simconf#L41805-L41828
[actuator-fuel]: https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/JetSelectionActuator.py#L69-L126
[damage]: https://github.com/SimDynamX/SpaceTeamsPRO/discussions/230#discussioncomment-19180829
[variance]: https://github.com/SimDynamX/SpaceTeamsPRO/discussions/232#discussioncomment-19226860
[seeds]: https://github.com/SimDynamX/SpaceTeamsPRO/discussions/227#discussioncomment-19116956
[spawn]: https://github.com/SimDynamX/STU4-Sim/blob/c1e3fc8f9ee3b189bba64d8bd87818a8003639cc/Systems/SpawnDebris.py#L72-L77
