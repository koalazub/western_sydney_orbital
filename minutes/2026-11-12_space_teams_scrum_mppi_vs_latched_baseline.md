# Space Teams — Daily Scrum: MPPI Testing versus Latched Baseline

**Date:** Thursday, 12 November 2026  
**Meeting:** Space Teams competition development scrum, including live simulator tests and follow-up debugging  
**Submission deadline:** Friday, 20 November 2026 (exact organiser deadline/time zone not verified in this transcript)  
**Source:** Meeting transcript supplied by Naqib Ibnul  
**Status:** Draft, for team review. Reported scores were observed or stated during the meeting; original simulator logs, seeds, build revision and commit SHAs have not been independently audited.

## Executive summary

Tom Kirkpatrick's latched, burn/coast controller remains the strongest demonstrated baseline from today's live comparisons, scoring **44.5** with approximately **48 kg** of fuel. Ali El Ali's experimental Model Predictive Path Integral (MPPI) controller was able to find a viable travel corridor, but repeated attitude corrections and thruster pulses consumed excessive fuel. Several successive MPPI revisions reportedly scored **−10**, **−47** and **+3**; the latest live experiment was stopped early and is **not a scored run**. The team agreed to retain Tom's controller as the main submission baseline for now, develop alternatives separately, and prioritise controlled comparisons as the deadline approaches.

## Results discussed in the meeting

| Controller / revision | Reported score | Fuel used | Completion time | Observations |
| --- | ---: | ---: | ---: | --- |
| Ali's first MPPI run | −10 | ~95 kg | 180.88 s | Followed a clear corridor without an observed collision, but continued pulsing and did not visibly settle into coasting. |
| Tom's latched controller | **+44.5** | **~48 kg** | Not stated | Coasted near 8 m/s with occasional top-up burns; Tom described the result as typical of his recent runs. Reorientation was disabled for this test. |
| MPPI, subsequent bias-related changes | −47 | Not confirmed | Not confirmed | More costly than the first MPPI run. |
| MPPI, following revision | +3 | Not confirmed | Described as ~100 s faster than the previous run | Less visible oscillation for part of the flight, but still used excessive fuel. |
| Latest MPPI/latching attempt | **Unscored** | Not confirmed | Run stopped early | Continued attitude oscillation; participants judged it unlikely to improve on the baseline. |

**Comparability caveat:** These are individual live runs of evolving code revisions, not a matched-seed benchmark. Do not infer mean performance, success probability or a statistically meaningful ranking from these numbers alone. Collect the source commits and corresponding full scoring logs before including them in a formal comparison.

## MPPI observations and debugging

- The MPPI implementation reportedly considers the nearest **40** asteroids and samples many candidate future paths. A figure of approximately **36,000** candidate paths was mentioned, but its precise definition and computational cost should be checked against the code.
- The first run navigated around obstacles but generated persistent thruster activity. Participants could not confirm its velocity or whether it had entered a true coast phase from the GUI alone.
- Repeated orientation control was suspected of wasting fuel without improving forward velocity or reducing risk. Overshoot and insufficient angular damping were identified as possible causes.
- Proposed improvements included a **burn/attitude latch or dead band**, more deliberate initial orientation, limiting unnecessary attitude updates and adding an angular-rate damping term.
- During follow-up debugging, dead-band values and a derivative gain (including a trial value of **2**) were changed interactively. No validated improvement emerged; code readability and the inability to isolate each change impeded diagnosis.
- A version described as adding fuel-efficient latching was pushed during the meeting, but the immediately observed follow-up flight still appeared to oscillate; **it was not demonstrated to outperform Tom's baseline**.
- Recommended telemetry: simulation time, position and velocity, desired velocity, translational and angular acceleration commands, angular velocity, fuel used, tracked threats, and controller mode (burn/coast/avoid/reorient). Include a controller revision identifier.

## Tom's latched controller

