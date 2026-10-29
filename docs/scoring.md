# Scoring and ranking reference

Reviewed against first-party sources on 29 October 2026. This documents the
published STU4 scorer and the submission rules. It does not certify that our
practice physics, counters or timing reproduce Space Teams PRO.

For the competition, use the actual official run's score, counters, video and
logs. For local comparisons, keep historical practice receipts as diagnostics only.
Do not reconstruct missing scores from a plot or an archived partial flight.

## The documented formula requested for the dashboard

The [scoring wiki][wiki] defines the successful-run equation as:

```text
Time Score = 120 / T
Damage Penalty = 2 * D / 209200
Fuel Penalty = F / 1.15
Raw Score = Time Score - Damage Penalty - Fuel Penalty
Final points = 100 * (Raw Score + 0.5)
```

`T` is completion time in seconds. `D` is accumulated collision damage, not
collision count. `F` must be STP's `Stats/FuelUsed` counter. The constants come
from the pinned competition configuration. A nominal success scores **50**.
The wiki's worked example, `T = 96`, `D = 10460`, `F = 1.035`, has
`1.25 - 0.10 - 0.90 = 0.25` raw score, then **75** final points. The raw score is
dimensionless. The 0.5 offset and factor of 100 belong only to final points.

The wiki says failure substitutes distance for time, but does not publish the
distance equation or numerical category enforcement. The executable equations
below are a separate source, not a complete definition supplied by the wiki.

**The local dashboard cannot report comparable STP points.** Its physical fuel
estimate is not STP's fuel counter. The earlier roughly -7,000 and -26,000 totals
were invalid as STP comparisons. Swapping fuel penalties, dividing fuel by mass,
or assuming a fixed 60 Hz actuator rate cannot repair the missing counter history.

New local flights return no score. The dashboard compares measured duration,
estimated SI fuel, damage, burns and turns without an invented combined ranking.
Historical receipts remain unchanged in archives and explicitly labelled legacy
diagnostics in downloads. Version-2 publications carry a closed
`score_compatibility` record with `stp_points_available = false`; matching legacy
formula IDs and seed coverage do not override it.

## Which source answers which question?

- [Rules & Info][rules] declares itself authoritative when competition information
  conflicts. Its submission section defines a two-run average and organizer
  validation. It does not give the per-run equations needed to resolve the
  wiki/code differences below.
- [Mission Success, Failure, and Scoring][wiki] describes scoring and says arrivals
  belong above non-arrivals. The linked page is pinned to wiki revision
  `4ddae12f7d577b09c27dbfc9cab886ef98e693c5`, dated 19 October 2026.
- [Scoring.py][scorer] is the published executable per-run algebra. The
  [competition configuration][config] supplies its constants. These are source
  evidence, not an observation of a licensed runtime.
- Official answers clarify [collision energy and arrival][damage],
  [fuel/timing variability and validation][variance], and
  [the current debris seed][seeds]. Discussion comments can change. Their dates
  and comment anchors identify what was reviewed.

The simulation pin is `c1e3fc8f9ee3b189bba64d8bd87818a8003639cc`. The observed
`main` was `3d7c342cd28817345bcd237279c44c95f9a990d1`, dated 26 October 2026.
GitHub's trees show identical blobs for all three scoring-related files at both
commits.

| File in `SimDynamX/STU4-Sim` | Git blob SHA-1 | Bytes |
| --- | --- | ---: |
| `Systems/Scoring.py` | `388558e7fac38a1b7bfb73fd37c0a303f58fa0fb` | 6,862 |
| `Systems/JetSelectionActuator.py` | `2d7f0f1ddad2e6c275e32af387196381dac9e6f5` | 11,815 |
| `Sims/COMPETITION_SIM.simconf` | `4a228831ea71c21e9aad304914abbf95d11bf5ed` | 822,061 |

The [machine-readable reference](../src/wsorb/scoring_reference.json) records
these pins, constants, disagreements and worked examples. Its formula IDs are
repository labels, not version names assigned by the competition organizers.
Schema version 1 is documentation data, not executable scenario configuration. The
`algebra` strings are explanatory, not expressions to evaluate. `worked_examples`
contains inputs, outcomes and published-code point receipts. `documented_model`
separately records the wiki formula and its 50-point and 75-point examples.
Null ranking fields mean the
reviewed sources do not specify a policy; null running-score fields mean no
terminal score is available. Content updates change `reference_version`;
incompatible shape changes also change `schema_version`.

## Published per-run score

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

## Counter meanings and local differences

### Fuel is an official counter, not our SI estimate

[JetSelectionActuator.py][actuator-fuel] forms a six-component activity vector
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

Do not replace `F` with a physical kilograms estimate and claim identical
official scoring. Our practice engine uses its own estimated SI fuel model.
Matching `Isp = 80` and `g0 = 9.8` does not make the accumulation methods equal.

On [27 October][variance], collaborator AlexSein98 acknowledged PC-dependent
fuel results from asynchronous Python systems and zero-order-held forces and
torques. The reply says backend improvements may follow. Recheck upstream pins
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
guards the failure-distance denominator with the minimum step when the initial
distance is degenerate. The upstream `DistancePenalty` divides directly by
`r0`. None of the examples here uses a degenerate initial distance.

These counter differences make those totals unsuitable as STP point comparisons,
not merely uncertified results. New local assessments retain arrival and damage
outcomes but leave score and score breakdown unavailable. Existing archives are
not rewritten.
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

## Known documentation conflicts

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

## Worked algebra examples

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
STP flights. Native mission tests verify local assessments do not invent points.

## Local teammate comparisons and efficiency metrics

The README and app's matched-seed comparison is a local experiment, not an
official team or school leaderboard. Compare measured duration, fuel, damage,
success counts and activity without ranking incompatible legacy point totals.
Declare the seed list, scenario/settings,
engine build, controller version and budgets, and use the same cohort for every
profile.

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
