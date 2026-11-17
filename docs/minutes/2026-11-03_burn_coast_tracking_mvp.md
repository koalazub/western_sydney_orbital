# Meeting minutes: burn-and-coast controller and tracking MVP

- Meeting date: Not stated in the supplied transcript.
- Filing date: 3 November 2026. The filename uses this date.
- Presenter: Nick, inferred from the closing thanks.
- Attendance: Full list not recorded in the transcript.
- Next scrum: Proposed for the following day during the Western Sydney Orbital meeting. A separate invitation will be sent.

## Main recommendations

Build the simplest working controller first. Burn towards the station, coast, avoid threatening debris and correct the route afterwards. Target tracking and alpha-beta filtering are needed to handle noisy measurements. Gravity compensation and principal-axis optimisation can be deferred for the MVP.

The next scrum should review working MVPs. The team needs a clean repository submission and a short video showing and explaining the solution.

## Discussion

### Phobos gravity and the flight profile

The presenter described Phobos gravity as small enough to approximate away over the short simulation. Including it would make the model more complete, but firing thrusters to compensate for a small speed loss may reduce the score more through fuel use than it recovers through speed.

The example flight profile used an initial burn to a reported target speed of about 16.7, followed by coasting. Units were not stated in the transcript. The example burn acted along the shuttle's local Z axis. These were walkthrough values, not a final controller configuration agreed by the team.

### Reference frames and the station aim point

The walkthrough distinguished the Phobos, radar and shuttle body frames. Radar positions must be interpreted in their measurement frame. Express the shuttle and target positions in a common frame before subtracting them, and transform manoeuvre directions into the body frame as needed.

Aim near the station rather than directly at its centre. The presenter recommended a 10 m offset because a direct hit might register damage in the logs even if the simulation ends at the arrival point.

### Fuel use and principal-axis manoeuvres

Diagonal translations can require multiple body-axis thrusters and consume more fuel than a suitable single-axis manoeuvre. The planner could compare the shortest avoidance displacement with alternatives that use fewer thruster axes.

That optimisation adds decision logic and code. The recommendation was to defer it if the MVP makes few manoeuvres. No general fuel-penalty percentage or optimisation rule was established.

### Target association and filtering

The example used a global nearest-neighbour tracker to associate new radar measurements with existing tracks. Matched tracks receive updates. Unmatched tracks coast using their predicted motion, then expire if they go too long without measurements.

An alpha-beta filter estimates position and velocity without following every noisy measurement directly. The presenter did not use an acceleration term because changes in acceleration were considered too small to justify it in the example. Alpha and beta control how strongly new measurements correct the estimates; no final gains were specified.

Tracking and filtering were identified as necessary for the MVP. Poor estimates can force unnecessarily wide avoidance manoeuvres, increasing the distance travelled.

### Threat monitoring and avoidance

Threat assessment should use relative position and velocity to predict how close debris will pass, rather than relying only on its current distance. Clearance should account for the debris boundary, not just its centre.

The proposed avoidance approach was to find enough displacement to clear the threat with as little velocity change as practical, then correct towards the station. The clearance formula relied on diagrams and notation not available in the transcript, so no implementation equation is recorded here.

Track confidence and freshness are separate considerations:

- A track needs enough measurement history before its estimated motion is trusted.
- A track that has gone too long without an update becomes stale, even if it was previously well established.

The example trust age was about 10 seconds. With radar updates described as roughly 1.6 seconds apart, that represents around six or seven updates. Waiting that long could miss a threat arriving early in the simulation. Higher sensor noise may require more history, so the trust age and stale-track limits need tuning. No final thresholds were agreed.

### Implementation and demonstration

The presenter reported an example controller of about 125 lines excluding logging, to illustrate that a short solution was possible. This was not a line-count requirement.

The walkthrough outputs were not displaying correctly on the presenter's machine. The presenter intended to share the material so contributors could run it locally and inspect the outputs. The team was asked to attempt its own solution rather than wait for a complete code submission.

## Actions and requests

Individual acceptance of the requests was not recorded. Timing remains relative because the transcript does not establish the meeting date.

| Action | Owner | Timing |
| --- | --- | --- |
| Share the walkthrough material for contributors to run locally. | Presenter. | Not specified. |
| Build and demonstrate a minimal burn, coast, avoid and route-correction controller with tracking and alpha-beta filtering. | Contributors. | Next scrum. |
| Send a separate scrum invitation overlapping the Western Sydney Orbital meeting. | Presenter or organiser. | Following day, exact time unconfirmed. |
| Prepare a clean repository submission and a short demonstration video. | Team, individual owners not assigned. | Submission timing not stated. |
| Send a private message if interested in team leadership. | Interested members, to the presenter. | Not specified. |

## Open points

- Confirm the radar reference frame and required coordinate transforms.
- Confirm the clearance geometry and avoidance thresholds from the shared walkthrough.
- Tune filter gains, trust age and stale-track expiry, including early-arriving threats and changes in sensor noise.
- Confirm the next scrum time and assign submission and video owners.
