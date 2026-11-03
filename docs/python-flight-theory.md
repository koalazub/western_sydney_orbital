# Python flight theory and evidence limits

The sweep runs the local physics engine. The PID and fixed-gain trackers discussed
in the October minutes are separate reference calculations. They do not explain
the published arrivals unless a policy actually calls them. No gains or objective
weights are selected here. This review covers the named implementations and their
assumptions. It does not claim that every point in the minutes is complete.

## What the scripts execute

```text
examples/flight_sweep.py -> POLICIES -> wsorb.sweep.run_sweep
  -> fresh HeadlessSimulation and policy factory for each trial
  -> configure_flight -> public observation -> policy -> Decision
  -> paused native run_step -> next observation -> repeat
```

[`run_sweep`](../src/wsorb/sweep.py) owns the experiment loop.
[`HeadlessSimulation`](../src/wsorb/flight_engine.py) uses the authenticated local
API and isolated Node headless adapter. That adapter loads the same built Rust/Bevy
WASM physics used by the browser, without a renderer. This is not a Python
replacement plant, and it does not run the licensed STP runtime.

The [historical HY script](../notebooks/20261028-MVP-HY/flight_sweep.py) maps
`basic_pd_radar` directly to `flight.control.basic_command`, plus a coast trial.
The [current example](../examples/flight_sweep.py) uses `policy_factory` for that
same numeric PD/radar law and exposes more profiles. Their default JSON files
still request `far_side_l2`, 600 s, 0.5 s commands and basic/coast policies. Merely
adding an official policy to `POLICIES` does not select it or change that preset.

`basic_command` requests position-error acceleration minus velocity damping, with
short-range repulsion from the first 24 public radar rows. It has no integral,
filtered derivative, target tracker or collision proof. Coast means zero thrust,
not zero velocity.

The official sweep factory creates fresh `ResilientGuidance`. On the
`competition_reference` preset it dispatches through `LocalMotionPlanner` to
[`flight.official_profile.official_command`](../src/wsorb/flight/official_profile.py).
That is a different policy from both basic PD and the pure PID reference.

## Coordinates and time

Local observations use SI units in the Phobos-centred rotating reference frame.
Position and velocity are reference-frame vectors; commands are body-frame m/s²
and rad/s². `reference_to_body` applies the declared quaternion convention.
Local active Hamilton `xyzw` maps body to reference, so reference components use
its rotation matrix transpose. Do not conjugate SDK values just because their
convention is called passive. The SDK adapter declares its own Phobos frame;
matching array shapes does not establish matching physical frames.

The scenario's physics step is 0.05 s. A sweep's `step_s` is a separate command
hold and decision interval, constrained to the physics grid. At time `t_n`, the
policy sees an observation and chooses the command for the following interval.
The saved row at `t_(n+1)` contains that command and its resulting observation;
the first row has no command. A held body command need not be constant reference
acceleration when the vehicle rotates. Gravity, rotating-frame terms and contacts
remain active.

Paused manual steps do not test asynchronous ownership, lease expiry or transport
latency. A policy can use a modeled lease internally, but that is not evidence
that a live command remained valid for the sweep's entire hold. Sensor timestamps
also differ from the decision interval. Use each genuine acquisition time, not
the number of Python polls. STP radar-loop and mission clock origins require an
independently established offset.

## PID velocity reference

[`pid.py`](../src/wsorb/flight/pid.py) implements, component by component:

```text
e_n = desired_velocity_n - measured_velocity_n
I_n = clip(I_(n-1) + e_n*dt, -integral_limit, integral_limit)
d_n = (e_n - e_(n-1))/dt
D_n = D_(n-1) + dt/(tau + dt) * (d_n - D_(n-1))
u_n = norm_cap(Kp*e_n + Ki*I_n + Kd*D_n)
```

The first derivative is zero. `e` has units m/s, `I` metres and `D` m/s².
Consequently `Kp` is 1/s, `Ki` is 1/s² and `Kd` is dimensionless. The integral
limit is metres per component, not an acceleration limit. Clamping limits stored
integral growth but does not feed output saturation back into the integral. It is
not back-calculation or conditional-integration anti-windup. Error differentiation
also responds to setpoint jumps; the low-pass filter reduces that response but
does not remove derivative kick.

All stored vectors must keep one coordinate basis across updates. This loop does
not rotate its memory as the body turns. Compute in a common reference frame and
convert the final command at the flight boundary. No current sweep or resilient
guidance path imports this PID class.