- Tom demonstrated his controller, with reorientation temporarily disabled, and reported a final score of **+44.5** at approximately **48 kg** fuel used.
- Its `BurnGovernor` applies **hysteresis/dead-band control**: it withholds thrust when the velocity error is small rather than continually correcting every deviation. Tom described the burn trigger as around **0.3 m/s**, with top-ups occurring when speed fell to approximately **7.7 m/s** against an approximately **8 m/s** target.
- Participants estimated that Phobos dynamics reduce the shuttle's speed by about **0.01 m/s each second** in the relevant portion of the journey; this was an observation, not a verified constant acceleration over the whole trajectory.
- The current version was described as approximately **420 lines in total**, including the STP scaffolding. Its behaviour was considered relatively explainable, compared with the experimental MPPI controller.
- Proposed next technical focus: assess the current **avoidance decisions**, log closeness to avoidance and governor thresholds, and determine whether a faster initial burn improves the score after accounting for extra fuel and reduced manoeuvre time.

## Fuel–time trade-off and scoring

- The competition was described as having a **75 kg nominal fuel** reference. The MPPI trial's ~95 kg fuel consumption was materially above that reference.
- The team discussed whether accelerating to a higher initial speed could reduce total travel time enough to compensate for additional fuel usage. No optimal speed was established during this meeting.
- An analysis of **time-to-target, fuel consumed, score and number of corrections** across speed targets is needed. Prefer matched random seeds and multiple runs per setting.
- Distinguish fuel used during **initial acceleration**, **course correction**, **asteroid avoidance**, and **attitude control** where telemetry permits; total fuel alone does not identify the source of inefficiency.

## Integration decisions

1. **Tom's current latched controller remains the main branch / competition baseline for now.** Do not replace it with MPPI on the evidence of today's tests.
2. Continue MPPI as a **separate experiment**, with latching, orientation control and damping reviewed independently.
3. Share exact controller revisions and simulator logs through **Discord**; Tom offered to run incoming changes.
4. Prefer small, explainable, isolated modifications before considering changes to the submission candidate.
5. The next scrum was discussed for **Monday, 16 November at 10:30 a.m.** (confirm against the calendar invite).

## Action items

| Owner | Action | Priority |
| --- | --- | --- |
| **Tom Kirkpatrick** | Preserve and continue incremental improvements to the latched baseline; run new candidate revisions shared on Discord, keeping their logs. | High |
| **Naqib Ibnul** | Trial MPPI with latching and a clearer initial orientation policy; compare against Tom's baseline without changing multiple subsystems simultaneously. | High |
| **Naqib Ibnul** | Brief Bui on the meeting and communicate benchmark requirements to absent members. | High |
| **Ali El Ali / MPPI developer** | Publish or identify exact revision(s); document and debug angular control, latching, sampled-path count and required imports. | High |
| **Nic Ralph** | Revisit fuel–coast-speed–time–score relationship and review avoidance behaviour, as discussed. | Medium |
| **Harry / benchmarking contributors** | If available, analyse official run logs across matched seeds and report score variability and failure causes. | High |
| **All testers** | Post code SHA, simulator version, seed/configuration, score, fuel, completion time, damage and complete logs for each scored run. | High |

## Open questions / risks

- Does the MPPI cost function penalise **attitude-control fuel** adequately, or is there a mismatch between predicted and actual thruster costs?
- Are the angular-velocity frame, units and signs correct? Can an angular-rate damper stop overshoot without creating counter-burns?
- Does MPPI replan too frequently, and are the chosen trajectories physically achievable with the revised actuator dynamics?
- How does Tom's baseline perform across a representative seed set, including near-miss and collision cases?
- What is the optimal burn/coast velocity when **time, fuel, damage and avoidance opportunity** are considered jointly?
- Which exact simulator build, controller commits, scoring logs and run seeds generated the reported −10, −47, +3 and +44.5 results?

