"""Deterministic synthetic validation for the reference control and filter modules.

This module measures the pure ``PIDVelocityController`` and fixed-gain
``AlphaBetaTracker`` / ``AlphaBetaGammaTracker`` against synthetic plants. It is
not a controller, a tuned gain set, a debris-truth study or a real asteroid
forecast. Every gain is supplied by the caller and no value here is presented as
a competition recommendation.

Limits that apply to every result:

- The PID plant is an ideal three-axis double integrator. There are no actuator
  dynamics, delays, gravity, drag or disturbance terms.
- Tracker truth is the analytical constant-acceleration trajectory. Filtering
  receives exact positions at every step; the separate forecast APIs condition
  only through their cutoff and then accept no measurements. Neither study
  includes noise, association ambiguity or manoeuvres.
- The fixed step is explicit: ``horizon_s`` must be an integer multiple of
  ``dt_s`` within floating-point tolerance, and an excessive step count is
  rejected rather than silently shortened.
- Each conditioning or prediction horizon is capped at 300 s, and combined
  forecast work at 100,000 steps. These are synthetic study bounds, not a
  validated forecast horizon for a real asteroid.
- Nothing in this module uses engine state, the SDK, file I/O, threads,
  randomness or wall-clock timing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np
import numpy.typing as npt

from ._validation import Vec3, scalar, vector
from .pid import PIDConfig, PIDVelocityController
from .tracking_filter import AlphaBetaGains, AlphaBetaGammaTracker, AlphaBetaTracker

MAX_HORIZON_S = 300.0
MAX_SAMPLE_COUNT = 100_000
_FIXED_STEP_TOLERANCE = 1.0e-9
_SATURATION_TOLERANCE = 1.0e-9


def _finite_vec3(value: npt.ArrayLike, name: str) -> Vec3:
    array = vector(value, name)
    return float(array[0]), float(array[1]), float(array[2])


def _require_finite(*arrays: npt.NDArray[np.float64], name: str) -> None:
    if not all(np.isfinite(array).all() for array in arrays):
        raise ValueError(f"{name} is not representable")


def _row_norms(array: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Euclidean norms without overflowing intermediate component squares."""
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        norms = np.hypot.reduce(array, axis=1)
    _require_finite(array, norms, name="synthetic vector norm")
    return norms


def _root_mean_square(norms: npt.NDArray[np.float64]) -> float:
    scale = float(np.max(norms))
    if scale == 0.0:
        return 0.0
    with np.errstate(under="ignore"):
        return scale * float(np.sqrt(np.mean((norms / scale) ** 2)))


def _checked_timing(
    dt_s: npt.ArrayLike,
    horizon_s: npt.ArrayLike,
    *,
    name: str,
) -> tuple[float, float, int]:
    """Validate one fixed-step interval and return ``(dt, horizon, steps)``."""
    dt = scalar(dt_s, f"{name}.dt_s", positive=True)
    horizon = scalar(horizon_s, f"{name}.horizon_s", positive=True)
    if horizon > MAX_HORIZON_S:
        raise ValueError(f"{name}.horizon_s cannot exceed {MAX_HORIZON_S} s")
    steps_float = horizon / dt
    if not math.isfinite(steps_float) or steps_float > MAX_SAMPLE_COUNT:
        raise ValueError(f"{name} cannot exceed {MAX_SAMPLE_COUNT} fixed steps")
    steps = int(round(steps_float))
    if steps < 1:
        raise ValueError(f"{name} must contain at least one fixed step")
    if abs(steps * dt - horizon) > _FIXED_STEP_TOLERANCE * max(1.0, horizon):
        raise ValueError(f"{name}.horizon_s must be an integer multiple of dt_s")
    return dt, horizon, steps