For a constant velocity target, ideal velocity integrator, no delay and no
saturation, proportional-only control gives:

```text
e_(n+1) = (1 - Kp*dt) * e_n
```

Thus `0 < Kp*dt < 2` gives asymptotic decay; values below one approach monotonically,
values above one alternate, and two is marginal. With applied thrust gain `g`,
replace `Kp*dt` by `g*Kp*dt`. This is a local linear result, not a stability proof
for integral/derivative feedback, rotating bodies, avoidance switches or the
position PD law. The latter has separate position gain in 1/s² and velocity gain
in 1/s. A norm cap bounds requested acceleration; it does not prove convergence.

`validate_pid_step` holds each bounded command on an ideal double integrator and
reports sampled rise, settling, overshoot and saturation. There is no gravity,
actuator delay or contact model. A reported final error is the error at the finite
study endpoint, not a proof of an asymptotic steady state.

## Alpha-beta and gamma reference

[`tracking_filter.py`](../src/wsorb/flight/tracking_filter.py) expects already
associated target positions in one frame. For measurement `z` after interval `dt`:

```text
CV:  p_minus = p + v*dt;                 v_minus = v
CA:  p_minus = p + v*dt + 0.5*a*dt²;     v_minus = v + a*dt
r = z - p_minus
p_new = p_minus + alpha*r
v_new = v_minus + beta*r/dt
CA: a_new = a + 2*gamma*r/dt²
```

All three gains are dimensionless. The factor two belongs to this gamma
convention; a source using `gamma*r/dt²` needs a different numeric gamma. With
gamma zero, acceleration stays frozen. Only zero initial acceleration reduces
the CA tracker to alpha-beta. Nonzero initial acceleration still affects every
prediction, even though no acceleration correction occurs.

Gain bounds in `[0, 1]` are not a general stability certificate. In scaled error
coordinates `[position, dt*velocity, dt²*acceleration]`, the fixed-step CA
transition is `F = [[1,1,0.5],[0,1,1],[0,0,1]]`, measurement row `H = [1,0,0]` and
correction column `K = [alpha,beta,2*gamma]`. Error evolves with `(I-KH)F`.
Spectral radius below one means asymptotic linear error decay for this fixed
model only. For example, `alpha=1, beta=0, gamma=1` has radius `sqrt(2)` despite
passing the component bounds. The CV case has
`A = [[1-alpha,1-alpha],[-beta,1-beta]]` and strict stability conditions
`0 < alpha < 2`, `0 < beta < 4-2*alpha`. Zero gains can leave unit modes. Exact
truth initialization can hide unstable error dynamics because it supplies no
initial error to amplify.

The reference trackers estimate no covariance, association confidence or
unmodeled acceleration bound. Neither is wired into `ResilientGuidance`.

## Filtering is not forecasting

[`control_validation.py`](../src/wsorb/flight/control_validation.py) evaluates
analytical truth `p0 + v0*t + 0.5*a*t²`, independently of estimator integration.
`validate_alpha_beta`, `validate_alpha_beta_gamma` and `compare_gamma_effect`
receive an exact position at every step. A 300 s run is 300 s of filtering, not a
300 s open-loop forecast. Position RMSE after measurement correction can look
excellent while the velocity or acceleration estimate remains poor.

Use `validate_alpha_beta_forecast` or `validate_alpha_beta_gamma_forecast` for a
holdout. Supply a `TrackerScenario` containing the gains, truth, initial estimate,
fixed step and conditioning duration, then pass `prediction_horizon_s` explicitly.
Measurements stop at `conditioning.horizon_s`. The API freezes that estimated
state and extrapolates CV or CA without further updates. Post-cutoff truth is
used only to score errors.

The result reports `cutoff_s` and `prediction_horizon_s`, plus separate
`conditioning` and `forecast` histories and metrics. Forecast times are offsets
from the cutoff. The origin is included for plotting but excluded from forecast
RMSE and maximum error. Each phase is positive, at most 300 s and on the same
fixed grid; combined work is at most 100,000 steps. RMSE is over Euclidean vector
errors, not per-component RMS. Truth-valued initial states are the scenario
defaults; set explicit initial errors when testing acquisition or convergence.

A constructed counterexample makes the distinction concrete. Let `p(t)=t²/2`,
initial velocity zero, alpha one and beta zero. Filtered positions match every
measurement, yet a CV forecast frozen at `t=1` stays at 0.5 m and misses truth at
`t=3` by 4 m. The associated tests cover that distinction and analytical CV/CA
holdouts. They do not measure real asteroid forecast accuracy.

