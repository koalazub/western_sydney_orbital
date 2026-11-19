# Space Teams — Competition Readiness and Final Submission

**Date:** 19 November 2026  
**Attendees:** Nic Ralph, Tom Kirkpatrick, Ali El Ali, Bui Le, Naqib  
**Purpose:** Review controller performance, settle the submission architecture, establish a baseline, and coordinate the final competition submission.

## 1. Controller Progress

Tom reported fixing bugs and tuning the frustum angle and thrust behaviour. He also introduced an acceleration/speed-up option for situations where the planner finds no safe trajectory.

The team reported collisions in fewer than approximately 5% of runs, although the sample size and exact success rate were not established. A score around 47 was discussed as difficult to surpass.

An unresolved question remains about whether target-seeking commands can override avoidance commands.

## 2. Collision Edge Cases

Remaining failures appear to involve irregular, pointed, tumbling asteroids. Although the predicted asteroid centre may clear the shuttle, the asteroid's rotating extremities can still collide with it.

Nic suggested increasing the effective asteroid size used for clearance calculations by a couple of metres.

Ali recommended using the faster alternative simulator, with randomized asteroid configurations, to reproduce and isolate these edge cases before testing in the official simulator.

## 3. Submission and Recording Protocol

The team discussed the competition's unlimited-submission allowance and the risk that only the latest submission is counted.

The agreed approach is:

- Only Naqib makes official competition submissions.
- Contributors record two consecutive runs, including after controller changes.
- Recordings should preferably be uploaded to YouTube, accompanied by the score and key performance metrics.
- Recording links should be documented in the main repository README for easy review.
- The exact Python controller file must accompany any submission candidate, with required logs retained.
- Commentary on the recordings is optional.

## 4. Baseline and Reporting

Nic requested a stable baseline demonstrating the controller's approximate success probability, known failure cases, average completion time, fuel consumption and score.

A pinned Discord summary was proposed to provide consistent figures between meetings.

The team agreed to freeze a working MVP, record successful runs, and prepare a submission for 20 November unless a better verified candidate becomes available.

## 5. Controller Architecture and Ownership

The group clarified that Tom's frustum-based controller is the agreed competition submission baseline.

Ali's model-predictive controller remains an experimental alternative and was not intended to compete with or replace the agreed baseline without evidence of substantially better performance.

Harry and Kieran reportedly ran another version. A verified score of at least 45 would make it worth considering as a submission candidate.

Nic emphasised collective code ownership: contributions should be evaluated on effectiveness rather than individual attribution. The same principle is expected to apply to future CubeSat development.

## 6. Decisions

1. **Submission baseline:** Use Tom's controller architecture.
2. **Submission authority:** Naqib is the sole official submission coordinator.
3. **Recording:** Capture two consecutive runs and share YouTube links and performance metrics.
4. **Baseline freeze:** Prepare the current working MVP for submission on 20 November unless surpassed.
5. **Reporting:** Establish consistent performance statistics and known edge cases.
6. **Final coordination:** Review submissions and updates through Discord rather than scheduling another formal scrum.

## 7. Action Items

| Owner | Action |
|---|---|
| Naqib | Review candidate videos, verify associated controller files, coordinate official submissions |
| Tom | Finalise and share the current frustum-based controller baseline |
| Bui | Record runs and track the corresponding scenario/comet and controller version |
| Ali | Follow up with Harry and Kieran about running their controller; test promising versions on the server |
| Nic | Review MVP code and run videos when provided with exact links; follow up on travel |
| All | Capture candidate runs, report scores and key metrics, and prepare the baseline submission |

## 8. Open Issues

- Is target-seeking ever overriding obstacle avoidance?
- Will a larger asteroid safety margin eliminate the remaining collisions without significantly harming the score?
- What is the reproducible collision rate, based on a known number of runs?
- Can the Harry/Kieran implementation achieve a verified score of 45 or higher?
- Have the recording, controller source and any mandatory logs been matched to the same final candidate?

**Next milestone:** 20 November 2026 — official competition submission.

**Follow-up:** Discord coordination and review of candidate recordings. No further formal scrum scheduled.
