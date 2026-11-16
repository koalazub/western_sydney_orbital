# Space Teams — Daily Scrum: MPPI, Benchmarking and Integration
## Date: Wednesday, 11 November 2026
## Meeting: Space Teams competition development scrum
## Status: Draft minutes reconstructed from automated transcription; speaker identities and any numerical claims should be confirmed before publication.
### Summary
The team agreed to continue daily scrums, except Fridays, during the final approximately 1.5 weeks of Space Teams. An experimental Model Predictive Path Integral (MPPI) navigation controller has been pushed to GitHub but not yet tested in the official simulator. The team discussed the updated fuel/speed trade-off, proposed an offline giant-asteroid test, and agreed to work in small branches with a review/merge queue before transferring proven changes into Tom's competition submission.
Scrum cadence and communication
- Continue daily scrums except Fridays for the remainder of the competition; return to a weekly meeting cadence afterwards.
- Members noted that Wednesday mornings can be difficult. The group discussed 10:30 a.m. and 11:00 a.m.; 10:30 a.m. appeared to be retained, but confirm against the calendar invitation.
- Use Discord for asynchronous progress and coordination rather than relying on everyone being present at every discussion.
- A post-competition break, possibly around two weeks, was floated; duration not decided.
### MPPI experimental controller
- A participant described having pushed an approximately 240-line MPPI implementation to GitHub, with no completed official simulator run yet.
- Intended purpose: rank sampled future trajectories, potentially accounting for spacecraft rotation and manoeuvre costs; it is a longer-horizon planning candidate, not a replacement for the high-priority emergency avoidance check.
- The current experiment reportedly considers approximately 40 selected nearby asteroids out of as many as 500 and bounds parts of the route search to around 30. The precise number of evaluated futures and computational complexity should be established from the code, not inferred from the discussion.
- Plan: resolve test-server access, run the implementation, examine navigation behaviour and performance, and review the code together. Poor initial results need not invalidate the approach, but changes should be benchmarked before integration.
Fuel, speed and avoidance experiments
- Updated simulator dynamics and fuel costs continue to affect earlier controller assumptions.
- A test was described where accelerating from rest to approximately 7 m/s used around 43 kg of fuel, while 75 kg was cited as the nominal fuel reference. These are reported figures, not independently reproduced measurements.
- Coasting at around 7 m/s reportedly led to roughly 6.5 m/s near the destination because of the changing dynamics; subsequent corrections were identified as costly.
- Proposed analysis: sweep target/coast speeds and plot completion time against fuel consumed and resulting score. An optimum was hypothesised, not demonstrated in this meeting.
- Proposed offline stress test: place a large stationary asteroid about 100 m ahead and evaluate sustained avoidance, path recovery and fuel cost. This is a diagnostic experiment rather than a competition scenario.
- The group also joked about fitting code into a double-column A3 layout (approximately 158 lines). This was not established as a formal competition requirement; clarity and explainability remain the practical constraints.
### Code integration and submission workflow
- Keep experiments on small, isolated branches, so one subsystem change can be compared against a known baseline.
- Use the team's shared experimental repository to collect attempts. Review and queue promising changes before merging; this was an agreed workflow concept, not confirmation that GitHub's automated merge-queue feature has been configured.
- Once a variant is tested and selected, Tom's submission repository/controller remains the path to the final competition code.
- Discuss controller changes alongside simulation logs and, ideally, recordings, to connect observed behaviour to the exact code revision.
- For the next scrum (Thursday, 12 November), members were asked to share runs, logs and recordings through Discord for a concrete comparison.
### Research/publication discussion
- A possible short paper or technical account of the competition methods and experimental trade-offs was discussed.
- The unusually favourable radar model was identified as a limitation to generalisability; any publication would need to state this limitation and support claims with reproducible tests.
- No venue, manuscript scope or submission decision was finalised.
### Action items
| Owner | Action Timing | Status |
| :--- | :---: | ---: |
| Ali | Provide exact GitHub revision, test prerequisites and a brief code walk-through | After initial trial |
| Naqib | server-access contact	Assist with access to the Windows/Tailscale test environment and facilitate an official simulator smoke test | As soon as practical |
| All contributors | Post controller revision, official run logs and recordings to Discord | Before the next scrum |
| Team integration reviewers | Compare small subsystem changes, review candidate branches and queue proven changes for integration | Daily through competition end |
| Tom | Incorporate the selected tested controller into the competition submission base | After results review |
| Nic | Share the giant-asteroid test harness/controller and its conditions	| When ready |


### Open questions
1. What exact MPPI code revision, trajectory count and runtime were used?
2. Can the current MPPI experiment run within official controller timing and memory constraints?
3. What speed profile minimises expected score penalty across varied simulator scenarios, rather than one seed?
4. Who owns approval of each merge and which specific repository/branch is the integration source of truth?
5. Does the 10:30 a.m. scrum time match the current Teams calendar invitation?