Fitting is a third operation. Fit calibration and choose candidate gains on
training data; freeze them before evaluating a holdout. Choosing them from the
holdout errors is leakage even if the forecast loop receives no measurements.

Finite input components can still overflow during subtraction, integration or
gain application. PID and tracker updates now validate candidate states and
output before committing any state. Rejected updates raise `ValueError` and
leave the previous state usable. Gamma correction avoids forming an underflowed
`dt²` denominator. Validation uses scaled norms/RMSE and rejects unrepresentable
truth, errors and metrics instead of returning misleading infinities.

## Calibration, association and the actual radar limits

[`calibration.py`](../src/wsorb/flight/calibration.py) fits independent affine
channels, `measured = scale*reference + bias + noise`, and computes regularized
covariances. Labels must match, but labels alone do not transform frames, align
clocks or identify sensor physics. Its `N-1` covariance is a sample covariance of
in-sample fitted residuals, not an unbiased sensor-noise estimate after fitting
two parameters or an independent predictive covariance. Check calibration on
withheld paired measurements.

`estimate_process_noise` uses residuals `x_(k+1) - F_k*x_k`. The caller supplies
`F_k` for the actual intervals. Its discrete covariance includes model mismatch
and any noise in the supplied states; it is not automatically a continuous noise
density. Inspect the reported mean residual instead of hiding model bias in a
zero-mean noise assumption. Eigenvalue floors make matrix operations possible,
not physically calibrated. These calculations do not replace the flight estimator.

[`association.py`](../src/wsorb/flight/association.py) is bounded greedy
distance-gated assignment with confirmation, coasting and deletion. It retains
one hypothesis; ambiguous matches still count toward confirmation. Confirmation
does not prove identity at a crossing or after reacquisition, and cannot certify
clearance. Its distance matrix costs `O(N*M)`; sorting `P` gated candidate pairs
adds `O(P*log(P))` work. The hard pair cap bounds both, but vectorization does not
remove sorting cost. `update(time_s, measurements)` gives the entire scan one
timestamp. Rolling STP radar rows have different acquisition times and cannot be
treated as one simultaneous scan without an explicit timing/motion adaptation.

Actual radar rows are anonymous, sorted observations with their own acquisition
times, a relative vector, closest range, scalar range rate and diameter. A row
index is not an object ID, and range rate does not supply full velocity. Feed an
AB tracker an aligned target position only after resolving observer motion,
frame, time and association. The current resilient path instead anchors ownship
to direct navigation and uses anonymous radial motion envelopes with explicit
allowances. Those allowances do not bound Gaussian tails or all gravitational
and contact-driven motion. The official scan observes 200 of 500 bodies.

## Limits of the other reference helpers

These are reviewed constraints, not changes to flight behavior. None of these
helpers is integrated by this review.

- [`axis_avoidance.rank_maneuvers`](../src/wsorb/flight/axis_avoidance.py) trusts
  caller-supplied clearance. Its threshold does not certify a swept path or check
  unseen obstacles. When single-axis preference applies, it keeps candidates
  with **at most** one active axis, including coast, and discards multi-axis
  candidates before scoring. That is a hard eligibility preference, not a small
  reward for an active single-axis burn.
- [`maneuver_objective`](../src/wsorb/flight/maneuver_objective.py) scores metres
  of clearance/progress against an L1 acceleration sum in m/s². Weights must carry
  the corresponding units or follow declared normalization; equal numeric
  weights do not mean equal physical importance. Both candidate types require
  nonnegative clearance and progress, so neither can express signed penetration
  or progress regression. The objective scorer itself does not reject unsafe
  maneuvers. `FloorPenaltyProxy` penalizes low supplied clearance, not measured
  height above a proven physical floor or an established official floor rule.
- The L1 quantity in both modules is effort for one acceleration command, not kg
  of fuel. Accumulating `sum(abs(a))*duration` gives a velocity-like effort in
  m/s. Propellant comparisons also require mass, thruster allocation, specific
  impulse and the applicable force/torque model. The 41.4% diagonal increase is
  ideal equal-resultant geometry, not a measured consumption penalty.
- [`event_camera.detect_events`](../src/wsorb/flight/event_camera.py) thresholds
  `log1p(current) - log1p(previous)`. Changing luminance normalization changes the
  contrast because the added one fixes an intensity scale. The function emits
  at most one event per pixel per frame pair, assigns every event the newer
  frame's timestamp and keeps a row-major prefix when capped. It does not model
  asynchronous threshold-crossing times or repeated events. Its greedy clusters
  and distortion-free pinhole helpers do not establish object identity, camera
  calibration accuracy, hardware performance or a working flight vision path.