def _checked_pid_config(config: PIDConfig) -> PIDConfig:
    if not isinstance(config, PIDConfig):
        raise ValueError("config must be a PIDConfig")
    for name in ("kp", "ki", "kd"):
        value = getattr(config, name)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"PIDConfig.{name} must be finite and >= 0")
    for name in ("integral_limit", "derivative_tau_s", "accel_limit_mps2"):
        value = getattr(config, name)
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"PIDConfig.{name} must be finite and positive")
    if config.accel_limit_mps2 > 0.5:
        raise ValueError("PIDConfig.accel_limit_mps2 cannot exceed 0.5 m/s²")
    return config


def _checked_gains(gains: AlphaBetaGains) -> AlphaBetaGains:
    """Recheck the gain range instead of trusting the container alone."""
    if not isinstance(gains, AlphaBetaGains):
        raise ValueError("gains must be AlphaBetaGains")
    for name in ("alpha", "beta", "gamma"):
        value = getattr(gains, name)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"gains.{name} must be finite and in [0, 1]")
    return gains


def _read_only(array: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    array.setflags(write=False)
    return array


@dataclass(frozen=True, slots=True)
class PIDStepCase:
    """One synthetic velocity step on an ideal double integrator.

    ``config`` supplies the gains. They are test inputs, not competition values.
    Each interval applies a constant command exactly:
    ``position += velocity * dt + 0.5 * command * dt²`` and
    ``velocity += command * dt``. All vectors are three-component and must be
    finite. The horizon must be an integer multiple of ``dt_s`` and no longer
    than 300 s.
    """

    config: PIDConfig
    dt_s: float
    horizon_s: float
    target_velocity_mps: npt.ArrayLike
    initial_position_m: npt.ArrayLike = (0.0, 0.0, 0.0)
    initial_velocity_mps: npt.ArrayLike = (0.0, 0.0, 0.0)
    settle_band_fraction: float = 0.02

    def __post_init__(self) -> None:
        config = _checked_pid_config(self.config)
        dt, horizon, _steps = _checked_timing(self.dt_s, self.horizon_s, name="PIDStepCase")
        target = _finite_vec3(self.target_velocity_mps, "PIDStepCase.target_velocity_mps")
        position = _finite_vec3(self.initial_position_m, "PIDStepCase.initial_position_m")
        velocity = _finite_vec3(self.initial_velocity_mps, "PIDStepCase.initial_velocity_mps")
        band = scalar(
            self.settle_band_fraction,
            "PIDStepCase.settle_band_fraction",
            positive=True,
        )
        if band >= 1.0:
            raise ValueError("PIDStepCase.settle_band_fraction must be < 1")
        with np.errstate(over="ignore", invalid="ignore"):
            step = np.asarray(target) - np.asarray(velocity)
        _require_finite(step, name="PID velocity step")
        if not np.any(step):
            raise ValueError("PIDStepCase target and initial velocity must differ")
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "dt_s", dt)
        object.__setattr__(self, "horizon_s", horizon)
        object.__setattr__(self, "target_velocity_mps", target)
        object.__setattr__(self, "initial_position_m", position)
        object.__setattr__(self, "initial_velocity_mps", velocity)
        object.__setattr__(self, "settle_band_fraction", band)

    @property
    def sample_count(self) -> int:
        return _checked_timing(self.dt_s, self.horizon_s, name="PIDStepCase")[2]


@dataclass(frozen=True, slots=True)
class PIDStepMetrics:
    """Sampled step-response metrics for the synthetic double integrator.

    Rise time is the first 10% to first 90% axial-progress crossing. Overshoot
    is ``max(0, peak_progress - 1)`` as a fraction of the requested velocity
    step. Settling time is sampled: the first time after the last sample outside
    the band from which every later sample stays inside. Saturation is the
    fraction of commands whose norm reaches the configured acceleration cap
    within a 1e-9 relative tolerance. ``steady_state_error_mps`` is the final
    sampled error, not an established asymptotic value.
    """

    rise_time_s: float | None
    settling_time_s: float | None
    overshoot_fraction: float
    saturation_fraction: float
    peak_velocity_mps: float
    steady_state_error_mps: float
    max_command_norm_mps2: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class PIDStepResult:
    """Read-only synthetic trajectory; ``command_mps2`` has ``sample_count`` rows."""

    time_s: npt.NDArray[np.float64]
    position_m: npt.NDArray[np.float64]
    velocity_mps: npt.NDArray[np.float64]
    command_mps2: npt.NDArray[np.float64]
    metrics: PIDStepMetrics


