# Meeting minutes: qualifier MVP and team leadership

- Meeting date: Not established by the supplied transcript.
- Filing date: 4 November 2026. The filename uses this date.
- Presenter: Not reliably identified. Speaker labels in the transcript are inconsistent.
- Attendance: Not recorded.
- Qualifier: Code selection was due immediately. The team planned to start the submission form the following morning.
- Team league: Described as running over the next two weeks.

## Main outcomes

The meeting favoured a short, explainable controller for the qualifier. The example was a beeline with debris avoidance, simple velocity prediction and a final correction, at roughly 125 lines excluding logging. An alpha-beta filter was considered sufficient. An extended Kalman filter was judged unnecessary for the current model.

The group favourably discussed one participant's MVP as the qualifier base. It needs cleanup before submission. The transcript does not name the owner or record a formal vote.

The team lead will complete the qualifier form. Contributors need to pull the reorganised repository, restore their code to the matching profile folder, resolve conflicts with the agent and push.

## Qualifier submission

### Code choice

- The immediate need was an indication of whose code, or which code, would represent the team.
- Simplicity was the main criterion, even if it is not a scored metric. The speaker expected most teams to use Claude or another coding assistant. A first generated solution was hard to follow. A 125-line version was understandable, and the team did not need more than that.
- An extended Kalman filter was discussed. It linearises the model with a Taylor-series approximation and adds complexity. The team judged that the current measurement noise and dynamics are not strongly nonlinear or non-Gaussian, so the filter would not be statistically optimal here. Alpha-beta filtering reportedly gave the same or better results in their tests. These were the team's observations, not a general claim about EKFs.
- Even a simple beeline with avoidance could be enough. A controller could take radar measurements, estimate velocity, predict forward and check for a threat. It would not need full tracking or prediction.
- The 125-line figure excludes logging. Internal logs help replay an attempt but are not a submission requirement.
- A participant asked how much AI-assisted or "vibe" coding would be permitted. No rule was established. The discussion returned to explainability: the team still needs to understand and derive the submitted code.
- A tutorial notebook shared in Discord was reported as useful, particularly for state estimation. It explains each phase but does not include the full implementation.

### Submission logistics

- The following morning was reserved for opening the qualifier form and starting the submission. The team lead must fill it out. Recording would follow.
- The code must be accessible through Git. The repository is already organised by contributor profile, and the website reads from it so the team can compare controllers online.
- Contributors who pull the reorganised repository may need to re-add their code to the matching folder. Conflicts should be resolved with the agent rather than by hand, then pushed back.
- The transcript does not state the official qualifier deadline.

### Flight-control risks

- The simulator continues after the shuttle reaches the target. A controller that keeps commanding thrust will keep using fuel. The target time is timestamped.
- A brief opposite-thrust command was added to stop the shuttle after arrival. The final configuration still needs to be checked.
- A participant raised the risk of arriving too fast for the station's bounding box to register. A slow beeline reportedly still entered the 50 m target region.
- A beeline from two weeks earlier reportedly scored 119 when it hit something and 135 when it did not, at under 10 m/s. These were recollections from an earlier local run, not official results.

## Demonstration simulator

- A separate simulator was demonstrated with a first-person view, safe-corridor plotting, thrust vectors, adjustable debris count, threat highlighting, fuel use, score and predicted score.
- Threats inside the safe corridor were highlighted in yellow. The controller made small corrections and a final correction. A threat camera and chase camera were also available.
- The demo scored 164 with manoeuvres. Debris size and speed were then increased to stress the controller. The score is a local demo output, not an official result.
- An earlier attempt used a Gaussian-mixture probability-hypothesis-density filter for full nonlinear prediction. It took much longer to build. The current controller remains based on the simpler 125-line approach.
- A possible later step is to overlay instrument panels on the competition simulation. This was an idea, not a committed deliverable.

## Team leadership

- A speaker stepped back from the team-lead decision because they feel too close to the team to choose fairly.
- The team league runs over the next two weeks. It is part of a course and carries recognition. The speaker did not want anyone to feel rejected if not selected.
- Selection should weigh experience with sprints and finishing projects, not only coding ability. The team lead coordinates the final submission step and does not have to write the controller.
- A panel should make the decision rather than one person. An earlier appointment was made in about 15 minutes because the team had no other candidate. The speaker noted that choosing the busiest contributor can create capacity problems.
- More lead opportunities are planned. The student side will eventually have a student lead, and smaller sub-projects will offer leadership roles. The aim is a flat hierarchy: one student lead plus an academic committee, rather than a title for every small task. The speaker wanted to avoid a "feudal system" of titles.
- The speaker described the second-in-command role as offering about 80% of the benefit for about 20% of the work. Maintaining the team's current environment was a priority.

## Expected competition changes

- The team expected the organisers to change sensor noise or some debris or projectile details before the next stage.
- A larger change was considered unlikely. The next webinar was expected to be the last chance for the organisers to explain a change, with about a week left afterwards.
- The speaker was comfortable with a request to explain how the solution works. The simple controller was chosen partly so the team can derive and explain it from scratch.

## Actions and requests

Individual acceptance was not recorded. Timing is relative where the transcript does not establish the meeting date.

| Action | Owner | Timing |
| --- | --- | --- |
| Confirm the qualifier code and its owner. | Team and panel. | Immediately; final owner not recorded. |
| Open the qualifier form and begin the submission. | Team lead. | Following morning. |
| Record the qualifier submission. | Team. | After the form. |
| Finish an MVP. | Contributors. | That evening. |
| Pull the reorganised repository, restore code to the matching profile folder, resolve conflicts with the agent and push. | Contributors. | Before submission. |
| Clean up the selected MVP and review it with the team. | Selected MVP owner and reviewer. | After the meeting. |
| Decide the team lead through a panel. | Panel. | Before the team league. |
| Use the tutorial notebook shared in Discord. | Team. | Available now. |

## Open points

- The transcript does not clearly name the selected qualifier code or its owner.
- The team-lead panel and selection criteria are not defined.
- How much AI-assisted coding is permitted remains unresolved. The discussion emphasised that the code must be explainable.
- The thrust-stop safety for arrival needs final verification.
- The official qualifier deadline is not stated in the transcript.
- The instrument overlay for the competition simulation is optional and unplanned.
