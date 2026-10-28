# Meeting minutes: state estimation and target tracking

- Date: 20 October 2026.
- Format: Technical briefing and discussion.
- Participants: Not fully identified in the transcript.
- Next meeting: Not stated.

## Main outcomes

The briefing explained how state estimation turns sensor measurements into estimates of physical quantities, together with uncertainty. The discussion connected this process to spacecraft navigation, collision avoidance and tracking an unknown, changing number of objects.

The main guidance was to model both process and measurement uncertainty, use consistent reference frames, and adapt tracking methods to the timing and noise characteristics of the sensors. No specific estimator, tracker or implementation parameters were selected.

## Discussion

### State representation and reference frames

A state describes the properties needed to understand a system. Kinematic examples included position, velocity, acceleration, orientation and angular rates. These quantities can be collected into a state vector for calculation.

Every state needs a stated reference frame. World-frame coordinates describe a vehicle relative to its environment. Body and sensor frames describe quantities relative to the vehicle or instrument. Measurements and predictions must be transformed consistently before comparison or control.

### Measurements, disturbances and sensor models

Sensors provide measurements that need interpretation through a model. Examples included estimating travel from wheel rotation, positioning from satellite signals, and deriving angular positions from image pixels.

The speaker distinguished:

- Process uncertainty, including unmodelled motion, wheel slip, wind, actuator response and other disturbances.
- Measurement uncertainty, including sensor noise, bias, limited resolution, sparse sampling and missing information.

Dead reckoning can propagate position from velocity, but errors accumulate. Multiple sensors can provide complementary evidence. A participant asked about calibration over time. The speaker explained that an estimator can account for sensor-specific bias or change its weighting without necessarily changing the sensor's physical calibration.

### Uncertainty and covariance

A position estimate alone is insufficient for safety decisions. The estimator also needs to describe how uncertain that position is.

Covariance matrices and uncertainty ellipses illustrated different levels of uncertainty along different directions, and correlations between state variables. The discussion stressed that uncertainty generally grows during prediction without new measurements. It must be propagated alongside the state rather than carried forward as if confidence were unchanged.

Sensor specifications, repeated measurements, reference data and knowledge of the underlying dynamics were discussed as evidence for uncertainty models. A satellite-collision anecdote illustrated the consequences of overconfidence, but its historical causal claims were not verified in the transcript.

### Kalman filtering and the prediction-update cycle

An interactive position-estimation exercise showed why an estimator should not immediately follow every new measurement. Inconsistent readings may reflect noise, actual movement or an inadequate process model.

The walkthrough covered this cycle:

1. Start with the previous state estimate and its covariance.
2. Predict the next state using the motion model and known inputs, while propagating process uncertainty.
3. Map the prediction into measurement space and compare it with the observation. This difference is the innovation.
4. Use predicted and measurement uncertainty to calculate the Kalman gain.
5. Apply the correction and update the covariance.

The gain was introduced as the balance between trusting the prediction and trusting the measurements. The demonstration was qualitative, not a complete implementation specification.

Process noise covariance, Q, and measurement noise covariance, R, were discussed as tuning choices. Underestimating measurement noise can make the filter chase noisy readings. Overestimating it can cause useful measurements to be ignored. Underestimating process noise can make the filter overconfident in its motion model and slow to respond to disturbances.

### Multi-target tracking and identity

Tracking adds detection, association and track management to state estimation. The number of real targets may be unknown, and some measurements may be clutter or noise.

The lifecycle discussed was detection, tentative track initialisation, measurement-to-track association, confirmation, coasting through missed observations and eventual deletion.

Association was identified as a major computational challenge. Multiple measurements and tracks create competing assignments, and maintaining alternative hypotheses can become expensive. Identity is particularly difficult when targets cross, disappear, reappear or split.

The discussion contrasted point-target tracking with methods that also use appearance, target extent or additional sensors. Extra distinguishing information can help association, but adds modelling and computational cost. Reappearance near a predicted path does not by itself establish that a target has the same identity.

### Neuromorphic and event-based sensing

Event sensors were described as producing asynchronous pixel measurements with fine timing resolution rather than conventional, regularly spaced frames.

The discussion highlighted background noise, non-Gaussian measurement behaviour, intensity-dependent latency and pixel history as possible complications. Event timing can distort a target's apparent shape and affect centroid estimates. A single bright peak may also be a poor position estimate if it represents a hot pixel rather than the target centre.

Suggested approaches included clustering events into measurements, adapting noise models and update timing, and considering extended or unscented Kalman filters for nonlinear models or particle filters for non-Gaussian estimates. No method was chosen.

Track ageing needs particular care. A measurement for one target should not automatically count as a missed detection for every other target. Conversely, tracks without new events still need time-based management so they do not coast indefinitely.

### Image calibration and astronomical positioning

The speaker outlined a pipeline for converting detections into angular positions:

1. Detect image sources and estimate their centroids.
2. Match reference stars to a catalogue.
3. Estimate pointing, image rotation and pixel scale.
4. Transform target pixels into a celestial reference frame, such as right ascension and declination.
5. Propagate centroid and calibration uncertainty through the transformation.

Camera intrinsics, sensor placement, pointing and calibration all affect this conversion. Reference stars can provide evidence that differs from the telescope's reported pointing.

The closing discussion noted the size-range ambiguity in a single camera image. A small nearby rock and a large distant rock can look similar. Range measurements can add useful information, subject to their own reliability and uncertainty.

## Guidance for the spacecraft challenge

- Estimate both spacecraft motion and the states of nearby objects in consistent reference frames.
- Retain and propagate uncertainty when predicting possible collisions.
- Plan for an unknown, time-varying number of targets, including missed observations and ambiguous identities.
- Predict feasible spacecraft motion as well as obstacle motion when selecting avoidance manoeuvres.
- Use complementary sensor information where available, without assuming every sensor is reliable.
- Check whether conventional tracking assumptions fit the event stream before adopting an existing tracker unchanged.

## Follow-up and open points

No named action owners, deadlines or next meeting were recorded. Estimator selection, calibration parameters, noise models, association rules, track confirmation and deletion thresholds, and update schedules remain unspecified.