def validate_pid_step(case: PIDStepCase) -> PIDStepResult:
    """Run one caller-supplied velocity step through the reference PID loop.

    The result is synthetic evidence about step metrics only. It does not select
    gains, prove flight stability or model a real shuttle or asteroid.
    """
    if not isinstance(case, PIDStepCase):
        raise ValueError("case must be a PIDStepCase")
    dt, _horizon, steps = _checked_timing(case.dt_s, case.horizon_s, name="PIDStepCase")
    target = np.asarray(case.target_velocity_mps, dtype=np.float64)
    initial_velocity = np.asarray(case.initial_velocity_mps, dtype=np.float64)
    position = np.asarray(case.initial_position_m, dtype=np.float64)
    velocity = initial_velocity.copy()
    controller = PIDVelocityController(case.config)

    time = np.empty(steps + 1, dtype=np.float64)
    positions = np.empty((steps + 1, 3), dtype=np.float64)
    velocities = np.empty((steps + 1, 3), dtype=np.float64)
    commands = np.empty((steps, 3), dtype=np.float64)
    time[0] = 0.0
    positions[0] = position
    velocities[0] = velocity

    for index in range(steps):
        command = controller.update(target, velocity, dt)
        commands[index] = command
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            position = position + velocity * dt + 0.5 * command * dt * dt
            velocity = velocity + command * dt
        _require_finite(position, velocity, name="synthetic PID response")
        time[index + 1] = (index + 1) * dt
        positions[index + 1] = position
        velocities[index + 1] = velocity

    if not (
        np.isfinite(time).all()
        and np.isfinite(positions).all()
        and np.isfinite(velocities).all()
        and np.isfinite(commands).all()
    ):
        raise ValueError("synthetic PID response is not finite")

    with np.errstate(over="ignore", invalid="ignore"):
        step = target - initial_velocity
    step_norm = math.hypot(*step)
    if not math.isfinite(step_norm) or step_norm == 0.0:
        raise ValueError("PID velocity step norm is not representable and positive")
    axis = step / step_norm
    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        progress = ((velocities - initial_velocity) / step_norm) @ axis
        velocity_errors = velocities - target
        relative_errors = velocity_errors / step_norm
    _require_finite(progress, velocity_errors, relative_errors, name="synthetic PID errors")
    error_fraction = _row_norms(relative_errors)

    ten_percent = np.flatnonzero(progress >= 0.1)
    ninety_percent = np.flatnonzero(progress >= 0.9)
    rise_time = None
    if ten_percent.size and ninety_percent.size:
        rise_time = float(time[ninety_percent[0]] - time[ten_percent[0]])

    outside = np.flatnonzero(error_fraction > case.settle_band_fraction)
    if outside.size == 0:
        settling_time: float | None = 0.0
    elif outside[-1] == steps:
        settling_time = None
    else:
        settling_time = float(time[outside[-1] + 1])

    command_norms = _row_norms(commands)
    saturation_threshold = case.config.accel_limit_mps2 * (1.0 - _SATURATION_TOLERANCE)
    saturation_fraction = float(np.count_nonzero(command_norms >= saturation_threshold)) / steps
    metrics = PIDStepMetrics(
        rise_time_s=rise_time,
        settling_time_s=settling_time,
        overshoot_fraction=max(0.0, float(np.max(progress)) - 1.0),
        saturation_fraction=saturation_fraction,
        peak_velocity_mps=float(_row_norms(velocities).max()),
        steady_state_error_mps=float(_row_norms(velocity_errors)[-1]),
        max_command_norm_mps2=float(np.max(command_norms)),
        sample_count=steps,
    )
    return PIDStepResult(
        time_s=_read_only(time),
        position_m=_read_only(positions),
        velocity_mps=_read_only(velocities),
        command_mps2=_read_only(commands),
        metrics=metrics,
    )


