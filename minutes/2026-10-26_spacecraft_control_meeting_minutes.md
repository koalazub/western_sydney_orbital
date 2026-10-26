# Meeting minutes: spacecraft control and collision avoidance

**Date:** Monday 26 October 2026  
**Status:** Draft from the supplied transcript  
**Participants:** Not identified in the transcript  
**Next meeting:** Proposed for Tuesday 27 October 2026, time to be confirmed

## Main outcome

The main recommendation was to favour collision-avoidance translations along a single axis of the shuttle's local reference frame, where a safe option exists. The intended benefit is lower fuel consumption by reducing simultaneous use of thrusters on different axes.

Controller demonstrations and a path-planning simulation were proposed for the next scrum.

## 1. Collision avoidance and fuel use

The discussion considered a shuttle with body-aligned thrusters and a local reference frame centred at its centre of mass.

The speaker recommended that the manoeuvre optimiser account for:

- Collision risk and clearance.
- Deviation from the direct route to the space station.
- Fuel use, including whether a manoeuvre requires thrust along multiple body axes.

A slightly longer axis-aligned manoeuvre was suggested as potentially preferable to a shorter diagonal manoeuvre. No objective weights or validated fuel comparison were established.

The current simulation was described as having no separate thruster ignition cost. Fuel consumption from firing multiple thrusters still needs to be considered.

## 2. Attitude control

A participant questioned whether changing the shuttle's attitude conflicted with the existing approach.

The speaker clarified two possibilities:

- Bias the initial orientation towards a useful avoidance direction, if the initial manoeuvre already permits an attitude change.
- Keep the existing attitude and favour body-axis translations when selecting avoidance manoeuvres.

Initial reorientation remained an option, not an agreed requirement. The aim was to avoid unnecessary attitude changes during later avoidance manoeuvres.

## 3. Controller discussion

The speaker reviewed proportional, integral and derivative control using velocity tracking as the example.

- **Proportional control** was described as scaling the difference between desired and current velocity into a control input. The speaker suggested it might be sufficient for the simple beeline case.
- **Integral control** was discussed as accumulating error over time to compensate for persistent discrepancies, with gravity used as an example. Accumulated error and thrust saturation were discussed as possible causes of overshoot.
- **Derivative control** was presented as responding to the rate of change of error to damp the response.

No controller gains or final controller configuration were selected. The derivative sign convention and worked examples in the transcript need checking before implementation.

## 4. Software structure

The speaker recommended brief, modular code with separate functions for:

- Control.
- Statistical filtering.
- Particle tracking.
- Path planning.

The specific statistical filter was unclear in the transcript. This was a suggested structure, not a confirmed interface design.

## 5. Reported simulation

The speaker reported running a simulation that predicted future asteroid positions and selected a safe corridor close to the direct route to the space station.

The speaker described the result as promising and said it exposed the fuel cost of manoeuvres using multiple axes. These were reported observations. The simulation was not demonstrated in this transcript, and the quoted scores were unclear.

## Actions and requests

Speaker roles below are inferred from the undiarised transcript. Names remain unconfirmed. The following day was proposed for the next scrum, not confirmed as a scheduled meeting.

| Action | Owner | Timing | Completion |
|---|---|---|---|
| Arrange the next scrum. | Main speaker. | Scheduling deadline not specified. Scrum proposed for Tuesday 27 October 2026. | Meeting details shared. |
| Prepare and demonstrate current controllers. | Controller contributors, requested by the speaker. Individual acceptance not recorded. | Next scrum, proposed for Tuesday 27 October 2026. | Demonstrations available for comparison and troubleshooting. |
| Demonstrate the asteroid-prediction and safe-corridor simulation. | Main speaker. | Next meeting, proposed for Tuesday 27 October 2026. | Simulation and approach presented. |

Action evidence is in paragraphs P77 to P81 of the supplied transcript, counted at paragraph separators.

## Open points

- Confirm whether initial attitude bias is useful and permitted by the existing planner.
- Establish how safety, route deviation and fuel use will be balanced.
- Validate the claimed fuel penalty for diagonal manoeuvres.
- Check the PID sign conventions and clarify the proposed statistical filter.
- Compare controller approaches before combining them.
