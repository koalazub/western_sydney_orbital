# Meeting minutes: asteroid filtering and qualifier preparation

- Date: 29 October 2026.
- Presenter: Nick.
- Attendance: Full list not recorded in the transcript.
- Next scrum: Tuesday morning, 3 November 2026. Exact time to be confirmed.
- Qualifier deadline discussed: Wednesday, 4 November 2026, subject to confirmation against the official schedule.

## Main outcomes

Nick proposed a fixed-gain alpha-beta or alpha-beta-gamma filter as a simpler option for estimating asteroid motion in the simulation. The working assumption was that the shuttle's own state is already known, so this filtering would apply to the asteroids.

The qualifier submission should prioritise a short, readable implementation that contributors can explain, even if it is not the highest-scoring approach. The final scheduling decision was to hold the next scrum on Tuesday morning, replacing the earlier Monday suggestion.

## Discussion

### Why estimate asteroid motion?

Estimating position and velocity allows the team to predict where an asteroid will be and compare that prediction with the shuttle's future path. A possible collision requires the objects to reach the same region at the same time, not merely follow intersecting paths.

The speaker considered fixed-gain filtering suitable because the simulated asteroid dynamics and sensor noise were expected to remain reasonably consistent. This was a proposed simplification, not a validated performance result.

### Fixed-gain filtering versus Kalman filtering

The discussion revisited the balance between trusting a motion prediction and trusting a new measurement. A Kalman filter calculates its gain from uncertainty models. An alpha-beta filter uses fixed gains to correct position and velocity from the measurement residual. A gamma term can extend the model to acceleration.

The proposed filter is simpler to implement and does not itself estimate covariance. Hand tuning was recommended for this application. Deriving gains from the system dynamics was mentioned but not developed.

### Alpha-beta prediction and update

The walkthrough used a constant-velocity model and position measurements. In the notation below, `dt` is elapsed time between observations and `r` is the innovation, or measurement residual.

```text
dt = t_current - t_previous
x_pred = x_previous + v_previous * dt
v_pred = v_previous
r = measured_position - x_pred
x_updated = x_pred + alpha * r
v_updated = v_pred + beta * r / dt
```

The corrected position and velocity become the starting state for the next prediction.

In the one-dimensional example, the previous position was 3, the initial velocity was zero, and the timestep was one second. A new position measurement of 4 gave a residual of 1. With alpha and beta both set to 0.5, the updated position was 3.5 and the updated velocity was 0.5 units per second. The next predicted position was therefore 4.

The speaker explained that the same operations can apply to three-dimensional position and velocity vectors. Shared scalar gains across the axes were suggested for this simulation rather than separate tuning for each direction.

### Gain selection and acceleration

Alpha and beta do not need to match. In the update equation, a larger alpha moves the position estimate further towards the new measurement. A smaller beta makes velocity corrections more gradual. The examples illustrated the trade-off between smoothing noisy measurements and responding to changes in motion.

The gamma extension was introduced to estimate acceleration. Its spoken derivation and numerical example contained inconsistencies, so they are not recorded here as a verified implementation formula. No final gains, timestep or acceleration model were agreed.

The speaker also referred to a previous example predicting asteroid positions roughly 300 seconds ahead. No forecast-error measurements or validated prediction horizon were established in this meeting.

### Qualifier implementation

Tom's existing work was mentioned as a possible basis for the qualifier. No final entry was selected.

The recommended structure was to keep the actual contribution in a separate file, exposed through a function or object called by the controller or runner. Inputs and outputs should be clear. Supporting modules can hide implementation detail provided the main process remains understandable and contributors can explain their own work.

The stated preference was for as few lines as practical and code that can be read and understood directly, rather than a less explainable implementation chosen only for performance.

### Scheduling, exams and support

No scrum was planned for Friday, 30 October. Monday morning was initially proposed for the next meeting, but exam conflicts led to a final decision of Tuesday morning, 3 November.

Nick asked for contributors to demonstrate what they have where possible, while making clear that coursework and exams take priority. Contributions should fit around available capacity.

For Tuesday, Nick proposed breaking down the solution into a minimal flowchart and working through an MVP design with the group. He said he could explain the design rather than supply the competition code directly.

An interactive web demonstration with a noisy signal and adjustable alpha, beta and gamma controls was discussed as a possible teaching aid. It was an idea, not a committed deliverable.

## Actions and requests

| Action | Owner | Timing |
| --- | --- | --- |
| Schedule the next scrum and share the time. | Nick. | Tuesday morning, 3 November 2026. |
| Walk through a minimal solution flowchart and MVP design. | Nick. | Tuesday scrum. |
| Prepare readable, modular contributions and demonstrate current progress where possible. | Contributors, subject to exam commitments. | Next scrum. |
| Prepare and submit the qualifier entry. | Submission owner not assigned in the transcript. | Working deadline of Wednesday, 4 November 2026; official date requires confirmation. |

## Open points and transcript clarifications

- Confirm the official qualifier deadline. The discussion briefly questioned 4 versus 5 November before proceeding on the basis of Wednesday the 4th.
- Select the qualifier implementation and identify who will submit it.
- Confirm filter gains, measurement timing and whether acceleration estimation is needed.
- Check the gamma update before implementation. The worked acceleration arithmetic was inconsistent.
- The later verbal description of alpha conflicted with the earlier update equation. The equation above records the consistent position-correction example, not that later description.