@dataclass(frozen=True, slots=True)
class TrackerScenario:
    """Synthetic constant-acceleration target and filter initial estimate.

    Analytical truth is ``p(t) = p0 + v0*t + 0.5*a*t²`` and ``v(t) = v0 + a*t``.
    Measurements are exact truth positions, sampled at every step. The initial
    estimate defaults to the truth when omitted. Gains are caller-supplied test
    inputs, not tuned values. ``horizon_s`` is limited to 300 s as a synthetic
    study bound, not as a validated asteroid forecast horizon. In the forecast
    APIs this horizon is the conditioning duration and measurement cutoff.
    Truth-valued initial estimates are conveniences, not learned sensor state.
    """

    dt_s: float
    horizon_s: float
    gains: AlphaBetaGains
    true_position_m: npt.ArrayLike
    true_velocity_mps: npt.ArrayLike
    true_acceleration_mps2: npt.ArrayLike
    initial_position_m: npt.ArrayLike | None = None
    initial_velocity_mps: npt.ArrayLike | None = None
    initial_acceleration_mps2: npt.ArrayLike | None = None

    def __post_init__(self) -> None:
        dt, horizon, _steps = _checked_timing(self.dt_s, self.horizon_s, name="TrackerScenario")
        gains = _checked_gains(self.gains)
        true_position = _finite_vec3(self.true_position_m, "TrackerScenario.true_position_m")
        true_velocity = _finite_vec3(self.true_velocity_mps, "TrackerScenario.true_velocity_mps")
        true_acceleration = _finite_vec3(
            self.true_acceleration_mps2,
            "TrackerScenario.true_acceleration_mps2",
        )
        initial_position = (
            true_position
            if self.initial_position_m is None
            else _finite_vec3(self.initial_position_m, "TrackerScenario.initial_position_m")
        )
        initial_velocity = (
            true_velocity
            if self.initial_velocity_mps is None
            else _finite_vec3(self.initial_velocity_mps, "TrackerScenario.initial_velocity_mps")
        )
        initial_acceleration = (
            true_acceleration
            if self.initial_acceleration_mps2 is None
            else _finite_vec3(
                self.initial_acceleration_mps2,
                "TrackerScenario.initial_acceleration_mps2",
            )
        )
        object.__setattr__(self, "gains", gains)
        object.__setattr__(self, "dt_s", dt)
        object.__setattr__(self, "horizon_s", horizon)
        object.__setattr__(self, "true_position_m", true_position)
        object.__setattr__(self, "true_velocity_mps", true_velocity)
        object.__setattr__(self, "true_acceleration_mps2", true_acceleration)
        object.__setattr__(self, "initial_position_m", initial_position)
        object.__setattr__(self, "initial_velocity_mps", initial_velocity)
        object.__setattr__(self, "initial_acceleration_mps2", initial_acceleration)

    @property
    def sample_count(self) -> int:
        return _checked_timing(self.dt_s, self.horizon_s, name="TrackerScenario")[2]


@dataclass(frozen=True, slots=True)
class TrackerErrorMetrics:
    """Errors over the sampled horizon.

    Filtering includes the initial sample; forecast metrics exclude the cutoff
    sample and score only positive prediction times. ``sample_count`` counts
    fixed intervals, so filtering RMSE uses one more state than that count.
    RMSE is the root mean square of the Euclidean vector-error norm, not a
    component-wise RMS. Final errors use the last sampled estimate. Alpha-beta
    has no acceleration state, so its reported acceleration estimate is zero and
    its acceleration error is the synthetic target acceleration.
    """

    position_rmse_m: float
    position_max_error_m: float
    final_position_error_m: float
    velocity_rmse_mps: float
    final_velocity_error_mps: float
    acceleration_rmse_mps2: float
    final_acceleration_error_mps2: float
    sample_count: int
    horizon_s: float