- [`attitude_bias.bounded_angular_request`](../src/wsorb/flight/attitude_bias.py)
  builds an ideal rest-to-rest triangular or trapezoidal slew, assuming zero
  initial spin. The request has no measured angular-velocity input. A geometric
  shortest arc and bounded peak values do not supply feedback, inertia/torque
  allocation or a phase-tracked braking command. Candidate translation clearance
  remains the caller's responsibility.

## The two official-profile paths differ

The local `flight.official_profile` uses a forward/lateral potential, observed-row
uncertainty growth, a current-lease swept-motion gate and alternative commands.
Its 10 m/s reference decreases near the arrival region using the existing local
active-braking assumption. The gate does not certify a future stop or unseen
space.

[`stp_official.py`](../src/wsorb/stp_official.py), used by
[`examples/ali/stp_official_profile.py`](../examples/ali/stp_official_profile.py),
is a separate source port. It combines capped beeline acceleration with radial
repulsion based on closest range, margin and closing-rate lookahead. It has no
local swept candidate gate or arrival-distance braking envelope. Its configured
attitude response time is not a native lease. Sequential SDK reads/writes and a
post-compute deadline check cannot guarantee cleanup after process death. Shared
10 m/s speed references and acceleration caps do not make these algorithms
identical. The source port has no licensed-runtime evidence. Actuator behavior
and clock alignment still need external validation in that runtime.

## Useful experiments, and what 6/6 does not establish

Keep the existing flight policy, caps, gates and recorded trials as controls.
Research comparisons worth making next are:

- Sweep caller-supplied PID gains and decision intervals on the ideal plant,
  then model delay, disturbance and noisy velocity. Report saturation duration
  and recovery after reversal as well as rise time. Integrating a candidate into
  flight is separate work, not an automatic consequence of a reference result.
- Compare CV and CA on fixed training/holdout splits with initial errors,
  measurement noise, missing detections, association failures and changes in
  acceleration. Report error versus actual prediction horizon, including worst
  cases. Do not score only exact, truth-initialized CA cases.
- Check affine calibration, residual bias and discrete process covariance on
  held-out data and different sample intervals before assigning uncertainty
  probabilities.
- Compare single-axis and diagonal candidates only after the same independent
  clearance check, at matched durations and resultant requirements. Separate
  coast from active burns and inspect the Pareto frontier before choosing
  dimensioned objective weights. Use an actual allocation/fuel model to test
  whether L1 effort predicts propellant use.
- Keep event sensing and attitude-bias experiments offline until their missing
  assumptions are addressed. Vary luminance scale, frame cadence and event caps;
  compare planned slews with measured nonzero spin and closed-loop attitude
  dynamics. These are proposed studies, not completed flight capabilities.
- Run matched local seeds and repetitions with unchanged debris populations.
  Keep arrival, damage, contacts, entry speed, local fuel, elapsed time and
  eligibility/latency failures separate. Selecting a combined objective or final
  weights needs an explicit decision and data.

The [local initializer](../simulation/src/physics.rs) seeds its xorshift generator
with `seed.max(1)` and passes it to
[`official_cloud::spawn`](../simulation/src/physics/official_cloud.rs). The
requested seeds 100, 101 and 102 therefore feed separate local initializations;
they are not forced to 100. Repeating a seed reuses its deterministic population.
The [official script](https://github.com/SimDynamX/STU4-Sim/blob/c010564824c48488104e648781d5e987b402e47a/Systems/SpawnDebris.py)
hardcodes 100 for its Python and NumPy generators. Different generators, draw
order and local geometry mean that local seed 100 does not establish an identical
STP population.

The published [official comparison](official-comparison.md) records six local
arrivals for Ali's profile across three seeds and two attempts each. Two attempts
recorded damage. This does not establish collision-free flight, an independent
six-draw reliability estimate, unseen-body clearance, live lease performance,
low-speed docking, PID/ABG benefit, calibrated forecast uncertainty or licensed
STP success. Three matched local seeds with deterministic repeats remain a small
practice sample. Local fuel and damage counters cannot produce comparable STP
points. The publication supports those specific local outcomes, not a general
safety or optimality claim. Changes to reference helpers do not revise or improve
these historical flights.
