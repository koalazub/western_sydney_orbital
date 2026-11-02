# Official-profile comparison

This comparison runs on the local **Competition reference · 1.31 km hop** preset
with the **Official debris cloud**. It is a local practice comparison, not an STP
submission, leaderboard result or licensed-runtime measurement.

The historical [teammate comparison](../README.md#recorded-teammate-flights) is
separate: it used the older broad-stream world. This page covers the official
geometry, the fixed 500-body cloud and the published example controller.

## Policies

| Policy | What it is | Source |
| --- | --- | --- |
| `official_basic_beeline` | The published `BasicController.py` behaviour: 10 m/s goal velocity, unity translation gain, body +X alignment, no debris avoidance | [`BasicController.py`](https://github.com/SimDynamX/STU4-Solution/blob/8545c085426a71c39cbe1d544a844b3edb757007/BasicController.py) |
| `wso_official_profile` | WSO's official-profile beeline with fresh-radar potential avoidance and a current-lease sweep. This is the policy used by the official-profile arrival regression | `src/wsorb/flight/official_profile.py` |
| `baseline` | WSO's existing portable checkpoint controller | `src/wsorb/stp_policy.py` |
| `body-axis` | The same checkpoint controller with body-axis candidate preference | `src/wsorb/stp_policy.py` |
| `basic_pd_radar` | The older simple position/velocity PD with short-range radar repulsion, used as a local reference | `src/wsorb/flight/control.py` |

The official baseline deliberately has no debris avoidance. An arrival by that
policy is not evidence that its path was safe or that it would reproduce in the
licensed runtime.

## Matched conditions

- Preset: `competition_reference`, 1,305.8 m target, 50 m arrival region.
- Debris: `official_cloud`, 500 physical bodies, seed 100 in the published source.
- Seeds: 100, 101 and 102, with two consecutive unchanged-code repetitions per
  seed and policy.
- Horizon: 300 s; controller step 0.5 s; physics step 0.05 s.
- Caps: 0.5 m/s² linear and 0.05 rad/s² angular.
- Every attempt starts a fresh world; no debris count, speed, size or noise is
  reduced.
- Radar is the 200-of-500 rolling official scan, including each row's own
  acquisition timestamp. No hidden debris state is supplied to a policy.

Two repetitions per seed mirror the rules' consecutive-pair idea, but the local
averaging is descriptive. Three matched seeds are not three independent
population draws.

## Results

The per-trial records and summary are the published result:

- [Per-trial CSV](analytics/official-comparison/runs.csv)
- [Summary CSV](analytics/official-comparison/summary.csv)
- [Configuration](analytics/official-comparison/configuration.json)
- [Policy source record](analytics/official-comparison/policies.json)
- [Artifact checksums](analytics/official-comparison/manifest.json)

The full interactive report is not committed because it embeds raw public
observation traces. Regenerate it locally with `just flight-analyze` from a
saved run.

Across six attempts per policy (three seeds, two consecutive attempts each):

| Policy | Arrivals | Outcomes | Median arrival, s | Median final speed, m/s | Median local fuel, kg | Runs with damage |
| --- | ---: | --- | ---: | ---: | ---: | ---: |
| Published `BasicController` baseline | 4/6 | 4 success, 2 damage failures | 138.9 | 10.00 | 46.0 | 4/6 |
| WSO official profile | 6/6 | 6 success | 153.2 | 1.20 | 109.0 | 2/6 |
| WSO checkpoint `baseline` | 0/6 | 6 policy-ineligible | — | — | — | 2/6 |
| WSO checkpoint `body-axis` | 4/6 | 4 success, 2 policy-ineligible | 186.9 | 2.20 | 75.0 | 2/6 |
| Basic PD/radar | 6/6 | 6 success | 160.4 | 1.36 | 146.4 | 0/6 |

The published baseline is the fastest, but it enters the arrival region at about
10 m/s and both seed-102 attempts failed the 209,200 J damage threshold
(209,326 J). WSO's official profile arrived on every attempt and entered at
about 1.2 m/s; it observed 49,464 J on seed 101 but stayed below the threshold,
and used more fuel than the baseline. Basic PD also arrived on every attempt, but
later and with more local fuel. The checkpoint profiles were less reliable on
this field: `baseline` never produced an eligible decision, while `body-axis`
arrived on two seeds and became ineligible on the third. Those are real policy
outcomes, not hidden retries.

Completion means native mission success and entry to the 50 m arrival region.
It is not a docking or low-speed rendezvous claim. Damage, local fuel, time and
relative speed are recorded separately. Errors and timeouts stay in the attempt
counts; they are not silently replaced by a successful seed.

The official formula is applied only to local time, damage and fuel estimates.
Local fuel is integrated propellant mass, not STP's `FuelUsed` counter, so the
displayed score is a local diagnostic and not STP points.

## Repeat it

```sh
just build
just flight-sweep examples/official_comparison.json runs/official-comparison-new
```

The output directory must be new. The sweep records the engine build, public
policy sources, configuration and every trial trace in that directory. To
regenerate the HTML from saved trials, run:

```sh
just flight-analyze runs/official-comparison-new
```

## Limits

- Local terrain, absolute frame, gravity, contacts, fuel and damage differ from
  the licensed mission. The preset ports the relative 1,305.8 m geometry onto
  the local far-side site.
- The official baseline's attitude command is represented by the same bounded
  shortest-arc law used by the WSO official profile, so this comparison isolates
  translation and avoidance rather than claiming a line-by-line port of the
  example's torque loop.
- The checkpoint profiles are older WSO controllers. Their behaviour here does
  not describe the WSO official-profile policy or the official baseline.
- No result on this page is a competition score, a safety certificate or an STP
  leaderboard position.