@dataclass(frozen=True, slots=True)
class TrackerValidationResult:
    """Read-only synthetic trajectory and errors over one fixed-step horizon."""

    time_s: npt.NDArray[np.float64]
    estimated_position_m: npt.NDArray[np.float64]
    estimated_velocity_mps: npt.NDArray[np.float64]
    estimated_acceleration_mps2: npt.NDArray[np.float64]
    position_error_m: npt.NDArray[np.float64]
    velocity_error_mps: npt.NDArray[np.float64]
    acceleration_error_mps2: npt.NDArray[np.float64]
    metrics: TrackerErrorMetrics


def _tracker_metrics(
    time: npt.NDArray[np.float64],
    position_error: npt.NDArray[np.float64],
    velocity_error: npt.NDArray[np.float64],
    acceleration_error: npt.NDArray[np.float64],
    *,
    exclude_initial: bool = False,
) -> TrackerErrorMetrics:
    _require_finite(time, position_error, velocity_error, acceleration_error, name="tracker errors")
    start = 1 if exclude_initial else 0
    position_norm = _row_norms(position_error[start:])
    velocity_norm = _row_norms(velocity_error[start:])
    acceleration_norm = _row_norms(acceleration_error[start:])
    return TrackerErrorMetrics(
        position_rmse_m=_root_mean_square(position_norm),
        position_max_error_m=float(np.max(position_norm)),
        final_position_error_m=float(position_norm[-1]),
        velocity_rmse_mps=_root_mean_square(velocity_norm),
        final_velocity_error_mps=float(velocity_norm[-1]),
        acceleration_rmse_mps2=_root_mean_square(acceleration_norm),
        final_acceleration_error_mps2=float(acceleration_norm[-1]),
        sample_count=int(time.size - 1),
        horizon_s=float(time[-1]),
    )


