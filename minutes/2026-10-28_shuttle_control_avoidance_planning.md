# Meeting minutes: shuttle control and avoidance planning

- Meeting date: Not stated in the supplied transcript.
- Filing date: 28 October 2026. The filename uses this date.
- Participants: Not fully identified in the transcript.
- Next meeting: Proposed for the following day, with the time unconfirmed.
- Related record: [Supplied minutes dated 26 October 2026](2026-10-26_spacecraft_control_meeting_minutes.md), which cover the same discussion and are retained separately.

## Main recommendations

- Favour collision-avoidance translations along a single axis of the shuttle's local frame when a suitable safe option exists.
- Account for collision clearance, progress towards the space station and fuel use when comparing manoeuvres.
- Avoid unnecessary attitude changes during avoidance. An initial orientation bias remains an option, not a requirement.
- Demonstrate current controllers at the next scrum, compare approaches and troubleshoot before combining them.

## Discussion

### Fuel-aware collision avoidance

The discussion modelled the shuttle as a rectangular body with thrusters aligned to its local axes and a reference frame centred at its centre of mass.

The speaker explained that diagonal translations require thrust on multiple body axes. The simulator was described as having no separate ignition cost, but each active thruster still consumes fuel. The planner should therefore consider body-axis alignment rather than choosing an avoidance direction solely for the shortest clearance distance or route to the station.

A slightly longer single-axis burn was suggested as potentially cheaper than a shorter diagonal burn. No objective weights or validated comparison were agreed. The speaker cited roughly 41% extra fuel for a diagonal example, subject to the technical checks below.

### Initial attitude and later manoeuvres

A participant questioned whether initial reorientation conflicted with the existing approach. The speaker clarified that an orientation bias could be considered if the initial burn already involves changing attitude. One suggestion was to align a useful sideways avoidance direction with the mean motion of the asteroids.

If the beeline launch retains the starting attitude, the planner can instead favour translations along the existing body axes. The discussion did not establish a requirement to rotate the shuttle during avoidance.

### PID velocity control

The controller walkthrough defined velocity error as desired velocity minus current velocity. Tuned gains convert that error into a control input, discussed as acceleration.

- Proportional control responds to the current error. The speaker suggested that it may be sufficient for a simple beeline manoeuvre.
- Integral control accumulates error over time and can compensate for persistent disturbances. Gravity was the example. Accumulated error and thrust saturation were discussed in relation to overshoot.
- Derivative control responds to changes in error and was presented as a way to damp the response.

No gains or final controller configuration were selected. The walkthrough was conceptual, and its derivative sign convention and numerical examples need checking before implementation.

### Code structure and reported simulation

The speaker recommended brief, modular code, with separate functions for control, statistical filtering, particle tracking and path planning. The specific statistical filter was unclear in the transcript.

The speaker also reported a simulation that predicted future asteroid positions and selected a safe corridor close to the straight-line route to the station. The results were described as promising, and the simulation reportedly exposed the fuel penalty of multi-axis manoeuvres. A demonstration was proposed for the next meeting. The quoted scores were unclear and are not recorded as confirmed results.

## Actions and requests

Speaker identities and individual acceptance of requests were not recorded. Timing below remains relative because the transcript does not establish the meeting date.

| Action | Owner | Timing |
| --- | --- | --- |
| Arrange the next scrum. | Main speaker or organiser, name unconfirmed. | Meeting proposed for the following day; time unconfirmed. |
| Prepare and demonstrate current controllers. | Controller contributors. | Next scrum. |
| Demonstrate the asteroid-prediction and safe-corridor simulation. | Main speaker. | Next meeting. |
| Compare controllers, troubleshoot differences and discuss combining approaches. | Team. | During the demonstrations. |

## Open points and technical checks

- Decide how the optimiser balances safety, station-route deviation and fuel use.
- Establish whether an initial attitude bias provides enough benefit to justify any extra control effort.
- Validate the diagonal fuel comparison against the actual thruster model and manoeuvre. The cited 41% figure is not a general result for every diagonal path.
- Check the PID derivative sign convention and numerical examples before implementation.
- Confirm the statistical filter and tune the controller gains.
- Clarify the floor-related penalty mentioned briefly in the discussion. No formula or parameters were provided.