def _tracker_truth(
    scenario: TrackerScenario,
    time: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Evaluate truth independently of the estimator's recursive integration."""
    t = time[:, None]
    position = np.asarray(scenario.true_position_m, dtype=np.float64)
    velocity = np.asarray(scenario.true_velocity_mps, dtype=np.float64)
    acceleration = np.asarray(scenario.true_acceleration_mps2, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        positions = position + velocity * t + 0.5 * acceleration * t * t
        velocities = velocity + acceleration * t
    _require_finite(positions, velocities, name="synthetic tracker truth")
    return positions, velocities


def _run_tracker(
    scenario: TrackerScenario,
    *,
    use_gamma: bool,
) -> TrackerValidationResult:
    dt = scenario.dt_s
    steps = scenario.sample_count
    true_acceleration = np.asarray(scenario.true_acceleration_mps2, dtype=np.float64)

    if use_gamma:
        estimator: AlphaBetaTracker | AlphaBetaGammaTracker = AlphaBetaGammaTracker(
            scenario.initial_position_m,
            scenario.initial_velocity_mps,
            scenario.initial_acceleration_mps2,
            scenario.gains,
        )
    else:
        estimator = AlphaBetaTracker(
            scenario.initial_position_m,
            scenario.initial_velocity_mps,
            scenario.gains,
        )

    time = np.arange(steps + 1, dtype=np.float64) * dt
    true_positions, true_velocities = _tracker_truth(scenario, time)
    estimated_positions = np.empty((steps + 1, 3), dtype=np.float64)
    estimated_velocities = np.empty((steps + 1, 3), dtype=np.float64)
    estimated_accelerations = np.empty((steps + 1, 3), dtype=np.float64)
    position_errors = np.empty((steps + 1, 3), dtype=np.float64)
    velocity_errors = np.empty((steps + 1, 3), dtype=np.float64)
    acceleration_errors = np.empty((steps + 1, 3), dtype=np.float64)

    estimate_position = np.asarray(estimator.position_m, dtype=np.float64)
    estimate_velocity = np.asarray(estimator.velocity_mps, dtype=np.float64)
    if use_gamma:
        assert isinstance(estimator, AlphaBetaGammaTracker)
        estimate_acceleration = np.asarray(estimator.acceleration_mps2, dtype=np.float64)
    else:
        estimate_acceleration = np.zeros(3, dtype=np.float64)
    estimated_positions[0] = estimate_position
    estimated_velocities[0] = estimate_velocity
    estimated_accelerations[0] = estimate_acceleration

    for index in range(steps):
        if use_gamma:
            assert isinstance(estimator, AlphaBetaGammaTracker)
            estimate_position, estimate_velocity, estimate_acceleration = estimator.update(
                true_positions[index + 1],
                dt,
            )
        else:
            assert isinstance(estimator, AlphaBetaTracker)
            estimate_position, estimate_velocity = estimator.update(true_positions[index + 1], dt)
            estimate_acceleration = np.zeros(3, dtype=np.float64)
        estimated_positions[index + 1] = estimate_position
        estimated_velocities[index + 1] = estimate_velocity
        estimated_accelerations[index + 1] = estimate_acceleration

    with np.errstate(over="ignore", invalid="ignore"):
        position_errors[:] = estimated_positions - true_positions
        velocity_errors[:] = estimated_velocities - true_velocities
        acceleration_errors[:] = estimated_accelerations - true_acceleration

    if not (
        np.isfinite(estimated_positions).all()
        and np.isfinite(estimated_velocities).all()
        and np.isfinite(estimated_accelerations).all()
    ):
        raise ValueError("synthetic tracker response is not finite")

    return TrackerValidationResult(
        time_s=_read_only(time),
        estimated_position_m=_read_only(estimated_positions),
        estimated_velocity_mps=_read_only(estimated_velocities),
        estimated_acceleration_mps2=_read_only(estimated_accelerations),
        position_error_m=_read_only(position_errors),
        velocity_error_mps=_read_only(velocity_errors),
        acceleration_error_mps2=_read_only(acceleration_errors),
        metrics=_tracker_metrics(
            time,
            position_errors,
            velocity_errors,
            acceleration_errors,
        ),
    )


def validate_alpha_beta(scenario: TrackerScenario) -> TrackerValidationResult:
    """Track synthetic constant-acceleration truth with the alpha-beta filter.

    Every step receives a measurement. The horizon is a filtering study duration,
    not a prediction horizon. This is not a real asteroid forecast.
    Alpha-beta has no acceleration state; its acceleration error is reported
    against the synthetic truth for comparison with the gamma extension.
    """
    if not isinstance(scenario, TrackerScenario):
        raise ValueError("scenario must be a TrackerScenario")
    return _run_tracker(scenario, use_gamma=False)


def validate_alpha_beta_gamma(scenario: TrackerScenario) -> TrackerValidationResult:
    """Track synthetic constant-acceleration truth with the gamma extension.

    The same fixed-step, exact-measurement assumptions apply. This validates
    arithmetic and error behaviour on a synthetic case; it is not evidence for
    a 300 s asteroid forecast.
    """
    if not isinstance(scenario, TrackerScenario):
        raise ValueError("scenario must be a TrackerScenario")
    return _run_tracker(scenario, use_gamma=True)


@dataclass(frozen=True, slots=True)
class TrackerForecastResult:
    """Conditioned state followed by a measurement-free synthetic forecast.

    ``cutoff_s`` is the last measurement time since the scenario's origin.
    Forecast times are offsets from that cutoff and include the origin for
    plotting. Forecast metrics exclude that origin and score only positive
    prediction times. The conditioning metrics include their initial sample.
    """

    cutoff_s: float
    prediction_horizon_s: float
    conditioning: TrackerValidationResult
    forecast: TrackerValidationResult


def _run_tracker_forecast(
    conditioning: TrackerScenario,
    prediction_horizon_s: float,
    *,
    use_gamma: bool,
) -> TrackerForecastResult:
    if not isinstance(conditioning, TrackerScenario):
        raise ValueError("conditioning must be a TrackerScenario")
    dt, _horizon, steps = _checked_timing(
        conditioning.dt_s,
        prediction_horizon_s,
        name="tracker forecast",
    )
    if conditioning.sample_count + steps > MAX_SAMPLE_COUNT:
        raise ValueError(f"conditioning plus forecast cannot exceed {MAX_SAMPLE_COUNT} fixed steps")
    filtered = _run_tracker(conditioning, use_gamma=use_gamma)
    cutoff = float(filtered.time_s[-1])
    time = np.arange(steps + 1, dtype=np.float64) * dt
    t = time[:, None]
    position = filtered.estimated_position_m[-1]
    velocity = filtered.estimated_velocity_mps[-1]
    acceleration = filtered.estimated_acceleration_mps2[-1]
    # Freeze the cutoff state. No update or measurement access occurs here.
    with np.errstate(over="ignore", invalid="ignore", under="ignore"):
        positions = position + velocity * t + 0.5 * acceleration * t * t
        velocities = velocity + acceleration * t
        accelerations = np.broadcast_to(acceleration, positions.shape).copy()
    true_positions, true_velocities = _tracker_truth(conditioning, cutoff + time)
    with np.errstate(over="ignore", invalid="ignore"):
        position_errors = positions - true_positions
        velocity_errors = velocities - true_velocities
        acceleration_errors = accelerations - np.asarray(conditioning.true_acceleration_mps2)
    _require_finite(positions, velocities, accelerations, name="synthetic tracker forecast")
    forecast = TrackerValidationResult(
        time_s=_read_only(time),
        estimated_position_m=_read_only(positions),
        estimated_velocity_mps=_read_only(velocities),
        estimated_acceleration_mps2=_read_only(accelerations),
        position_error_m=_read_only(position_errors),
        velocity_error_mps=_read_only(velocity_errors),
        acceleration_error_mps2=_read_only(acceleration_errors),
        metrics=_tracker_metrics(
            time,
            position_errors,
            velocity_errors,
            acceleration_errors,
            exclude_initial=True,
        ),
    )
    return TrackerForecastResult(
        cutoff_s=cutoff,
        prediction_horizon_s=float(time[-1]),
        conditioning=filtered,
        forecast=forecast,
    )


def validate_alpha_beta_forecast(
    conditioning: TrackerScenario,
    *,
    prediction_horizon_s: float,
) -> TrackerForecastResult:
    """Condition a CV filter through ``conditioning.horizon_s``, then hold out.

    The caller supplies all gains and initial/truth states. The extra prediction
    horizon is positive, at most 300 s and on the same fixed grid. Conditioning
    plus prediction cannot exceed 100,000 steps. Truth after the cutoff is used
    only for scoring, never for correcting or fitting the frozen cutoff state.
    """
    return _run_tracker_forecast(conditioning, prediction_horizon_s, use_gamma=False)


def validate_alpha_beta_gamma_forecast(
    conditioning: TrackerScenario,
    *,
    prediction_horizon_s: float,
) -> TrackerForecastResult:
    """Condition the CA filter, then forecast without post-cutoff measurements.

    Timing, work bounds and scoring match ``validate_alpha_beta_forecast``.
    Even an exact synthetic CA result does not validate real asteroid dynamics.
    """
    return _run_tracker_forecast(conditioning, prediction_horizon_s, use_gamma=True)


@dataclass(frozen=True, slots=True)
class ZeroGammaReduction:
    """Exact alpha-beta equivalence check for a zero gamma and zero acceleration state."""

    max_position_difference_m: float
    max_velocity_difference_mps: float
    max_acceleration_estimate_mps2: float
    bitwise_equal: bool
    sample_count: int
    horizon_s: float


def validate_zero_gamma_reduction(scenario: TrackerScenario) -> ZeroGammaReduction:
    """Verify that gamma zero reduces exactly to the alpha-beta update.

    Both filters receive the same synthetic measurements, initial position and
    initial velocity. The gamma filter starts with zero acceleration and gamma is
    forced to zero, which is the reduction condition. The position and velocity
    histories must match bitwise.
    """
    if not isinstance(scenario, TrackerScenario):
        raise ValueError("scenario must be a TrackerScenario")
    zero_gains = AlphaBetaGains(
        alpha=scenario.gains.alpha,
        beta=scenario.gains.beta,
        gamma=0.0,
    )
    reduced = replace(
        scenario,
        gains=zero_gains,
        initial_acceleration_mps2=(0.0, 0.0, 0.0),
    )
    alpha_beta = _run_tracker(reduced, use_gamma=False)
    alpha_beta_gamma = _run_tracker(reduced, use_gamma=True)
    position_difference = float(
        np.max(np.abs(alpha_beta.estimated_position_m - alpha_beta_gamma.estimated_position_m))
    )
    velocity_difference = float(
        np.max(np.abs(alpha_beta.estimated_velocity_mps - alpha_beta_gamma.estimated_velocity_mps))
    )
    acceleration_norm = float(np.max(np.abs(alpha_beta_gamma.estimated_acceleration_mps2)))
    return ZeroGammaReduction(
        max_position_difference_m=position_difference,
        max_velocity_difference_mps=velocity_difference,
        max_acceleration_estimate_mps2=acceleration_norm,
        bitwise_equal=bool(
            np.array_equal(
                alpha_beta.estimated_position_m,
                alpha_beta_gamma.estimated_position_m,
            )
            and np.array_equal(
                alpha_beta.estimated_velocity_mps,
                alpha_beta_gamma.estimated_velocity_mps,
            )
        ),
        sample_count=reduced.sample_count,
        horizon_s=reduced.horizon_s,
    )


@dataclass(frozen=True, slots=True)
class GammaEffect:
    """Synthetic gamma comparison.

    ``*_change`` values are ``with_gamma - without_gamma``; a negative value
    means the gamma case has lower error on this synthetic case. This is not a
    forecast-error measurement and does not select a gain.
    """

    without_gamma: TrackerValidationResult
    with_gamma: TrackerValidationResult
    position_rmse_change_m: float
    velocity_rmse_change_mps: float
    acceleration_rmse_change_mps2: float


def compare_gamma_effect(scenario: TrackerScenario) -> GammaEffect:
    """Quantify gamma's effect while holding alpha, beta and initial state fixed.

    The baseline is the same alpha-beta-gamma filter with gamma forced to zero,
    so the comparison isolates gamma. The scenario's gamma must be positive. The
    outcome is synthetic only; it does not establish a real asteroid forecast
    horizon or a competition gain.
    """
    if not isinstance(scenario, TrackerScenario):
        raise ValueError("scenario must be a TrackerScenario")
    if scenario.gains.gamma <= 0.0:
        raise ValueError("compare_gamma_effect requires gains.gamma > 0")
    without_gamma = _run_tracker(
        replace(
            scenario,
            gains=AlphaBetaGains(
                alpha=scenario.gains.alpha,
                beta=scenario.gains.beta,
                gamma=0.0,
            ),
        ),
        use_gamma=True,
    )
    with_gamma = _run_tracker(scenario, use_gamma=True)
    return GammaEffect(
        without_gamma=without_gamma,
        with_gamma=with_gamma,
        position_rmse_change_m=(
            with_gamma.metrics.position_rmse_m - without_gamma.metrics.position_rmse_m
        ),
        velocity_rmse_change_mps=(
            with_gamma.metrics.velocity_rmse_mps - without_gamma.metrics.velocity_rmse_mps
        ),
        acceleration_rmse_change_mps2=(
            with_gamma.metrics.acceleration_rmse_mps2 - without_gamma.metrics.acceleration_rmse_mps2
        ),
    )
